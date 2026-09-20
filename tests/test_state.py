"""Config loading, state persistence, fail-closed behavior, and decision logging."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.decision_logger import (  # noqa: E402
    build_record,
    decisions_for_month,
    log_decision,
    new_decision_id,
    read_decisions,
    redact,
)
from src.guardrails import normalize, validate  # noqa: E402
from src.models import parse_money  # noqa: E402
from src.state import (  # noqa: E402
    BUDGET_SCHEMA_VERSION,
    ConfigError,
    CorruptStateError,
    atomic_write_json,
    current_month,
    fresh_state,
    load_budget_state,
    load_config,
    save_budget_state,
)
from tests.helpers import buy_decision, make_config, make_state, wait_decision  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rh-agent-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)

    def write(self, name: str, payload) -> str:
        target = self.path(name)
        with open(target, "w", encoding="utf-8") as handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                json.dump(payload, handle)
        return target


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


class ConfigTests(TempDirTestCase):
    def test_shipped_config_loads_and_matches_the_policy(self):
        config = load_config(os.path.join(REPO, "config.json"))
        self.assertEqual(config.monthly_budget_usd, Decimal("25.00"))
        self.assertFalse(config.live_trading)
        self.assertFalse(config.allow_selling)
        self.assertFalse(config.allow_options)
        self.assertFalse(config.allow_margin)
        self.assertFalse(config.allow_shorting)
        self.assertTrue(config.allow_crypto, "version 2 enables direct crypto")
        self.assertFalse(config.allow_leveraged_etfs)
        self.assertFalse(config.allow_inverse_etfs)
        self.assertFalse(config.allow_transfers)
        self.assertEqual(config.strategy, "long_term_buy_and_hold")
        self.assertGreaterEqual(config.min_investment_horizon_months, 24)

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load_config(self.path("nope.json"))

    def test_invalid_json_raises(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("bad.json", "{ not json"))

    def test_missing_flag_raises(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("c.json", {"monthly_budget_usd": "25.00"}))

    def test_non_boolean_flag_raises(self):
        payload = json.loads(read_text(os.path.join(REPO, "config.json")))
        payload["live_trading"] = "false"
        with self.assertRaises(ConfigError):
            load_config(self.write("c.json", payload))

    def test_absurd_budget_is_refused(self):
        payload = json.loads(read_text(os.path.join(REPO, "config.json")))
        payload["monthly_budget_usd"] = "9999999.00"
        with self.assertRaises(ConfigError):
            load_config(self.write("c.json", payload))

    def test_zero_or_negative_budget_is_refused(self):
        payload = json.loads(read_text(os.path.join(REPO, "config.json")))
        for value in ("0.00", "-25.00"):
            payload["monthly_budget_usd"] = value
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    load_config(self.write("c-%s.json" % value, payload))


# --------------------------------------------------------------------------
# Budget state persistence
# --------------------------------------------------------------------------


def valid_state_doc(month="2026-09", authorized="25.00", committed="0.00", remaining=None, **extra):
    if remaining is None:
        try:
            remaining = str(Decimal(str(authorized)) - Decimal(str(committed)))
        except Exception:
            remaining = str(authorized)
    doc = {
        "schema_version": BUDGET_SCHEMA_VERSION,
        "month": month,
        "authorized_budget_usd": authorized,
        "committed_usd": committed,
        "remaining_usd": remaining,
        "acted_decision_ids": [],
        "last_updated": "2026-09-01T00:00:00Z",
    }
    doc.update(extra)
    return doc


class BudgetStatePersistenceTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.config = make_config()

    def test_shipped_state_file_loads(self):
        config = load_config(os.path.join(REPO, "config.json"))
        state = load_budget_state(config, os.path.join(REPO, "state", "budget.json"))
        self.assertLessEqual(state.committed_usd, config.monthly_budget_usd)
        self.assertEqual(state.authorized_budget_usd, config.monthly_budget_usd)

    def test_round_trip(self):
        target = self.path("budget.json")
        original = fresh_state(self.config, month=current_month(), path=target)
        save_budget_state(original, target)
        reloaded = load_budget_state(self.config, target)
        self.assertEqual(reloaded.month, original.month)
        self.assertEqual(reloaded.remaining_usd, Decimal("25.00"))

    def test_writes_are_atomic_and_leave_no_temp_files(self):
        target = self.path("budget.json")
        save_budget_state(fresh_state(self.config, path=target), target)
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_money_is_persisted_as_strings_not_floats(self):
        target = self.path("budget.json")
        save_budget_state(fresh_state(self.config, path=target), target)
        raw = json.loads(read_text(target))
        for key in ("authorized_budget_usd", "committed_usd", "remaining_usd"):
            self.assertIsInstance(raw[key], str)


class FailClosedTests(TempDirTestCase):
    """Corrupt state must raise, and must never be silently rewritten."""

    def setUp(self):
        super().setUp()
        self.config = make_config()

    def assert_fails_closed(self, name, payload):
        target = self.write(name, payload)
        before = read_bytes(target)
        with self.assertRaises(CorruptStateError):
            load_budget_state(self.config, target)
        self.assertEqual(read_bytes(target), before, "corrupt state was modified")

    def test_missing_file_fails_closed(self):
        with self.assertRaises(CorruptStateError):
            load_budget_state(self.config, self.path("absent.json"))

    def test_empty_file_fails_closed(self):
        self.assert_fails_closed("empty.json", "")

    def test_truncated_json_fails_closed(self):
        self.assert_fails_closed("trunc.json", '{"schema_version": 1, "month": "2026-0')

    def test_json_array_fails_closed(self):
        self.assert_fails_closed("arr.json", "[1, 2, 3]")

    def test_wrong_schema_version_fails_closed(self):
        self.assert_fails_closed("v.json", valid_state_doc(schema_version=99))

    def test_bad_month_format_fails_closed(self):
        for month in ("2026-13", "202609", "September", "2026-9"):
            with self.subTest(month=month):
                self.assert_fails_closed("m-%s.json" % month, valid_state_doc(month=month))

    def test_future_month_fails_closed(self):
        self.assert_fails_closed("future.json", valid_state_doc(month="2999-01"))

    def test_inconsistent_arithmetic_fails_closed(self):
        self.assert_fails_closed(
            "math.json", valid_state_doc(committed="10.00", remaining="25.00")
        )

    def test_negative_committed_fails_closed(self):
        self.assert_fails_closed("neg.json", valid_state_doc(committed="-5.00", remaining="30.00"))

    def test_committed_over_authorized_fails_closed(self):
        self.assert_fails_closed(
            "over.json", valid_state_doc(committed="30.00", remaining="-5.00")
        )

    def test_authorization_above_the_configured_budget_fails_closed(self):
        """The classic carryover attack: a state file claiming a bigger budget."""
        self.assert_fails_closed(
            "carryover.json",
            valid_state_doc(authorized="75.00", committed="0.00", remaining="75.00"),
        )

    def test_float_money_in_state_fails_closed(self):
        self.assert_fails_closed("float.json", valid_state_doc(committed=10.0, remaining=15.0))

    def test_unparseable_money_fails_closed(self):
        self.assert_fails_closed("garbage.json", valid_state_doc(committed="ten dollars"))

    def test_duplicate_acted_ids_fail_closed(self):
        self.assert_fails_closed(
            "dupes.json", valid_state_doc(acted_decision_ids=["a", "a"])
        )

    def test_non_string_acted_ids_fail_closed(self):
        self.assert_fails_closed("ids.json", valid_state_doc(acted_decision_ids=[1, 2]))


class RolloverOnLoadTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.config = make_config()

    def test_loading_in_a_later_month_resets_spending(self):
        target = self.write(
            "budget.json",
            valid_state_doc(
                month="2026-08",
                committed="25.00",
                remaining="0.00",
                acted_decision_ids=["aug-1"],
            ),
        )
        later = datetime(2026, 9, 15, tzinfo=timezone.utc)
        state = load_budget_state(self.config, target, now=later)
        self.assertEqual(state.month, "2026-09")
        self.assertEqual(state.committed_usd, Decimal("0.00"))
        self.assertEqual(state.remaining_usd, Decimal("25.00"))
        self.assertIn("aug-1", state.acted_decision_ids)

    def test_rollover_does_not_carry_unused_budget_forward(self):
        target = self.write(
            "budget.json", valid_state_doc(month="2026-08", committed="0.00", remaining="25.00")
        )
        later = datetime(2026, 9, 1, tzinfo=timezone.utc)
        state = load_budget_state(self.config, target, now=later)
        self.assertEqual(state.authorized_budget_usd, Decimal("25.00"))
        self.assertEqual(state.remaining_usd, Decimal("25.00"))

    def test_rollover_is_not_written_to_disk_on_a_read(self):
        target = self.write("budget.json", valid_state_doc(month="2026-08"))
        before = read_bytes(target)
        load_budget_state(self.config, target, now=datetime(2026, 9, 1, tzinfo=timezone.utc))
        self.assertEqual(read_bytes(target), before)

    def test_same_month_load_preserves_committed(self):
        target = self.write(
            "budget.json", valid_state_doc(month="2026-09", committed="12.00", remaining="13.00")
        )
        state = load_budget_state(
            self.config, target, now=datetime(2026, 9, 20, tzinfo=timezone.utc)
        )
        self.assertEqual(state.month, "2026-09")
        self.assertEqual(state.committed_usd, Decimal("12.00"))
        self.assertEqual(state.remaining_usd, Decimal("13.00"))


class AtomicWriteTests(TempDirTestCase):
    def test_atomic_write_replaces_content_completely(self):
        target = self.path("x.json")
        atomic_write_json(target, {"a": 1})
        atomic_write_json(target, {"b": 2})
        self.assertEqual(json.loads(read_text(target)), {"b": 2})

    def test_atomic_write_creates_missing_directories(self):
        target = self.path(os.path.join("deep", "nested", "x.json"))
        atomic_write_json(target, {"ok": True})
        self.assertTrue(os.path.exists(target))


# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------


class MoneyTests(unittest.TestCase):
    def test_strings_ints_and_decimals_parse(self):
        self.assertEqual(parse_money("25.00"), Decimal("25.00"))
        self.assertEqual(parse_money("$1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_money(25), Decimal("25"))
        self.assertEqual(parse_money(Decimal("0.01")), Decimal("0.01"))

    def test_float_uses_the_decimal_repr_not_the_binary_expansion(self):
        self.assertEqual(parse_money(0.1), Decimal("0.1"))
        self.assertNotEqual(parse_money(0.1), Decimal(0.1))

    def test_decimal_addition_is_exact(self):
        total = Decimal("0.10") + Decimal("0.20")
        self.assertEqual(total, Decimal("0.30"))
        self.assertTrue(total <= Decimal("0.30"))


# --------------------------------------------------------------------------
# Decision logging
# --------------------------------------------------------------------------


class RedactionTests(unittest.TestCase):
    def test_credential_shaped_keys_are_stripped(self):
        payload = {
            "access_token": "abc123",
            "cookie": "session=xyz",
            "Authorization": "Bearer sk-live-1234567890",
            "account_number": "100200300",
            "device_id": "d-1",
            "ticker": "VTI",
        }
        cleaned = redact(payload)
        self.assertEqual(cleaned["access_token"], "[REDACTED]")
        self.assertEqual(cleaned["cookie"], "[REDACTED]")
        self.assertEqual(cleaned["Authorization"], "[REDACTED]")
        self.assertEqual(cleaned["account_number"], "[REDACTED]")
        self.assertEqual(cleaned["device_id"], "[REDACTED]")
        self.assertEqual(cleaned["ticker"], "VTI")

    def test_account_numbers_inside_free_text_are_masked(self):
        cleaned = redact({"thesis": "Checked account 100200300 for positions."})
        self.assertNotIn("100200300", cleaned["thesis"])
        self.assertIn("0300", cleaned["thesis"])

    def test_bearer_tokens_inside_free_text_are_masked(self):
        cleaned = redact({"note": "sent Bearer eyJhbGciOiJIUzI1NiJ9 upstream"})
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", cleaned["note"])

    def test_budget_field_names_survive_redaction(self):
        """'authorized' contains 'auth' but is not a credential."""
        payload = {
            "monthly_budget_authorized": "25.00",
            "authorized_budget_usd": "25.00",
            "author": "agent",
            "session_note": "sensitive",
        }
        cleaned = redact(payload)
        self.assertEqual(cleaned["monthly_budget_authorized"], "25.00")
        self.assertEqual(cleaned["authorized_budget_usd"], "25.00")
        self.assertEqual(cleaned["author"], "agent")
        self.assertEqual(cleaned["session_note"], "[REDACTED]")

    def test_camel_case_credential_keys_are_stripped(self):
        cleaned = redact({"accessToken": "abc", "refreshToken": "def", "tickerSymbol": "VTI"})
        self.assertEqual(cleaned["accessToken"], "[REDACTED]")
        self.assertEqual(cleaned["refreshToken"], "[REDACTED]")
        self.assertEqual(cleaned["tickerSymbol"], "VTI")

    def test_redaction_recurses(self):
        cleaned = redact({"evidence": [{"api_key": "k"}, {"symbol": "VTI"}]})
        self.assertEqual(cleaned["evidence"][0]["api_key"], "[REDACTED]")
        self.assertEqual(cleaned["evidence"][1]["symbol"], "VTI")

    def test_a_boolean_under_a_secret_looking_key_survives(self):
        """No credential is a boolean, and clobbering one corrupts a decision.

        'actionable_with_this_month_authorization' matches the key rule on
        "authorization". Replacing its value with the string "[REDACTED]" turned
        a field the guardrails require to be true or false into a violation, and
        made the logged record unapprovable.
        """
        payload = {
            "events_considered": [
                {"label": "August CPI",
                 "actionable_with_this_month_authorization": True,
                 "occurs_after_close": False},
                {"label": "MU FQ4 2026",
                 "actionable_with_this_month_authorization": False,
                 "occurs_after_close": True},
            ],
        }
        cleaned = redact(payload)
        self.assertIs(
            cleaned["events_considered"][0]["actionable_with_this_month_authorization"],
            True)
        self.assertIs(
            cleaned["events_considered"][1]["actionable_with_this_month_authorization"],
            False)

    def test_none_under_a_secret_looking_key_survives(self):
        self.assertIsNone(redact({"session": None})["session"])

    def test_the_exemption_does_not_extend_to_numbers(self):
        """An account number as an int is exactly what the key rule is for."""
        cleaned = redact({"account_number": 123456789, "pin": 1234,
                          "auth_attempts": 3})
        self.assertEqual(cleaned["account_number"], "[REDACTED]")
        self.assertEqual(cleaned["pin"], "[REDACTED]")
        self.assertEqual(cleaned["auth_attempts"], "[REDACTED]")

    def test_the_exemption_does_not_extend_to_strings(self):
        cleaned = redact({"authorization": "Bearer sk-live-999",
                          "oauth": "abc", "jwt": "x.y.z"})
        for key in ("authorization", "oauth", "jwt"):
            with self.subTest(key=key):
                self.assertEqual(cleaned[key], "[REDACTED]")

    def test_a_logged_decision_keeps_its_event_actionability(self):
        """The end-to-end version of the same bug, through build_record."""
        raw = buy_decision(make_state(), "10.00")
        raw["events_considered"] = [
            {"label": "August CPI", "date": "2026-09-11", "asset_class": "EQUITY",
             "occurs_after_close": False,
             "actionable_with_this_month_authorization": True}]
        config = make_config()
        state = make_state(config)
        proposal, _ = normalize(raw)
        record = build_record(proposal, validate(raw, config, state), state,
                              raw_decision=raw)
        self.assertIs(
            record["events_considered"][0]["actionable_with_this_month_authorization"],
            True)
        again = validate(record, config, state)
        self.assertTrue(again.valid, again.violation_codes)


class DecisionLogTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.config = make_config()
        self.state = make_state(self.config)
        self.log_path = self.path("decisions.jsonl")

    def _log(self, raw):
        proposal, _ = normalize(raw)
        result = validate(raw, self.config, self.state)
        record = build_record(proposal, result, self.state, raw_decision=raw)
        return log_decision(record, self.log_path), result

    def test_a_buy_record_carries_the_required_fields(self):
        record, result = self._log(buy_decision(self.state, "25.00"))
        self.assertTrue(result.valid, result.violation_codes)
        for field in (
            "decision_id", "timestamp", "decision", "ticker", "proposed_amount",
            "monthly_budget_before", "monthly_budget_after", "confidence", "thesis",
            "timing_reason", "alternatives_considered", "risks", "evidence",
            "validation_result", "execution_status",
        ):
            self.assertIn(field, record)
        self.assertEqual(record["decision"], "BUY")
        self.assertEqual(record["proposed_amount"], "25.00")
        self.assertEqual(record["monthly_budget_before"], "25.00")
        self.assertEqual(record["monthly_budget_after"], "0.00")
        self.assertEqual(record["monthly_budget_authorized"], "25.00")

    def test_every_record_is_marked_dry_run(self):
        record, _ = self._log(buy_decision(self.state, "5.00"))
        self.assertEqual(record["execution_status"], "DRY_RUN_NOT_EXECUTED")
        self.assertFalse(record["live_trading"])
        self.assertFalse(record["validation_result"]["executable"])

    def test_a_rejected_decision_is_still_logged(self):
        record, result = self._log(buy_decision(self.state, "999.00", decision_id="too-big"))
        self.assertFalse(result.valid)
        self.assertFalse(record["validation_result"]["valid"])
        self.assertIn("EXCEEDS_MONTHLY_BUDGET", [v["code"] for v in record["validation_result"]["violations"]])
        self.assertFalse(record["would_have_purchased"])

    def test_a_wait_record_is_logged(self):
        record, result = self._log(wait_decision(self.state))
        self.assertTrue(result.valid, result.violation_codes)
        self.assertEqual(record["decision"], "WAIT")
        self.assertEqual(record["proposed_amount"], "0.00")

    def test_logging_never_writes_execution_status_other_than_dry_run(self):
        with self.assertRaises(ValueError):
            log_decision({"decision": "BUY", "execution_status": "EXECUTED"}, self.log_path)

    def test_log_is_one_json_object_per_line(self):
        self._log(buy_decision(self.state, "5.00", decision_id="a"))
        self._log(wait_decision(self.state, decision_id="b"))
        lines = [line for line in read_text(self.log_path).splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)

    def test_secrets_never_reach_the_log(self):
        raw = buy_decision(self.state, "5.00")
        raw["evidence"] = [{"tool": "get_portfolio", "account_number": "100200300", "auth_token": "s3cret"}]
        record, _ = self._log(raw)
        blob = read_text(self.log_path)
        self.assertNotIn("s3cret", blob)
        self.assertNotIn("100200300", blob)

    def test_records_can_be_read_back_and_filtered_by_month(self):
        self._log(buy_decision(self.state, "5.00", decision_id="a"))
        records = read_decisions(self.log_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(len(decisions_for_month(self.state.month, self.log_path)), 1)
        self.assertEqual(len(decisions_for_month("1999-01", self.log_path)), 0)

    def test_decision_ids_are_unique(self):
        ids = {new_decision_id() for _ in range(500)}
        self.assertEqual(len(ids), 500)


class ShippedRepoTests(unittest.TestCase):
    def test_the_shipped_log_contains_only_dry_run_records(self):
        for record in read_decisions(os.path.join(REPO, "logs", "decisions.jsonl")):
            self.assertEqual(record.get("execution_status"), "DRY_RUN_NOT_EXECUTED")

    def test_no_robinhood_account_number_is_stored_in_the_repo(self):
        for folder in ("state", "logs", ""):
            directory = os.path.join(REPO, folder) if folder else REPO
            for name in os.listdir(directory):
                target = os.path.join(directory, name)
                if not os.path.isfile(target) or not name.endswith((".json", ".jsonl")):
                    continue
                content = read_text(target)
                with self.subTest(file=target):
                    self.assertNotIn("100200300", content)


if __name__ == "__main__":
    unittest.main()


class TheLoggedRecordMustSurviveRevalidationTests(TempDirTestCase):
    """A logged decision has to pass the guardrails a second time.

    Approval does not re-read the payload that was validated — it re-reads the
    **ledger record** and validates that (``scripts/approve_decision.py``). So a
    record that drops fields the guardrails require is not merely lossy: it is
    unapprovable, and it fails at the last possible moment with
    MISSING_REQUIRED_FIELD on a decision that was complete when it was written.

    This is the invariant that was missing when ``build_record`` persisted only a
    hand-picked projection of the payload.
    """

    def setUp(self):
        super().setUp()
        self.config = make_config()
        self.state = make_state(self.config)

    def record_for(self, raw):
        proposal, _ = normalize(raw)
        result = validate(raw, self.config, self.state)
        self.assertTrue(result.valid, result.violation_codes)
        return build_record(proposal, result, self.state, raw_decision=raw)

    def test_a_logged_new_position_buy_revalidates(self):
        record = self.record_for(buy_decision(self.state, "10.00"))
        again = validate(record, self.config, self.state)
        self.assertTrue(again.valid, again.violation_codes)

    def test_a_logged_add_to_existing_buy_revalidates(self):
        from tests.helpers import add_to_existing

        record = self.record_for(add_to_existing(self.state, "10.00"))
        again = validate(record, self.config, self.state)
        self.assertTrue(again.valid, again.violation_codes)

    def test_a_logged_crypto_buy_revalidates(self):
        from tests.helpers import crypto_decision, make_crypto_universe

        raw = crypto_decision(self.state, "10.00")
        proposal, _ = normalize(raw)
        universe = make_crypto_universe()
        result = validate(raw, self.config, self.state, crypto_universe=universe)
        self.assertTrue(result.valid, result.violation_codes)
        record = build_record(proposal, result, self.state, raw_decision=raw)
        again = validate(record, self.config, self.state, crypto_universe=universe)
        self.assertTrue(again.valid, again.violation_codes)

    def test_a_logged_wait_revalidates(self):
        record = self.record_for(wait_decision(self.state))
        again = validate(record, self.config, self.state)
        self.assertTrue(again.valid, again.violation_codes)

    def test_every_block_the_guardrails_require_on_a_buy_is_persisted(self):
        record = self.record_for(buy_decision(self.state, "10.00"))
        for field in ("why_not_wait", "margin_for_error", "monthly_optionality",
                      "portfolio_sprawl_assessment", "theme",
                      "new_position_justification", "research_package"):
            with self.subTest(field=field):
                self.assertIn(field, record, "%s was dropped by build_record" % field)

    def test_a_wait_does_not_acquire_empty_buy_blocks(self):
        """Absent stays absent — a null block is not the same as no block."""
        record = self.record_for(wait_decision(self.state))
        for field in ("new_position_justification", "research_package",
                      "margin_for_error"):
            with self.subTest(field=field):
                self.assertNotIn(field, record)

    def test_the_carried_set_covers_what_a_buy_needs(self):
        """Guards against a future required field being added and not carried."""
        from src.decision_logger import CARRIED_VERBATIM

        raw = buy_decision(self.state, "10.00")
        record = self.record_for(raw)
        for key in raw:
            if key in record:
                continue
            with self.subTest(key=key):
                stripped = {k: v for k, v in raw.items() if k != key}
                result = validate(stripped, self.config, self.state)
                self.assertTrue(
                    result.valid,
                    "%r is required by the guardrails but is neither projected "
                    "into the record nor in CARRIED_VERBATIM (%s)"
                    % (key, sorted(CARRIED_VERBATIM)))


class PolicyFingerprintSemanticsTests(TempDirTestCase):
    """What counts as a policy change, and what is merely a rewrite.

    The fingerprint voids outstanding approvals. That is the right behaviour
    for a real policy change and a false alarm for a formatting rewrite — and
    false alarms are expensive, because an operator who has seen three of them
    stops believing the fourth. config.json is the one policy file an operator
    must edit mid-procedure, so it is hashed by parsed meaning; the rest keep
    raw-byte hashing, where the bytes *are* the meaning.
    """

    def setUp(self):
        super().setUp()
        from src.approval import POLICY_SURFACE_FILES

        self.surface = POLICY_SURFACE_FILES
        for rel in self.surface:
            dst = os.path.join(self.tmp, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(rel, dst)

    def fingerprint(self):
        from src.approval import policy_fingerprint

        return policy_fingerprint(self.tmp)

    def rewrite_config(self, **changes):
        path = os.path.join(self.tmp, "config.json")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        payload.update(changes)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=8, sort_keys=True)
        return path

    def test_a_formatting_only_rewrite_does_not_change_the_fingerprint(self):
        before = self.fingerprint()
        self.rewrite_config()          # different indent, different key order
        self.assertEqual(self.fingerprint(), before)

    def test_whitespace_and_key_order_are_both_irrelevant(self):
        before = self.fingerprint()
        path = os.path.join(self.tmp, "config.json")
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(reversed(list(payload.items()))),
                                    separators=(",", ":")))
        self.assertEqual(self.fingerprint(), before)

    def test_every_semantic_config_change_still_changes_it(self):
        for field, value in (("live_trading", True),
                             ("agent_enabled", True),
                             ("execution_mode", "APPROVAL_REQUIRED"),
                             ("monthly_budget_usd", "50.00"),
                             ("min_investment_horizon_months", 12),
                             ("allow_selling", True)):
            with self.subTest(field=field):
                for rel in self.surface:
                    shutil.copy(rel, os.path.join(self.tmp, rel))
                before = self.fingerprint()
                self.rewrite_config(**{field: value})
                self.assertNotEqual(self.fingerprint(), before,
                                    "%s change did not void approvals" % field)

    def test_adding_or_removing_a_key_changes_it(self):
        before = self.fingerprint()
        self.rewrite_config(a_new_flag=False)
        self.assertNotEqual(self.fingerprint(), before)

    def test_textual_policy_files_stay_byte_sensitive(self):
        """A .md or .py file's bytes are its meaning — no canonicalization."""
        for rel in ("INVESTMENT_POLICY.md", "src/guardrails.py", "src/models.py"):
            with self.subTest(rel=rel):
                for surface in self.surface:
                    shutil.copy(surface, os.path.join(self.tmp, surface))
                before = self.fingerprint()
                with open(os.path.join(self.tmp, rel), "a", encoding="utf-8") as handle:
                    handle.write("\n")
                self.assertNotEqual(self.fingerprint(), before,
                                    "%s stopped being byte-sensitive" % rel)

    def test_only_config_json_is_canonicalized(self):
        from src.approval import CANONICAL_JSON_POLICY_FILES

        self.assertEqual(set(CANONICAL_JSON_POLICY_FILES), {"config.json"})

    def test_an_unparseable_config_fails_closed(self):
        from src.approval import ApprovalError

        with open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(ApprovalError):
            self.fingerprint()

    def test_a_missing_policy_file_fails_closed(self):
        from src.approval import ApprovalError

        os.remove(os.path.join(self.tmp, "config.json"))
        with self.assertRaises(ApprovalError):
            self.fingerprint()


class TradingClockTests(unittest.TestCase):
    """One clock per question. Using one clock for all of them was a bug.

    A decision written at 19:10 America/Chicago was rejected minutes later for
    reporting the wrong number of days remaining, because UTC had rolled to the
    next day while neither the owner's date nor the market's had.
    """

    def setUp(self):
        from src import market_calendar

        self.mc = market_calendar

    def test_the_three_zones_are_declared(self):
        self.assertEqual(self.mc.PROJECT_TIMEZONE, "America/Chicago")
        self.assertEqual(self.mc.EXCHANGE_TIMEZONE, "America/New_York")
        self.assertEqual(self.mc.CRYPTO_TIMEZONE, "UTC")

    def test_late_evening_chicago_is_still_the_same_project_day(self):
        late = datetime(2026, 9, 10, 0, 30, tzinfo=timezone.utc)   # 19:30 on the 9th
        self.assertEqual(self.mc.project_date(late), date(2026, 9, 9))
        self.assertEqual(self.mc.exchange_date(late), date(2026, 9, 9))
        self.assertEqual(self.mc.project_month(late), "2026-09")

    def test_the_month_does_not_roll_until_it_rolls_in_chicago(self):
        self.assertEqual(
            self.mc.project_month(datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc)),
            "2026-09")
        self.assertEqual(
            self.mc.project_month(datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)),
            "2026-10")

    def test_days_remaining_uses_the_project_clock(self):
        from src.guardrails import days_remaining_in_month

        late = datetime(2026, 9, 10, 0, 30, tzinfo=timezone.utc)   # still the 9th
        self.assertEqual(days_remaining_in_month(late), 30 - 9 + 1)

    def test_sessions_remaining_uses_the_exchange_clock(self):
        from src.guardrails import tradable_sessions_remaining

        late = datetime(2026, 9, 10, 0, 30, tzinfo=timezone.utc)   # 20:30 ET on the 9th
        counted_from_the_ninth = tradable_sessions_remaining(late)
        noon_on_the_ninth = datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(counted_from_the_ninth,
                         tradable_sessions_remaining(noon_on_the_ninth))

    def test_the_two_clocks_can_legitimately_disagree(self):
        """23:30 ET on the 30th is the 30th in NY and the 30th in Chicago;
        03:30Z the next day is the 1st in UTC. Neither market rolled."""
        moment = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)
        self.assertEqual(self.mc.exchange_date(moment), date(2026, 9, 30))
        self.assertEqual(self.mc.project_date(moment), date(2026, 9, 30))
        self.assertEqual(moment.date(), date(2026, 10, 1))

    def test_a_naive_datetime_is_read_as_utc(self):
        naive = datetime(2026, 9, 10, 0, 30)
        self.assertEqual(self.mc.project_date(naive), date(2026, 9, 9))

    def test_current_month_agrees_with_the_project_clock(self):
        from src.state import current_month

        for moment in (datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc),
                       datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)):
            with self.subTest(moment=moment):
                self.assertEqual(current_month(moment), self.mc.project_month(moment))
