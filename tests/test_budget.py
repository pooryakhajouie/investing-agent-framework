"""Monthly budget arithmetic, rollover, and the no-carryover rule."""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.guardrails import validate  # noqa: E402
from src.state import BudgetError, commit_purchase, fresh_state  # noqa: E402
from tests.helpers import buy_decision, make_config, make_state  # noqa: E402


class CommitTests(unittest.TestCase):
    def test_commit_reduces_remaining(self):
        state = make_state()
        state = commit_purchase(state, "a", Decimal("7.50"))
        self.assertEqual(state.committed_usd, Decimal("7.50"))
        self.assertEqual(state.remaining_usd, Decimal("17.50"))

    def test_commit_to_exactly_the_budget_is_allowed(self):
        state = make_state()
        state = commit_purchase(state, "a", Decimal("24.99"))
        state = commit_purchase(state, "b", Decimal("0.01"))
        self.assertEqual(state.committed_usd, Decimal("25.00"))
        self.assertEqual(state.remaining_usd, Decimal("0.00"))

    def test_commit_one_cent_over_raises(self):
        state = make_state()
        state = commit_purchase(state, "a", Decimal("25.00"))
        with self.assertRaises(BudgetError):
            commit_purchase(state, "b", Decimal("0.01"))

    def test_commit_rejects_a_duplicate_decision_id(self):
        state = make_state()
        state = commit_purchase(state, "a", Decimal("1.00"))
        with self.assertRaises(BudgetError):
            commit_purchase(state, "a", Decimal("1.00"))

    def test_commit_rejects_non_positive_amounts(self):
        state = make_state()
        for amount in (Decimal("0.00"), Decimal("-1.00")):
            with self.subTest(amount=amount):
                with self.assertRaises(BudgetError):
                    commit_purchase(state, "x-%s" % amount, amount)

    def test_commit_rejects_sub_cent_amounts(self):
        state = make_state()
        with self.assertRaises(BudgetError):
            commit_purchase(state, "a", Decimal("0.001"))

    def test_commit_is_immutable(self):
        """commit_purchase returns a new state and leaves the original alone."""
        original = make_state()
        updated = commit_purchase(original, "a", Decimal("5.00"))
        self.assertEqual(original.committed_usd, Decimal("0.00"))
        self.assertEqual(updated.committed_usd, Decimal("5.00"))
        self.assertIsNot(original.acted_decision_ids, updated.acted_decision_ids)


class MonthRolloverTests(unittest.TestCase):
    def test_a_new_month_is_authorized_at_exactly_the_configured_budget(self):
        config = make_config()
        september = make_state(config, month="2026-09", committed="25.00", acted=["sep-1"])
        october = fresh_state(config, month="2026-10", acted_decision_ids=september.acted_decision_ids)
        self.assertEqual(october.month, "2026-10")
        self.assertEqual(october.authorized_budget_usd, Decimal("25.00"))
        self.assertEqual(october.committed_usd, Decimal("0.00"))
        self.assertEqual(october.remaining_usd, Decimal("25.00"))

    def test_unused_budget_does_not_increase_the_next_month(self):
        config = make_config()
        quiet_month = make_state(config, month="2026-09", committed="0.00")
        self.assertEqual(quiet_month.remaining_usd, Decimal("25.00"))

        october = fresh_state(config, month="2026-10", acted_decision_ids=quiet_month.acted_decision_ids)
        self.assertEqual(
            october.authorized_budget_usd,
            Decimal("25.00"),
            "unused authorization must never accumulate",
        )
        self.assertEqual(october.remaining_usd, Decimal("25.00"))

        # And the guardrail agrees: $25.01 in the new month is still too much.
        result = validate(buy_decision(october, "25.01"), config, october)
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_MONTHLY_BUDGET", result.violation_codes)

    def test_three_quiet_months_do_not_compound(self):
        config = make_config()
        state = make_state(config, month="2026-06")
        for month in ("2026-07", "2026-08", "2026-09"):
            state = fresh_state(config, month=month, acted_decision_ids=state.acted_decision_ids)
        self.assertEqual(state.authorized_budget_usd, Decimal("25.00"))
        result = validate(buy_decision(state, "50.00"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_MONTHLY_BUDGET", result.violation_codes)

    def test_acted_decision_ids_survive_rollover(self):
        """A decision may never be replayed, even in a later month."""
        config = make_config()
        september = make_state(config, month="2026-09", committed="10.00", acted=["sep-1"])
        october = fresh_state(config, month="2026-10", acted_decision_ids=september.acted_decision_ids)
        self.assertIn("sep-1", october.acted_decision_ids)

        result = validate(buy_decision(october, "10.00", decision_id="sep-1"), config, october)
        self.assertFalse(result.valid)
        self.assertIn("DUPLICATE_DECISION_ID", result.violation_codes)

    def test_a_new_month_restores_spending_capacity(self):
        config = make_config()
        exhausted = make_state(config, month="2026-09", committed="25.00", acted=["sep-1"])
        self.assertEqual(exhausted.remaining_usd, Decimal("0.00"))
        result = validate(buy_decision(exhausted, "5.00", decision_id="sep-2"), config, exhausted)
        self.assertFalse(result.valid)

        october = fresh_state(config, month="2026-10", acted_decision_ids=exhausted.acted_decision_ids)
        result = validate(buy_decision(october, "5.00", decision_id="oct-1"), config, october)
        self.assertTrue(result.valid, result.violation_codes)


class BudgetIsCanonicalTests(unittest.TestCase):
    def test_the_limit_follows_config_not_a_hard_coded_number(self):
        """Changing the one canonical source changes every downstream limit."""
        config = make_config(budget="10.00")
        state = make_state(config)
        self.assertEqual(state.authorized_budget_usd, Decimal("10.00"))

        ok = validate(buy_decision(state, "10.00"), config, state)
        self.assertTrue(ok.valid, ok.violation_codes)

        too_much = validate(buy_decision(state, "10.01"), config, state)
        self.assertFalse(too_much.valid)
        self.assertIn("EXCEEDS_MONTHLY_BUDGET", too_much.violation_codes)

    def test_no_module_hard_codes_the_dollar_budget(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src_dir = os.path.join(repo, "src")
        for filename in sorted(os.listdir(src_dir)):
            if not filename.endswith(".py"):
                continue
            with open(os.path.join(src_dir, filename), "r", encoding="utf-8") as handle:
                content = handle.read()
            with self.subTest(module=filename):
                self.assertNotIn('Decimal("25', content)
                self.assertNotIn("Decimal('25", content)


if __name__ == "__main__":
    unittest.main()
