"""Promotion — carrying a scheduled recommendation into the decision pipeline.

Promotion mints a **PROPOSED** decision and stops. The properties under test are
the ones that keep it from becoming anything more, and the re-checks that keep a
stale recommendation from being acted on:

* the execution switches must still be **closed** — their absence is a blocker,
  not a green light;
* a recommendation may not carry a ``decision_id`` or any approval field;
* the ACTION banner a human read must describe the payload being promoted;
* stale, superseded, or mismatched recommendations are refused;
* the settled-cash, quote-freshness, slippage and tradability re-checks come
  from ``src.execution`` rather than a second implementation.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution import (  # noqa: E402
    MAX_CRYPTO_QUOTE_AGE_SECONDS,
    MAX_CRYPTO_SLIPPAGE_PCT,
    MAX_EQUITY_QUOTE_AGE_SECONDS,
    MAX_EQUITY_SLIPPAGE_PCT,
    PreflightResult,
)
from src.promotion import (  # noqa: E402
    EXPECTED_AT_PROMOTION,
    FORBIDDEN_RECOMMENDATION_KEYS,
    MAX_RECOMMENDATION_AGE_HOURS,
    RECOMMENDATION_SCHEMA_VERSION,
    REQUIRED_GATE_CODES,
    PromotionError,
    build_promoted_decision,
    check_banner_agreement,
    check_freshness,
    classify_preflight,
    price_move_pct,
    quote_age_limit,
    slippage_tolerance,
    snapshot_coverage,
    snapshot_from_dict,
    validate_recommendation,
)
from src.reporting import parse_action_banner  # noqa: E402

NOW = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)


def leg(**over) -> dict:
    payload = {
        "decision": "BUY",
        "action": "buy",
        "side": "buy",
        "ticker": "SNDK",
        "asset_class": "EQUITY",
        "asset_type": "us_common_stock",
        "proposed_amount_usd": "10.00",
        "current_price_usd": "1809.00",
        "quote_timestamp": "2026-09-09T16:56:00Z",
    }
    payload.update(over)
    return payload


def recommendation(**over) -> dict:
    payload = {
        "schema_version": RECOMMENDATION_SCHEMA_VERSION,
        "generated_at": "2026-09-09T16:56:00Z",
        "source_report": "reports/2026-09-09_1156.md",
        "plan_type": "SINGLE_BUY",
        "legs": [leg()],
    }
    payload.update(over)
    return payload


def preflight_result(*codes) -> PreflightResult:
    """A PreflightResult carrying exactly these blocker codes."""
    return PreflightResult(
        ok=not codes,
        next_state="PROPOSED",
        blockers=[(code, "…") for code in codes],
        warnings=[],
        checks=["execution_switches"],
    )


CLOSED_SWITCHES = tuple(REQUIRED_GATE_CODES) + ("NOT_APPROVED",)


class BlockerPartitionTests(unittest.TestCase):
    """Promotion succeeds only when the closed switches are the sole obstacle."""

    def test_closed_switches_plus_no_approval_is_promotable(self):
        check = classify_preflight(preflight_result(*CLOSED_SWITCHES))
        self.assertTrue(check.ok, check.blockers)
        self.assertEqual(sorted(set(check.expected)), sorted(set(CLOSED_SWITCHES)))

    def test_every_expected_code_is_declared(self):
        for code in CLOSED_SWITCHES:
            with self.subTest(code=code):
                self.assertIn(code, EXPECTED_AT_PROMOTION)

    def test_a_missing_gate_code_means_execution_is_enabled_and_blocks(self):
        """The load-bearing inversion: absence of a blocker IS a blocker."""
        for missing in REQUIRED_GATE_CODES:
            with self.subTest(missing=missing):
                codes = [c for c in CLOSED_SWITCHES if c != missing]
                check = classify_preflight(preflight_result(*codes))
                self.assertFalse(check.ok)
                self.assertIn("EXECUTION_ENABLED", check.codes)
                self.assertTrue(
                    any(missing in m for _, m in check.blockers),
                    "the blocker does not name %s" % missing)

    def test_a_fully_clean_preflight_is_refused(self):
        """If nothing blocks, all three switches are open. Refuse loudly."""
        check = classify_preflight(preflight_result())
        self.assertFalse(check.ok)
        self.assertEqual(check.codes.count("EXECUTION_ENABLED"), 3)

    def test_any_other_blocker_is_fatal(self):
        for code in ("INSUFFICIENT_SETTLED_CASH", "PRICE_MOVED_BEYOND_TOLERANCE",
                     "STALE_QUOTE", "SECURITY_UNTRADABLE", "BROKER_UNREADABLE",
                     "EXCEEDS_RECONCILED_AUTHORIZATION", "DUPLICATE_DECISION_ID",
                     "MARGIN_RISK", "REVALIDATION_FAILED", "CASH_UNKNOWN",
                     "TRADABILITY_UNKNOWN", "BELOW_MIN_ORDER_SIZE",
                     "RECONCILIATION_REQUIRED", "MONTH_ROLLED_OVER"):
            with self.subTest(code=code):
                check = classify_preflight(preflight_result(*(CLOSED_SWITCHES + (code,))))
                self.assertFalse(check.ok)
                self.assertIn(code, check.codes)

    def test_a_self_approval_attempt_is_fatal(self):
        check = classify_preflight(
            preflight_result(*(CLOSED_SWITCHES + ("SELF_APPROVAL_ATTEMPTED",))))
        self.assertFalse(check.ok)

    def test_autonomous_mode_is_not_an_expected_code(self):
        """AUTONOMOUS is refused unconditionally; it is never 'expected'."""
        self.assertNotIn("AUTONOMOUS_NOT_IMPLEMENTED", EXPECTED_AT_PROMOTION)
        check = classify_preflight(
            preflight_result("AUTONOMOUS_NOT_IMPLEMENTED", "NOT_APPROVED"))
        self.assertFalse(check.ok)


class RecommendationPayloadTests(unittest.TestCase):
    def test_a_well_formed_recommendation_validates(self):
        self.assertEqual(validate_recommendation(recommendation()), [])

    def test_a_decision_id_is_refused(self):
        payload = recommendation(legs=[leg(decision_id="dec_abc")])
        problems = validate_recommendation(payload)
        self.assertTrue(any("decision_id" in p for p in problems))
        self.assertTrue(any("promotion mints" in p for p in problems))

    def test_every_forbidden_key_is_refused(self):
        for key in FORBIDDEN_RECOMMENDATION_KEYS:
            with self.subTest(key=key):
                payload = recommendation(legs=[leg(**{key: "x"})])
                problems = validate_recommendation(payload)
                self.assertTrue(any(key in p for p in problems), "%s allowed" % key)

    def test_a_wait_is_not_a_recommendation(self):
        problems = validate_recommendation(recommendation(plan_type="WAIT"))
        self.assertTrue(any("plan_type" in p for p in problems))

    def test_a_single_buy_must_carry_exactly_one_leg(self):
        payload = recommendation(legs=[leg(), leg(ticker="MU")])
        self.assertTrue(any("1 leg" in p or "SINGLE_BUY but 2" in p
                            for p in validate_recommendation(payload)))

    def test_a_plan_must_carry_two_to_five_legs(self):
        one = recommendation(plan_type="SPLIT_BUY_PLAN", legs=[leg()])
        self.assertTrue(validate_recommendation(one))
        six = recommendation(plan_type="SPLIT_BUY_PLAN",
                             legs=[leg(ticker="A%d" % i) for i in range(6)])
        self.assertTrue(validate_recommendation(six))
        two = recommendation(plan_type="SPLIT_BUY_PLAN",
                             legs=[leg(), leg(ticker="MU")])
        self.assertEqual(validate_recommendation(two), [])

    def test_a_non_buy_leg_is_refused(self):
        payload = recommendation(legs=[leg(decision="WAIT")])
        self.assertTrue(any("not a BUY" in p for p in validate_recommendation(payload)))

    def test_a_leg_that_declares_itself_with_action_and_side_is_a_BUY(self):
        """The 2026-09-17 payload: no `decision` key at all.

        `INVESTMENT_POLICY.md` §11 never lists `decision` among the fields a BUY
        must contain, and the scheduled prompt's Step 13 sends the run to that
        list. Requiring it here rejected a conforming payload and cost a real
        BUY its promotion.
        """
        payload = recommendation(legs=[leg(action="BUY", side="buy")])
        del payload["legs"][0]["decision"]
        self.assertEqual(validate_recommendation(payload), [])

    def test_a_leg_that_declares_itself_only_in_decision_is_still_a_BUY(self):
        payload = recommendation(legs=[leg()])
        for key in ("action", "side"):
            del payload["legs"][0][key]
        self.assertEqual(validate_recommendation(payload), [])

    def test_a_leg_that_says_nothing_at_all_is_refused(self):
        """Silence is not a BUY. An empty verb set must never pass by default."""
        payload = recommendation(legs=[leg()])
        for key in ("decision", "action", "side"):
            del payload["legs"][0][key]
        problems = validate_recommendation(payload)
        self.assertTrue(any("does not say what it is" in p for p in problems),
                        problems)

    def test_a_leg_whose_verbs_contradict_each_other_is_refused(self):
        """Disagreement is refused rather than resolved by picking a winner."""
        for over in ({"action": "sell"}, {"side": "sell"}, {"decision": "WAIT"}):
            with self.subTest(over=over):
                payload = recommendation(legs=[leg(**over)])
                self.assertTrue(
                    any("not a BUY" in p
                        for p in validate_recommendation(payload)),
                    over)

    def test_a_zero_or_negative_amount_is_refused(self):
        for amount in ("0.00", "-5.00"):
            with self.subTest(amount=amount):
                payload = recommendation(legs=[leg(proposed_amount_usd=amount)])
                self.assertTrue(validate_recommendation(payload))

    def test_a_missing_source_report_is_refused(self):
        payload = recommendation()
        del payload["source_report"]
        self.assertTrue(any("source_report" in p
                            for p in validate_recommendation(payload)))

    def test_a_wrong_schema_version_is_refused(self):
        self.assertTrue(validate_recommendation(recommendation(schema_version=99)))


class FreshnessTests(unittest.TestCase):
    def test_a_recent_recommendation_for_the_newest_report_passes(self):
        check = check_freshness(
            recommendation(), "2026-09-09_1156.md", "2026-09-09_1156.md", NOW)
        self.assertTrue(check.ok, check.blockers)

    def test_a_stale_recommendation_is_refused(self):
        old = recommendation(generated_at="2026-09-06T16:56:00Z")
        check = check_freshness(old, "2026-09-09_1156.md", "2026-09-09_1156.md", NOW)
        self.assertFalse(check.ok)
        self.assertIn("RECOMMENDATION_STALE", check.codes)

    def test_the_staleness_limit_matches_the_approval_ttl(self):
        from src.approval import MAX_APPROVAL_TTL_HOURS

        self.assertEqual(MAX_RECOMMENDATION_AGE_HOURS, MAX_APPROVAL_TTL_HOURS)

    def test_an_undated_recommendation_is_refused(self):
        payload = recommendation(generated_at="not a timestamp")
        check = check_freshness(payload, "2026-09-09_1156.md", "2026-09-09_1156.md", NOW)
        self.assertIn("RECOMMENDATION_UNDATED", check.codes)

    def test_a_future_stamped_recommendation_is_refused(self):
        ahead = (NOW + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        check = check_freshness(
            recommendation(generated_at=ahead), "2026-09-09_1156.md",
            "2026-09-09_1156.md", NOW)
        self.assertIn("RECOMMENDATION_IN_THE_FUTURE", check.codes)

    def test_a_superseded_recommendation_is_refused(self):
        """A newer evaluation has run since; do not promote the older one."""
        check = check_freshness(
            recommendation(), "2026-09-10_1030.md", "2026-09-09_1156.md", NOW)
        self.assertFalse(check.ok)
        self.assertIn("NOT_THE_NEWEST_REPORT", check.codes)

    def test_a_digest_pointing_elsewhere_is_refused(self):
        check = check_freshness(
            recommendation(), "2026-09-09_1156.md", "2026-09-08_1617.md", NOW)
        self.assertFalse(check.ok)
        self.assertIn("DIGEST_RECOMMENDATION_MISMATCH", check.codes)

    def test_the_boundary_is_inclusive_of_the_limit(self):
        edge = (NOW - timedelta(hours=MAX_RECOMMENDATION_AGE_HOURS)).isoformat()
        check = check_freshness(
            recommendation(generated_at=edge.replace("+00:00", "Z")),
            "2026-09-09_1156.md", "2026-09-09_1156.md", NOW)
        self.assertNotIn("RECOMMENDATION_STALE", check.codes)


class BannerAgreementTests(unittest.TestCase):
    """The headline a human read must describe what is being promoted."""

    def banner(self, line):
        return parse_action_banner(line)

    def test_a_matching_banner_passes(self):
        check = check_banner_agreement(
            self.banner("ACTION: BUY $10.00 SNDK"), recommendation())
        self.assertTrue(check.ok, check.blockers)

    def test_amount_formatting_differences_are_tolerated(self):
        for line in ("ACTION: BUY $10 SNDK", "ACTION: BUY $10.0 SNDK",
                     "ACTION: BUY $10.00 SNDK"):
            with self.subTest(line=line):
                self.assertTrue(
                    check_banner_agreement(self.banner(line), recommendation()).ok)

    def test_a_different_amount_is_refused(self):
        check = check_banner_agreement(
            self.banner("ACTION: BUY $25.00 SNDK"), recommendation())
        self.assertFalse(check.ok)
        self.assertIn("BANNER_AMOUNT_MISMATCH", check.codes)

    def test_a_different_symbol_is_refused(self):
        check = check_banner_agreement(
            self.banner("ACTION: BUY $10.00 NVDA"), recommendation())
        self.assertFalse(check.ok)
        self.assertIn("BANNER_SYMBOL_MISMATCH", check.codes)

    def test_a_leg_count_mismatch_is_refused(self):
        check = check_banner_agreement(
            self.banner("ACTION: BUY $10.00 SNDK + $5.00 BTC-USD"), recommendation())
        self.assertFalse(check.ok)
        self.assertIn("BANNER_LEG_COUNT_MISMATCH", check.codes)

    def test_a_matching_two_leg_plan_passes(self):
        payload = recommendation(
            plan_type="SPLIT_BUY_PLAN",
            legs=[leg(), leg(ticker="BTC-USD", asset_class="CRYPTO",
                             asset_type="crypto", proposed_amount_usd="5.00")])
        check = check_banner_agreement(
            self.banner("ACTION: BUY $10.00 SNDK + $5.00 BTC-USD"), payload)
        self.assertTrue(check.ok, check.blockers)

    def test_a_wait_banner_has_nothing_to_promote(self):
        check = check_banner_agreement(
            self.banner("ACTION: NONE — WAIT"), recommendation())
        self.assertFalse(check.ok)
        self.assertIn("NO_BUY_BANNER", check.codes)

    def test_a_missing_banner_has_nothing_to_promote(self):
        check = check_banner_agreement(None, recommendation())
        self.assertFalse(check.ok)
        self.assertIn("NO_BUY_BANNER", check.codes)


class SnapshotTests(unittest.TestCase):
    def raw(self, **over):
        payload = {
            "as_of": "2026-09-09T17:58:00Z",
            "account_is_agentic": True,
            "account_masked": "••••0002",
            "equity_orders": [],
            "crypto_orders": [],
            "buying_power_usd": "25.00",
            "cash_usd": "25.00",
            "unsettled_funds_usd": "0.00",
            "account_type": "limited_margin",
            "quote_price_usd": "1815.00",
            "quote_timestamp": "2026-09-09T17:57:30Z",
            "tradable": True,
            "fractional_tradable": True,
            "account_type_tradable": True,
        }
        payload.update(over)
        return payload

    def test_a_full_snapshot_loads(self):
        snapshot = snapshot_from_dict(self.raw(), NOW)
        self.assertTrue(snapshot.account_is_agentic)
        self.assertEqual(snapshot.cash_usd, Decimal("25.00"))
        self.assertEqual(snapshot.account_type, "limited_margin")
        self.assertEqual(snapshot_coverage(snapshot, is_crypto=False), [])

    def test_settled_cash_is_carried_not_defaulted(self):
        """The gap that would silently skip the settled-cash re-check."""
        raw = self.raw()
        del raw["cash_usd"]
        snapshot = snapshot_from_dict(raw, NOW)
        self.assertIsNone(snapshot.cash_usd)
        self.assertTrue(any("cash_usd" in m
                            for m in snapshot_coverage(snapshot, is_crypto=False)))

    def test_coverage_names_what_is_missing_for_equity(self):
        snapshot = snapshot_from_dict(
            {"account_is_agentic": True, "account_masked": "x"}, NOW)
        missing = " ".join(snapshot_coverage(snapshot, is_crypto=False))
        for field in ("quote_price_usd", "cash_usd", "account_type",
                      "equity_orders", "fractional_tradable"):
            with self.subTest(field=field):
                self.assertIn(field, missing)

    def test_coverage_names_the_crypto_specific_fields(self):
        snapshot = snapshot_from_dict(
            {"account_is_agentic": True, "account_masked": "x"}, NOW)
        missing = " ".join(snapshot_coverage(snapshot, is_crypto=True))
        self.assertIn("crypto_pair_halted", missing)
        self.assertIn("crypto_min_order_size", missing)
        self.assertIn("crypto_orders", missing)

    def test_a_non_object_snapshot_raises(self):
        with self.assertRaises(PromotionError):
            snapshot_from_dict(["not", "an", "object"], NOW)

    def test_a_naive_timestamp_is_treated_as_utc(self):
        snapshot = snapshot_from_dict(
            self.raw(quote_timestamp="2026-09-09T17:57:30"), NOW)
        self.assertEqual(snapshot.quote_timestamp.tzinfo, timezone.utc)


class ThresholdReuseTests(unittest.TestCase):
    """Promotion must not invent its own tolerances."""

    def test_slippage_tolerances_come_from_execution(self):
        self.assertEqual(slippage_tolerance(False), MAX_EQUITY_SLIPPAGE_PCT)
        self.assertEqual(slippage_tolerance(True), MAX_CRYPTO_SLIPPAGE_PCT)

    def test_quote_age_limits_come_from_execution(self):
        self.assertEqual(quote_age_limit(False), MAX_EQUITY_QUOTE_AGE_SECONDS)
        self.assertEqual(quote_age_limit(True), MAX_CRYPTO_QUOTE_AGE_SECONDS)

    def test_the_price_move_is_signed_and_percentage(self):
        self.assertEqual(price_move_pct("100.00", "102.00"), Decimal("2.00"))
        self.assertEqual(price_move_pct("100.00", "97.50"), Decimal("-2.50"))

    def test_an_unusable_price_yields_no_move_rather_than_zero(self):
        for was, now_price in (("0.00", "10.00"), ("abc", "10.00"), (None, "10")):
            with self.subTest(was=was):
                self.assertIsNone(price_move_pct(was, now_price))

    def test_a_move_beyond_the_equity_tolerance_is_recognisable(self):
        move = price_move_pct("1809.00", "1900.00")
        self.assertGreater(abs(move), slippage_tolerance(False))


class PromotedDecisionTests(unittest.TestCase):
    def test_the_decision_id_is_added_and_provenance_recorded(self):
        decision = build_promoted_decision(
            leg(), "dec_abc123", "reports/2026-09-09_1156.md",
            "2026-09-09T16:56:00Z")
        self.assertEqual(decision["decision_id"], "dec_abc123")
        self.assertEqual(decision["promoted_from"]["source"], "SCHEDULED_EVALUATION")
        self.assertEqual(
            decision["promoted_from"]["source_report"], "reports/2026-09-09_1156.md")

    def test_nothing_the_fingerprint_binds_is_altered(self):
        from src.approval import BINDING_FIELDS

        source = leg()
        decision = build_promoted_decision(source, "dec_abc123", "r.md", "t")
        for field in BINDING_FIELDS:
            if field == "decision_id" or field not in source:
                continue
            with self.subTest(field=field):
                self.assertEqual(decision[field], source[field])

    def test_a_forbidden_key_is_stripped_rather_than_carried(self):
        source = leg(approved=True, execution_state="APPROVED")
        decision = build_promoted_decision(source, "dec_abc123", "r.md", "t")
        self.assertNotIn("approved", decision)
        self.assertNotIn("execution_state", decision)

    def test_the_source_leg_is_not_mutated(self):
        source = leg()
        build_promoted_decision(source, "dec_abc123", "r.md", "t")
        self.assertNotIn("decision_id", source)

    def test_a_promoted_decision_fingerprints(self):
        from src.approval import fingerprint

        decision = build_promoted_decision(leg(), "dec_abc123", "r.md", "t")
        first = fingerprint(decision)
        self.assertEqual(len(first), 64)
        self.assertEqual(first, fingerprint(dict(decision)))

    def test_a_leg_without_decision_is_promoted_into_a_valid_BUY(self):
        """The other half of the 2026-09-17 defect.

        Accepting the payload is not enough: the minted decision is handed
        straight to the guardrails, which require a canonical `decision` verb.
        Without canonicalisation here the run would clear validation and then
        fail as INVALID_DECISION instead.
        """
        from src import guardrails

        source = leg(action="BUY", side="buy")
        del source["decision"]
        decision = build_promoted_decision(
            source, "dec_abc123", "reports/2026-09-17_1030.md",
            "2026-09-17T15:35:00Z")
        proposal, violations = guardrails.normalize(decision)
        self.assertEqual(proposal.decision, "BUY")
        self.assertEqual(proposal.action, "buy")
        self.assertEqual(proposal.side, "buy")
        self.assertEqual([v.code for v in violations], [])

    def test_canonicalising_the_verbs_does_not_move_the_fingerprint(self):
        """Canonicalisation is not a rewrite of anything an approval binds."""
        from src.approval import fingerprint

        spelled_out = build_promoted_decision(leg(), "dec_abc123", "r.md", "t")
        source = leg(action="BUY", side="buy")
        del source["decision"]
        implied = build_promoted_decision(source, "dec_abc123", "r.md", "t")
        self.assertEqual(fingerprint(spelled_out), fingerprint(implied))

    def test_a_non_buy_leg_is_never_canonicalised_into_a_BUY(self):
        """Promotion must not launder a contradictory leg into a purchase."""
        decision = build_promoted_decision(
            leg(decision="WAIT"), "dec_abc123", "r.md", "t")
        self.assertEqual(decision["decision"], "WAIT")

    def test_changing_the_amount_changes_the_fingerprint(self):
        from src.approval import fingerprint

        a = build_promoted_decision(leg(), "dec_abc123", "r.md", "t")
        b = build_promoted_decision(
            leg(proposed_amount_usd="25.00"), "dec_abc123", "r.md", "t")
        self.assertNotEqual(fingerprint(a), fingerprint(b))


class ChallengePhraseTests(unittest.TestCase):
    """What promotion tells you to type must be what approval demands."""

    def test_the_phrase_is_shared_not_duplicated(self):
        from src.approval import challenge_phrase

        self.assertEqual(challenge_phrase("sndk", "10"), "APPROVE SNDK $10.00")
        self.assertEqual(challenge_phrase("BTC-USD", "5.00"), "APPROVE BTC-USD $5.00")

    def test_the_approval_script_uses_the_shared_helper(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "approve_decision.py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("challenge_phrase(ticker, amount)", body)
        self.assertNotIn('"APPROVE %s %s"', body)

    def test_the_promotion_script_shows_the_same_phrase(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "promote_latest_recommendation.py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("challenge_phrase(", body)


if __name__ == "__main__":
    unittest.main()


class RecorderPayloadTests(unittest.TestCase):
    """The seam between "a run recommended something" and "a human can act".

    Exercised against the real recorder function rather than its source text,
    because a BUY that quietly writes no payload looks like a successful run.
    """

    def setUp(self):
        import importlib.util

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "record_scheduled_run.py")
        spec = importlib.util.spec_from_file_location("_recorder", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.check = module.check_recommendation
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, payload) -> str:
        path = os.path.join(self.tmp, "rec.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        return path

    def test_a_buy_with_a_valid_payload_passes(self):
        problems = []
        self.check(self.write(recommendation()), "SINGLE_BUY", problems)
        self.assertEqual(problems, [])

    def test_a_buy_with_no_payload_fails_the_run(self):
        problems = []
        self.check(os.path.join(self.tmp, "missing.json"), "SINGLE_BUY", problems)
        self.assertTrue(any("wrote no recommendation payload" in p
                            for p in problems), problems)

    def test_a_plan_with_no_payload_fails_the_run(self):
        problems = []
        self.check(os.path.join(self.tmp, "missing.json"), "SPLIT_BUY_PLAN", problems)
        self.assertTrue(problems)

    def test_a_wait_that_writes_a_payload_fails_the_run(self):
        problems = []
        self.check(self.write(recommendation()), "WAIT", problems)
        self.assertTrue(any("Only a BUY writes one" in p for p in problems),
                        problems)

    def test_a_wait_with_no_payload_is_the_normal_case(self):
        problems = []
        self.check(os.path.join(self.tmp, "missing.json"), "WAIT", problems)
        self.assertEqual(problems, [])

    def test_an_unparseable_payload_fails_the_run(self):
        path = os.path.join(self.tmp, "rec.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        problems = []
        self.check(path, "SINGLE_BUY", problems)
        self.assertTrue(any("could not read" in p for p in problems), problems)

    def test_a_payload_carrying_a_decision_id_fails_the_run(self):
        problems = []
        self.check(self.write(recommendation(legs=[leg(decision_id="dec_x")])),
                   "SINGLE_BUY", problems)
        self.assertTrue(any("decision_id" in p for p in problems), problems)
