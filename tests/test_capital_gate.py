"""The gate in front of the expensive evaluation.

Two properties. It must **skip** on days when nothing could be bought whatever
the evaluation concluded, and it must tell the two skip reasons apart: an
exhausted authorization is a finished month, a funding shortfall is a request
addressed to the owner. Reporting either as the other is the failure mode.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.capital_gate import (  # noqa: E402
    GATE_AUTHORIZATION_EXHAUSTED,
    GATE_BROKER_UNREADABLE,
    GATE_EVALUATE,
    GATE_FUNDING_REQUIRED,
    MAX_SNAPSHOT_AGE_SECONDS,
    MIN_DEPLOYABLE_USD,
    SKIP_OUTCOMES,
    authorization_only_gate,
    evaluate_gate,
    refresh_problems,
    settled_cash_from_snapshot,
)


def snapshot(cash="25.00", unsettled="0.00", age_seconds=60, **over):
    """A broker snapshot, fresh by default.

    ``as_of`` is mandatory in practice: an undated or stale snapshot is refused,
    because a cash figure from yesterday is not a cash figure.
    """
    taken = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    payload = {"as_of": taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "cash_usd": cash, "unsettled_funds_usd": unsettled,
               "buying_power_usd": "50.00"}
    payload.update(over)
    return payload


class FundedAndAuthorizedTests(unittest.TestCase):
    def test_money_and_permission_means_evaluate(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot())
        self.assertEqual(gate.outcome, GATE_EVALUATE)
        self.assertTrue(gate.should_evaluate)
        self.assertEqual(gate.deployable_usd, Decimal("25.00"))

    def test_deployable_is_the_lesser_of_the_two(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(cash="10.00"))
        self.assertEqual(gate.outcome, GATE_EVALUATE)
        self.assertEqual(gate.deployable_usd, Decimal("10.00"))
        self.assertTrue(any("size against cash" in n for n in gate.notes))

    def test_the_broker_minimum_is_the_floor_not_zero(self):
        gate = evaluate_gate("2026-09", "0.50", snapshot())
        self.assertEqual(gate.outcome, GATE_AUTHORIZATION_EXHAUSTED)

    def test_exactly_the_minimum_still_evaluates(self):
        gate = evaluate_gate("2026-09", "1.00", snapshot(cash="1.00"))
        self.assertEqual(gate.outcome, GATE_EVALUATE)


class ExhaustedAuthorizationTests(unittest.TestCase):
    """A spent month is a success, and must be silent."""

    def test_zero_authorization_skips(self):
        gate = evaluate_gate("2026-09", "0.00", snapshot())
        self.assertEqual(gate.outcome, GATE_AUTHORIZATION_EXHAUSTED)
        self.assertIn(gate.outcome, SKIP_OUTCOMES)

    def test_it_is_a_quiet_outcome(self):
        gate = evaluate_gate("2026-09", "0.00", snapshot())
        self.assertTrue(gate.is_quiet, "a finished month must not notify")

    def test_it_says_so_without_calling_it_a_failure(self):
        gate = evaluate_gate("2026-09", "0.00", snapshot())
        self.assertIn("not a failure", gate.reason)

    def test_exhausted_does_not_depend_on_the_broker(self):
        """No snapshot needed: authorization alone settles it, and cheaply."""
        gate = evaluate_gate("2026-09", "0.00", snapshot=None)
        self.assertEqual(gate.outcome, GATE_AUTHORIZATION_EXHAUSTED)

    def test_a_new_month_reactivates_evaluation(self):
        spent = evaluate_gate("2026-09", "0.00", snapshot())
        fresh = evaluate_gate("2026-10", "25.00", snapshot())
        self.assertEqual(spent.outcome, GATE_AUTHORIZATION_EXHAUSTED)
        self.assertEqual(fresh.outcome, GATE_EVALUATE)


class FundingRequiredTests(unittest.TestCase):
    def test_authorization_without_cash_requires_funding(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(cash="0.00"))
        self.assertEqual(gate.outcome, GATE_FUNDING_REQUIRED)
        self.assertEqual(gate.shortfall_usd, Decimal("25.00"))

    def test_it_is_not_quiet(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(cash="0.00"))
        self.assertFalse(gate.is_quiet, "the owner is the only one who can fix this")

    def test_cash_below_the_broker_minimum_is_unfunded(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(cash="0.99"))
        self.assertEqual(gate.outcome, GATE_FUNDING_REQUIRED)

    def test_restored_funding_reactivates_evaluation(self):
        self.assertEqual(
            evaluate_gate("2026-09", "25.00", snapshot(cash="0.00")).outcome,
            GATE_FUNDING_REQUIRED)
        self.assertEqual(
            evaluate_gate("2026-09", "25.00", snapshot(cash="25.00")).outcome,
            GATE_EVALUATE)

    def test_the_message_names_the_deposit_needed(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(cash="0.40"))
        self.assertIn("$24.60", gate.reason)

    def test_partial_funding_above_the_minimum_still_evaluates(self):
        """$4 of a $25 authorization buys $4 of something. That is not a skip."""
        gate = evaluate_gate("2026-09", "25.00", snapshot(cash="4.00"))
        self.assertEqual(gate.outcome, GATE_EVALUATE)
        self.assertEqual(gate.deployable_usd, Decimal("4.00"))


class SettledCashTests(unittest.TestCase):
    """Buying power is never a substitute. This is the whole point."""

    def test_buying_power_is_not_used_as_a_fallback(self):
        gate = evaluate_gate(
            "2026-09", "25.00",
            {"buying_power_usd": "25.00"})      # no cash_usd at all
        self.assertEqual(gate.outcome, GATE_BROKER_UNREADABLE)

    def test_a_large_buying_power_cannot_rescue_zero_cash(self):
        gate = evaluate_gate(
            "2026-09", "25.00", snapshot(cash="0.00", buying_power_usd="1000.00"))
        self.assertEqual(gate.outcome, GATE_FUNDING_REQUIRED)

    def test_unsettled_funds_are_subtracted(self):
        self.assertEqual(
            settled_cash_from_snapshot(snapshot(cash="25.00", unsettled="10.00")),
            Decimal("15.00"))

    def test_fully_unsettled_cash_is_not_deployable(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(cash="25.00", unsettled="25.00"))
        self.assertEqual(gate.outcome, GATE_FUNDING_REQUIRED)

    def test_settled_cash_never_goes_negative(self):
        self.assertEqual(
            settled_cash_from_snapshot(snapshot(cash="5.00", unsettled="10.00")),
            Decimal("0.00"))

    def test_a_missing_snapshot_fails_closed(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot=None)
        self.assertEqual(gate.outcome, GATE_BROKER_UNREADABLE)
        self.assertFalse(gate.should_evaluate)

    def test_a_malformed_cash_value_fails_closed(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(cash="not a number"))
        self.assertEqual(gate.outcome, GATE_BROKER_UNREADABLE)


class ReservationTests(unittest.TestCase):
    def test_approved_siblings_reduce_deployable_capital(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(), reservations_usd="15.00")
        self.assertEqual(gate.remaining_authorization_usd, Decimal("10.00"))
        self.assertEqual(gate.deployable_usd, Decimal("10.00"))

    def test_reservations_can_exhaust_the_month(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(), reservations_usd="25.00")
        self.assertEqual(gate.outcome, GATE_AUTHORIZATION_EXHAUSTED)

    def test_reservations_are_reported(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(), reservations_usd="15.00")
        self.assertTrue(any("reserved" in n for n in gate.notes))


class SerializationTests(unittest.TestCase):
    def test_the_decision_round_trips_as_json(self):
        import json

        gate = evaluate_gate("2026-09", "25.00", snapshot(cash="0.40"))
        payload = json.loads(json.dumps(gate.to_dict()))
        self.assertEqual(payload["outcome"], GATE_FUNDING_REQUIRED)
        self.assertEqual(payload["shortfall_usd"], "24.60")

    def test_no_dollar_figure_is_hard_coded_in_the_module(self):
        """The budget lives in config.json and is passed in."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "capital_gate.py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertNotIn("25.00", body)
        self.assertEqual(MIN_DEPLOYABLE_USD, Decimal("1.00"))


if __name__ == "__main__":
    unittest.main()


class SnapshotFreshnessTests(unittest.TestCase):
    """A cash figure from yesterday is not a cash figure.

    The first version of this gate read a persisted ``broker.json`` that nothing
    regenerated. That fails in both directions and fails *silently*: expensive
    evaluations against an account emptied a week ago, or evaluation suppressed
    forever after a deposit the file never learned about.
    """

    def test_a_fresh_snapshot_is_believed(self):
        self.assertEqual(
            evaluate_gate("2026-09", "25.00", snapshot(age_seconds=60)).outcome,
            GATE_EVALUATE)

    def test_a_stale_snapshot_is_refused(self):
        gate = evaluate_gate(
            "2026-09", "25.00",
            snapshot(age_seconds=MAX_SNAPSHOT_AGE_SECONDS + 60))
        self.assertEqual(gate.outcome, GATE_BROKER_UNREADABLE)
        self.assertIn("not a cash figure", gate.reason)

    def test_yesterdays_snapshot_is_always_refused(self):
        gate = evaluate_gate("2026-09", "25.00", snapshot(age_seconds=26 * 3600))
        self.assertEqual(gate.outcome, GATE_BROKER_UNREADABLE)

    def test_an_undated_snapshot_is_refused(self):
        raw = snapshot()
        del raw["as_of"]
        self.assertEqual(
            evaluate_gate("2026-09", "25.00", raw).outcome, GATE_BROKER_UNREADABLE)

    def test_a_malformed_date_is_refused(self):
        self.assertEqual(
            evaluate_gate("2026-09", "25.00", snapshot(as_of="not a time")).outcome,
            GATE_BROKER_UNREADABLE)

    def test_staleness_cannot_be_mistaken_for_funding(self):
        """A stale snapshot must never be reported as an unfunded account."""
        gate = evaluate_gate("2026-09", "25.00",
                             snapshot(cash="0.00", age_seconds=99999))
        self.assertEqual(gate.outcome, GATE_BROKER_UNREADABLE)
        self.assertNotEqual(gate.outcome, GATE_FUNDING_REQUIRED)

    def test_the_exhausted_month_never_needs_a_snapshot(self):
        """Phase one is free: no broker data, no refresh, no Claude call."""
        gate = authorization_only_gate("2026-09", "0.00")
        self.assertEqual(gate.outcome, GATE_AUTHORIZATION_EXHAUSTED)

    def test_phase_one_defers_the_cash_question(self):
        gate = authorization_only_gate("2026-09", "25.00")
        self.assertEqual(gate.outcome, GATE_EVALUATE)
        self.assertIsNone(gate.settled_cash_usd)
        self.assertTrue(any("not checked" in n for n in gate.notes))


class RestoredFundingThroughTheRealRunnerPathTests(unittest.TestCase):
    """The end-to-end property: a deposit reactivates evaluation by itself.

    This drives the actual ``scripts/check_capital_gate.py`` process against a
    real snapshot file, which is the path the runner uses — not the pure
    function. What it proves is that the transition needs no human action and no
    expensive evaluation: the runner regenerates the snapshot every morning, so
    the next run simply sees the new balance.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "broker_snapshot.json")
        self.repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def write_snapshot(self, cash, age_seconds=30):
        taken = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"as_of": taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "account_is_agentic": True,
                       "cash_usd": cash, "unsettled_funds_usd": "0.00",
                       "buying_power_usd": cash}, handle)

    def run_gate(self, *extra):
        return subprocess.run(
            [sys.executable, os.path.join(self.repo, "scripts", "check_capital_gate.py"),
             "--snapshot", self.path] + list(extra),
            cwd=self.repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_empty_account_then_deposit_reactivates_evaluation(self):
        self.write_snapshot("0.00")
        self.assertEqual(self.run_gate().returncode, 11, "expected FUNDING_REQUIRED")

        # The owner deposits. The next morning's run regenerates the snapshot;
        # here that regeneration is simulated by rewriting the same file.
        self.write_snapshot("25.00")
        self.assertEqual(self.run_gate().returncode, 0, "expected EVALUATE")

    def test_a_snapshot_that_is_never_refreshed_stops_being_believed(self):
        """The bug this architecture change fixes, asserted directly."""
        self.write_snapshot("0.00", age_seconds=MAX_SNAPSHOT_AGE_SECONDS + 600)
        self.assertEqual(self.run_gate().returncode, 12, "expected BROKER_UNREADABLE")

    def test_a_missing_snapshot_fails_closed_through_the_real_script(self):
        self.assertEqual(self.run_gate().returncode, 12)

    def test_the_funding_notification_reaches_the_emit_path(self):
        self.write_snapshot("0.00")
        out = os.path.join(self.tmp, "note.json")
        self.assertEqual(self.run_gate("--emit-notification", out).returncode, 11)
        with open(out, encoding="utf-8") as handle:
            note = json.load(handle)
        self.assertEqual(note["kind"], "FUNDING_REQUIRED")

    def test_no_notification_is_written_when_funded(self):
        self.write_snapshot("25.00")
        out = os.path.join(self.tmp, "note.json")
        self.assertEqual(self.run_gate("--emit-notification", out).returncode, 0)
        self.assertFalse(os.path.exists(out), "a funded account is silent")


class RunnerWiringTests(unittest.TestCase):
    """The runner must regenerate the snapshot, not read a persisted one."""

    def setUp(self):
        self.repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(self.repo, "scripts", "scheduled_evaluation.sh"),
                  encoding="utf-8") as handle:
            self.runner = handle.read()

    def test_the_runner_refreshes_the_snapshot_every_run(self):
        self.assertIn("refresh_broker_snapshot.sh", self.runner)

    def test_the_runner_does_not_read_a_hand_written_broker_json(self):
        self.assertIn('GATE_SNAPSHOT="$REPO_ROOT/state/broker_snapshot.json"',
                      self.runner)
        self.assertNotIn('GATE_SNAPSHOT="$REPO_ROOT/broker.json"', self.runner)

    def test_phase_one_runs_before_any_refresh(self):
        """An exhausted month must cost nothing at all."""
        phase_one = self.runner.index("--authorization-only")
        refresh = self.runner.index("refresh_broker_snapshot.sh --out")
        self.assertLess(phase_one, refresh)

    def test_a_failed_refresh_removes_the_snapshot_rather_than_reusing_it(self):
        self.assertIn('rm -f "$GATE_SNAPSHOT"', self.runner)

    def test_the_refresh_grants_no_order_tool(self):
        with open(os.path.join(self.repo, "scripts", "refresh_broker_snapshot.sh"),
                  encoding="utf-8") as handle:
            body = handle.read()
        allowed = body[body.index("ALLOWED="):body.index("DISALLOWED=")]
        for tool in ("place_equity_order", "place_crypto_order", "review_",
                     "preview_", "cancel_"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, allowed)

    def test_the_refresh_grants_only_the_two_account_tools(self):
        with open(os.path.join(self.repo, "scripts", "refresh_broker_snapshot.sh"),
                  encoding="utf-8") as handle:
            body = handle.read()
        allowed = body[body.index("ALLOWED="):body.index("DISALLOWED=")]
        self.assertIn("get_accounts", allowed)
        self.assertIn("get_portfolio", allowed)
        for tool in ("get_equity_quotes", "WebSearch", "WebFetch", "get_equity_news"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, allowed)


class RefreshVerificationTests(unittest.TestCase):
    """A refresh succeeds only if THIS invocation produced the snapshot.

    The defect this guards: a subprocess exited 0 having written nothing, the
    previous file was still on disk, and the refresh script announced "snapshot
    refreshed" against a file 254,484 seconds — 2.9 days — old. Only the capital
    gate's independent freshness check stopped that reading being used.

    "Does a parseable snapshot exist" is the wrong question. "Did this
    invocation produce one" is the right one.
    """

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.started = time.time()

    def snap(self, age_seconds=30, **over):
        taken = self.now - timedelta(seconds=age_seconds)
        payload = {"as_of": taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "account_is_agentic": True,
                   "account_masked": "••••0002",
                   "cash_usd": "25.00",
                   "unsettled_funds_usd": "0.00"}
        payload.update(over)
        return payload

    # --- the four required cases ------------------------------------------

    def test_a_fresh_file_written_by_this_invocation_succeeds(self):
        self.assertEqual(
            refresh_problems(self.snap(), self.started + 2, self.started), [])

    def test_an_old_file_and_no_write_fails(self):
        """The subprocess wrote nothing; a file from an earlier run remains."""
        problems = refresh_problems(
            self.snap(age_seconds=254484), self.started - 3600, self.started)
        self.assertTrue(problems)
        self.assertTrue(any("predates this invocation" in p for p in problems))
        self.assertTrue(any("as_of is" in p and "over the" in p for p in problems))

    def test_exit_zero_with_an_unchanged_file_fails(self):
        """Exactly the observed failure: stale content, untouched mtime."""
        problems = refresh_problems(
            self.snap(age_seconds=254484), self.started - 1, self.started)
        self.assertTrue(any("predates this invocation" in p for p in problems))

    def test_a_malformed_or_undated_snapshot_fails(self):
        for label, payload in (
            ("undated", self.snap(as_of="")),
            ("bad date", self.snap(as_of="not a timestamp")),
            ("not an object", ["nope"]),
            ("missing as_of", {k: v for k, v in self.snap().items() if k != "as_of"}),
        ):
            with self.subTest(case=label):
                self.assertTrue(
                    refresh_problems(payload, self.started + 1, self.started))

    # --- the rest of the contract -----------------------------------------

    def test_no_file_at_all_fails(self):
        problems = refresh_problems(None, None, self.started)
        self.assertTrue(any("wrote nothing" in p for p in problems))

    def test_a_missing_cash_figure_fails(self):
        payload = self.snap()
        del payload["cash_usd"]
        self.assertTrue(refresh_problems(payload, self.started + 1, self.started))

    def test_buying_power_cannot_stand_in_for_cash(self):
        payload = self.snap()
        del payload["cash_usd"]
        payload["buying_power_usd"] = "25.00"
        self.assertTrue(refresh_problems(payload, self.started + 1, self.started))

    def test_a_non_agentic_account_fails(self):
        self.assertTrue(refresh_problems(
            self.snap(account_is_agentic=False), self.started + 1, self.started))

    def test_a_future_dated_snapshot_fails(self):
        self.assertTrue(refresh_problems(
            self.snap(age_seconds=-600), self.started + 1, self.started))

    def test_content_fresh_but_file_old_still_fails(self):
        """Both conditions are required, not either."""
        self.assertTrue(refresh_problems(
            self.snap(age_seconds=10), self.started - 10, self.started))

    def test_file_new_but_content_stale_still_fails(self):
        self.assertTrue(refresh_problems(
            self.snap(age_seconds=MAX_SNAPSHOT_AGE_SECONDS + 60),
            self.started + 1, self.started))


class RefreshScriptContractTests(unittest.TestCase):
    """The script must act on the verdict, not merely compute it."""

    def setUp(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "scripts", "refresh_broker_snapshot.sh"),
                  encoding="utf-8") as handle:
            self.body = handle.read()

    def test_the_start_time_is_recorded_before_the_subprocess(self):
        started = self.body.index("STARTED_AT=")
        claude = self.body.index('"$CLAUDE_BIN" -p "$PROMPT"')
        self.assertLess(started, claude)

    def test_a_failed_subprocess_clears_the_stale_snapshot(self):
        section = self.body[self.body.index('CLAUDE_EXIT" -ne 0'):]
        self.assertIn('rm -f "$OUT"', section[:400])

    def test_an_unusable_snapshot_is_removed(self):
        self.assertIn("os.remove(path)", self.body)
        self.assertIn("cannot be", self.body)

    def test_it_uses_the_shared_validator(self):
        self.assertIn("refresh_problems", self.body)

    def test_it_compares_against_the_prior_digest(self):
        self.assertIn("PRIOR_DIGEST", self.body)
        self.assertIn("byte-identical", self.body)
