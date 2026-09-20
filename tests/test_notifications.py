"""When to interrupt a human, and — mostly — when not to.

The failure mode this module exists to prevent is not a missing notification.
It is a *daily* notification, which becomes weather within a week and takes the
one that mattered down with it. So most of these tests assert silence.

The suppression key is a fingerprint of what is being *said*. Two funding
reminders naming the same shortfall are the same notification however far apart
they were generated; one naming a different shortfall is new, because the
owner's decision changes.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.capital_gate import evaluate_gate  # noqa: E402
from src.notifications import (  # noqa: E402
    NOTIFICATION_KINDS,
    NOTIFY_ACTIONABLE_BUY,
    NOTIFY_FUNDING_REQUIRED,
    NOTIFY_MONTH_END_FUNDING,
    NOTIFY_PROMOTION_FAILED,
    NOTIFY_RECONCILIATION_FAILED,
    REMINDER_HOURS,
    NotificationError,
    actionable_buy,
    failure,
    funding_required,
    load_state,
    month_end_funding,
    record_sent,
    save_state,
    should_send,
)

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)


def leg(ticker="SNDK", amount="10.00"):
    return {"ticker": ticker, "proposed_amount_usd": amount}


def fresh_snapshot(cash="0.00"):
    """A snapshot the gate will believe: dated, and recent."""
    taken = datetime.now(timezone.utc) - timedelta(seconds=30)
    return {"as_of": taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cash_usd": cash, "unsettled_funds_usd": "0.00"}


class SilenceTests(unittest.TestCase):
    """The default is to say nothing."""

    def test_a_wait_produces_no_notification(self):
        self.assertIsNone(actionable_buy("WAIT", [], "25.00", "MEDIUM"))

    def test_a_buy_with_no_legs_produces_nothing(self):
        self.assertIsNone(actionable_buy("SINGLE_BUY", [], "25.00", "MEDIUM"))

    def test_an_exhausted_month_produces_no_funding_notification(self):
        gate = evaluate_gate("2026-09", "0.00", fresh_snapshot())
        self.assertIsNone(funding_required(gate))

    def test_an_evaluating_gate_produces_no_funding_notification(self):
        gate = evaluate_gate("2026-09", "25.00", fresh_snapshot("25.00"))
        self.assertIsNone(funding_required(gate))

    def test_a_funded_account_gets_no_month_end_reminder(self):
        self.assertIsNone(month_end_funding("2026-09", "25.00", "25.00"))


class ActionableBuyTests(unittest.TestCase):
    def test_a_single_buy_carries_everything_the_owner_needs(self):
        note = actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM")
        self.assertEqual(note.kind, NOTIFY_ACTIONABLE_BUY)
        self.assertIn("SNDK", note.title)
        self.assertIn("$10.00", note.title)
        self.assertIn("MEDIUM", note.body)
        self.assertIn("$25.00", note.body)
        self.assertIn("HUMAN APPROVAL REQUIRED", note.body)
        self.assertTrue(note.urgent)

    def test_a_split_plan_names_every_leg(self):
        note = actionable_buy(
            "SPLIT_BUY_PLAN", [leg(), leg("BTC-USD", "5.00")], "25.00", "HIGH")
        self.assertIn("SNDK", note.title)
        self.assertIn("BTC-USD", note.title)
        self.assertEqual(note.details["total_usd"], "15.00")
        self.assertEqual(len(note.details["legs"]), 2)

    def test_it_never_claims_approval_is_unnecessary(self):
        note = actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM")
        lowered = note.body.lower()
        for phrase in ("pre-approved", "already approved", "no approval",
                       "was submitted", "executed"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, lowered)

    def test_claiming_no_approval_needed_is_refused_outright(self):
        with self.assertRaises(NotificationError):
            actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM",
                           approval_required=False)

    def test_a_malformed_leg_still_notifies(self):
        """Better a notification with a bad number than silence."""
        note = actionable_buy("SINGLE_BUY", [{"ticker": "SNDK",
                                              "proposed_amount_usd": "???"}],
                              "25.00", "LOW")
        self.assertIsNotNone(note)


class FundingTests(unittest.TestCase):
    def test_a_shortfall_names_the_deposit(self):
        gate = evaluate_gate("2026-09", "25.00", fresh_snapshot())
        note = funding_required(gate)
        self.assertEqual(note.kind, NOTIFY_FUNDING_REQUIRED)
        self.assertIn("$25.00", note.body)
        self.assertIn("cash-only", note.body)

    def test_month_end_reminder_uses_the_configured_budget(self):
        note = month_end_funding("2026-09", "50.00", "10.00")
        self.assertIn("$50.00", note.body)
        self.assertIn("$40.00", note.body)
        self.assertIn("does not change the monthly budget", note.body)

    def test_no_dollar_amount_is_hard_coded(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "src", "notifications.py")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertNotIn("25.00", body)


class SuppressionTests(unittest.TestCase):
    def setUp(self):
        self.sent = {}

    def send(self, note, now):
        if should_send(note, self.sent, now=now):
            self.sent = record_sent(note, self.sent, now=now)
            return True
        return False

    def gate_note(self, cash="0.00"):
        gate = evaluate_gate("2026-09", "25.00", fresh_snapshot(cash))
        return funding_required(gate)

    def test_the_first_one_always_sends(self):
        self.assertTrue(self.send(self.gate_note(), NOW))

    def test_an_identical_condition_is_suppressed(self):
        self.send(self.gate_note(), NOW)
        self.assertFalse(self.send(self.gate_note(), NOW + timedelta(hours=24)))

    def test_it_repeats_after_the_reminder_interval(self):
        self.send(self.gate_note(), NOW)
        later = NOW + timedelta(hours=REMINDER_HOURS[NOTIFY_FUNDING_REQUIRED] + 1)
        self.assertTrue(self.send(self.gate_note(), later))

    def test_a_materially_changed_condition_sends_immediately(self):
        self.send(self.gate_note(cash="0.00"), NOW)
        # still unfunded, but the shortfall changed, so the deposit the owner
        # needs to make changed, so it is news again
        self.assertTrue(self.send(self.gate_note(cash="0.50"),
                                  NOW + timedelta(minutes=5)))

    def test_a_different_recommendation_is_always_news(self):
        first = actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM")
        second = actionable_buy("SINGLE_BUY", [leg("MU", "10.00")], "25.00", "MEDIUM")
        self.assertTrue(self.send(first, NOW))
        self.assertTrue(self.send(second, NOW + timedelta(minutes=1)))

    def test_the_same_recommendation_twice_in_a_day_is_suppressed(self):
        note = actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM")
        self.assertTrue(self.send(note, NOW))
        self.assertFalse(self.send(
            actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM"),
            NOW + timedelta(hours=2)))

    def test_kinds_are_suppressed_independently(self):
        self.send(self.gate_note(), NOW)
        buy = actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM")
        self.assertTrue(self.send(buy, NOW + timedelta(minutes=1)))

    def test_reconciliation_failures_repeat_soonest(self):
        self.assertLess(REMINDER_HOURS[NOTIFY_RECONCILIATION_FAILED],
                        REMINDER_HOURS[NOTIFY_FUNDING_REQUIRED])

    def test_every_kind_has_a_reminder_interval(self):
        for kind in NOTIFICATION_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, REMINDER_HOURS)


class FailureNotificationTests(unittest.TestCase):
    def test_each_failure_kind_builds(self):
        for kind in (NOTIFY_PROMOTION_FAILED, NOTIFY_RECONCILIATION_FAILED):
            with self.subTest(kind=kind):
                note = failure(kind, "it broke", "here is why")
                self.assertEqual(note.kind, kind)
                self.assertIn("here is why", note.body)

    def test_reconciliation_failure_is_urgent(self):
        self.assertTrue(failure(NOTIFY_RECONCILIATION_FAILED, "x").urgent)

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(NotificationError):
            failure("MADE_UP_KIND", "x")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "notifications.json")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_missing_ledger_is_empty_not_an_error(self):
        self.assertEqual(load_state(self.path), {})

    def test_it_round_trips(self):
        note = actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM")
        save_state(record_sent(note, {}, now=NOW), self.path)
        loaded = load_state(self.path)
        self.assertEqual(loaded[NOTIFY_ACTIONABLE_BUY]["fingerprint"], note.fingerprint)

    def test_a_corrupt_ledger_raises_rather_than_silencing(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(NotificationError):
            load_state(self.path)

    def test_an_unparseable_timestamp_sends_rather_than_suppresses(self):
        note = actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM")
        sent = {NOTIFY_ACTIONABLE_BUY: {"fingerprint": note.fingerprint,
                                        "at": "not a timestamp"}}
        self.assertTrue(should_send(note, sent, now=NOW))

    def test_the_write_is_atomic(self):
        save_state({"x": {"fingerprint": "f", "at": "2026-09-11T00:00:00Z"}}, self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()


class MonthEndReminderTests(unittest.TestCase):
    """Exactly one reminder, near the end, for the *configured* budget.

    Three things have to hold: it fires only near the month's final tradable
    day, it uses whatever `monthly_budget_usd` says rather than a literal, and
    a second run on the following day stays quiet.
    """

    def setUp(self):
        import importlib.util

        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location(
            "_gate_cli", os.path.join(repo, "scripts", "check_capital_gate.py"))
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    # --- the date boundary -------------------------------------------------

    def test_it_fires_on_the_final_tradable_day(self):
        # 2026-09-30 is a Wednesday and September's last session.
        self.assertTrue(self.cli.near_month_end(
            datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc)))

    def test_it_fires_within_the_reminder_window(self):
        for day in (28, 29, 30):
            with self.subTest(day=day):
                self.assertTrue(self.cli.near_month_end(
                    datetime(2026, 9, day, 15, 0, tzinfo=timezone.utc)))

    def test_it_stays_quiet_earlier_in_the_month(self):
        for day in (1, 10, 20, 26):
            with self.subTest(day=day):
                self.assertFalse(self.cli.near_month_end(
                    datetime(2026, 9, day, 15, 0, tzinfo=timezone.utc)))

    def test_the_window_is_measured_on_the_project_clock(self):
        """03:00Z on the 1st is still the 30th in Chicago — still in window."""
        self.assertTrue(self.cli.near_month_end(
            datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)))

    def test_the_window_ends_once_the_month_rolls(self):
        self.assertFalse(self.cli.near_month_end(
            datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)))

    # --- the configured budget, never a literal ----------------------------

    def test_it_recommends_the_configured_budget(self):
        for budget, expected in (("25.00", "$25.00"), ("50.00", "$50.00"),
                                 ("100.00", "$100.00")):
            with self.subTest(budget=budget):
                note = month_end_funding("2026-09", budget, "0.00")
                self.assertIn(expected, note.body)

    def test_a_changed_budget_changes_the_shortfall(self):
        self.assertIn("$40.00", month_end_funding("2026-09", "50.00", "10.00").body)
        self.assertIn("$90.00", month_end_funding("2026-09", "100.00", "10.00").body)

    def test_it_never_proposes_changing_the_budget(self):
        note = month_end_funding("2026-09", "25.00", "0.00")
        self.assertIn("does not change the monthly budget", note.body)

    def test_an_already_funded_account_gets_no_reminder(self):
        self.assertIsNone(month_end_funding("2026-09", "25.00", "25.00"))
        self.assertIsNone(month_end_funding("2026-09", "25.00", "40.00"))

    def test_the_cli_reads_the_budget_from_config_not_a_literal(self):
        import inspect

        source = inspect.getsource(self.cli)
        self.assertIn("config.monthly_budget_usd", source)
        self.assertNotIn('"25.00"', source)

    # --- exactly one -------------------------------------------------------

    def test_a_second_run_the_next_day_is_suppressed(self):
        note = month_end_funding("2026-09", "25.00", "0.00")
        sent = record_sent(note, {}, now=datetime(2026, 9, 28, 15, 0,
                                                  tzinfo=timezone.utc))
        again = month_end_funding("2026-09", "25.00", "0.00")
        self.assertFalse(should_send(again, sent,
                                     now=datetime(2026, 9, 29, 15, 0,
                                                  tzinfo=timezone.utc)))

    def test_it_stays_suppressed_across_the_whole_window(self):
        note = month_end_funding("2026-09", "25.00", "0.00")
        sent = record_sent(note, {}, now=datetime(2026, 9, 28, 15, 0,
                                                  tzinfo=timezone.utc))
        for day in (29, 30):
            with self.subTest(day=day):
                self.assertFalse(should_send(
                    month_end_funding("2026-09", "25.00", "0.00"), sent,
                    now=datetime(2026, 9, day, 15, 0, tzinfo=timezone.utc)))

    def test_the_next_month_is_a_new_reminder(self):
        sent = record_sent(month_end_funding("2026-09", "25.00", "0.00"), {},
                           now=datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc))
        october = month_end_funding("2026-10", "25.00", "0.00")
        self.assertTrue(should_send(october, sent,
                                    now=datetime(2026, 10, 28, 15, 0,
                                                 tzinfo=timezone.utc)))

    def test_a_changed_budget_makes_it_news_again(self):
        sent = record_sent(month_end_funding("2026-09", "25.00", "0.00"), {},
                           now=datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc))
        raised = month_end_funding("2026-09", "50.00", "0.00")
        self.assertTrue(should_send(raised, sent,
                                    now=datetime(2026, 9, 29, 15, 0,
                                                 tzinfo=timezone.utc)))


class NotificationCoverageTests(unittest.TestCase):
    """Every event that should reach a human, and the one that should not."""

    def test_actionable_buy_notifies(self):
        self.assertIsNotNone(actionable_buy("SINGLE_BUY", [leg()], "25.00", "MEDIUM"))
        self.assertIsNotNone(
            actionable_buy("SPLIT_BUY_PLAN", [leg(), leg("BTC-USD", "5.00")],
                           "25.00", "HIGH"))

    def test_wait_stays_silent(self):
        self.assertIsNone(actionable_buy("WAIT", [], "25.00", "MEDIUM"))

    def test_promotion_failure_notifies(self):
        note = failure(NOTIFY_PROMOTION_FAILED, "could not promote", "why")
        self.assertEqual(note.kind, NOTIFY_PROMOTION_FAILED)

    def test_reconciliation_failure_notifies_and_is_urgent(self):
        note = failure(NOTIFY_RECONCILIATION_FAILED, "could not reconcile", "why")
        self.assertTrue(note.urgent)

    def test_proposal_invalidation_notifies(self):
        from src.notifications import NOTIFY_PROPOSAL_INVALIDATED

        note = failure(NOTIFY_PROPOSAL_INVALIDATED, "proposal no longer valid",
                       "the quote moved beyond tolerance")
        self.assertEqual(note.kind, NOTIFY_PROPOSAL_INVALIDATED)

    def test_current_month_funding_shortage_notifies(self):
        gate = evaluate_gate("2026-09", "25.00", fresh_snapshot("0.00"))
        self.assertIsNotNone(funding_required(gate))

    def test_every_declared_kind_is_reachable(self):
        """No kind is declared and then never constructible."""
        builders = {
            NOTIFY_ACTIONABLE_BUY: lambda: actionable_buy(
                "SINGLE_BUY", [leg()], "25.00", "MEDIUM"),
            NOTIFY_FUNDING_REQUIRED: lambda: funding_required(
                evaluate_gate("2026-09", "25.00", fresh_snapshot("0.00"))),
            NOTIFY_MONTH_END_FUNDING: lambda: month_end_funding(
                "2026-09", "25.00", "0.00"),
        }
        for kind in NOTIFICATION_KINDS:
            with self.subTest(kind=kind):
                note = builders[kind]() if kind in builders else failure(kind, "x")
                self.assertIsNotNone(note)
                self.assertEqual(note.kind, kind)
