"""Stage 7 — multi-buy allocation plans.

The load-bearing property under test: **a plan can never spend more than the
month's single $25 authorization**, no matter how it is sliced, and every leg
remains separately approvable, fingerprintable and idempotent.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.allocation import (  # noqa: E402
    AllocationError,
    CombinedExposure,
    assert_plan_within_authorization,
    combined_exposure,
    cumulative_state,
    sibling_reservations_usd,
    validate_plan,
)
from src.approval import ApprovalRecord  # noqa: E402
from src.execution import (  # noqa: E402
    ALL_STATES,
    IN_FLIGHT_STATES,
    RELEASED_STATES,
    ExecutionState,
)
from src.models import ZERO  # noqa: E402
from src.reconciliation import Reconciliation  # noqa: E402
from tests.helpers import (  # noqa: E402
    allocation_rationale,
    buy_decision,
    crypto_decision,
    make_config,
    make_crypto_universe,
    make_state,
    single_buy_plan,
    split_plan,
    wait_plan,
)


class PlanShapeTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)
        self.universe = make_crypto_universe()

    def check(self, plan, state=None):
        return validate_plan(
            plan, self.config, state or self.state, crypto_universe=self.universe
        )

    def test_wait_plan_is_valid_and_spends_nothing(self):
        result = self.check(wait_plan(self.state))
        self.assertTrue(result.valid, result.violation_codes)
        self.assertEqual(result.total_amount_usd, ZERO)
        self.assertEqual(result.remaining_after_usd, Decimal("25.00"))
        self.assertEqual(result.leg_count, 0)

    def test_wait_plan_may_not_carry_legs(self):
        plan = wait_plan(self.state)
        plan["legs"] = [buy_decision(self.state, "10.00")]
        result = self.check(plan)
        self.assertIn("WAIT_PLAN_WITH_LEGS", result.violation_codes)

    def test_wait_plan_still_enforces_the_wait_rules(self):
        plan = wait_plan(self.state)
        del plan["decision"]["best_new_crypto_candidate"]
        result = self.check(plan)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_CAPITAL_USE_COMPARISON", result.violation_codes)

    def test_single_buy_plan_is_valid(self):
        result = self.check(single_buy_plan(self.state, "25.00"))
        self.assertTrue(result.valid, result.violation_codes)
        self.assertEqual(result.leg_count, 1)
        self.assertEqual(result.total_amount_usd, Decimal("25.00"))
        self.assertEqual(result.remaining_after_usd, ZERO)

    def test_split_plan_is_valid(self):
        result = self.check(split_plan(self.state, ["15.00", "10.00"]))
        self.assertTrue(result.valid, result.violation_codes)
        self.assertEqual(result.leg_count, 2)
        self.assertEqual(result.total_amount_usd, Decimal("25.00"))

    def test_unknown_plan_type_is_rejected(self):
        plan = split_plan(self.state)
        plan["plan_type"] = "DOUBLE_DOWN"
        result = self.check(plan)
        self.assertIn("INVALID_PLAN_TYPE", result.violation_codes)

    def test_plan_id_is_required(self):
        plan = split_plan(self.state)
        plan["plan_id"] = ""
        result = self.check(plan)
        self.assertIn("MISSING_PLAN_ID", result.violation_codes)

    def test_single_buy_plan_may_not_carry_two_legs(self):
        plan = split_plan(self.state, ["10.00", "10.00"])
        plan["plan_type"] = "SINGLE_BUY"
        result = self.check(plan)
        self.assertIn("INVALID_LEG_COUNT", result.violation_codes)

    def test_split_plan_requires_at_least_two_legs(self):
        plan = split_plan(self.state, ["10.00"])
        result = self.check(plan)
        self.assertIn("INVALID_LEG_COUNT", result.violation_codes)

    def test_split_plan_refuses_more_than_five_legs(self):
        plan = split_plan(self.state, ["4.00"] * 6)
        result = self.check(plan)
        self.assertIn("INVALID_LEG_COUNT", result.violation_codes)

    def test_five_legs_are_allowed(self):
        plan = split_plan(self.state, ["5.00"] * 5)
        result = self.check(plan)
        self.assertNotIn("INVALID_LEG_COUNT", result.violation_codes)
        self.assertEqual(result.leg_count, 5)

    def test_plan_may_not_approve_itself(self):
        for field in ("approved", "approved_by", "user_approved", "execution_state"):
            with self.subTest(field=field):
                plan = split_plan(self.state)
                plan[field] = True
                result = self.check(plan)
                self.assertIn("SELF_APPROVAL_ATTEMPTED", result.violation_codes)

    def test_a_plan_is_never_executable(self):
        for plan in (wait_plan(self.state), single_buy_plan(self.state),
                     split_plan(self.state)):
            result = self.check(plan)
            self.assertFalse(result.executable)
            self.assertEqual(result.execution_status, "DRY_RUN_NOT_EXECUTED")


class MultiLegBudgetTests(unittest.TestCase):
    """The whole point of Stage 7: $25 stays $25 however it is divided."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)
        self.universe = make_crypto_universe()

    def check(self, plan, state=None):
        return validate_plan(
            plan, self.config, state or self.state, crypto_universe=self.universe
        )

    def test_combined_legs_may_not_exceed_the_authorization(self):
        result = self.check(split_plan(self.state, ["15.00", "15.00"]))
        self.assertFalse(result.valid)
        self.assertIn("PLAN_EXCEEDS_REMAINING_BUDGET", result.violation_codes)

    def test_three_legs_that_each_look_affordable_alone_are_caught_together(self):
        # Each $10 leg would pass against a full $25. Cumulatively they are $30.
        result = self.check(split_plan(self.state, ["10.00", "10.00", "10.00"]))
        self.assertFalse(result.valid)
        self.assertIn("PLAN_EXCEEDS_REMAINING_BUDGET", result.violation_codes)

    def test_legs_are_validated_against_a_cumulative_budget(self):
        result = self.check(split_plan(self.state, ["15.00", "10.00"]))
        self.assertEqual(result.legs[0].preceding_committed_usd, ZERO)
        self.assertEqual(result.legs[1].preceding_committed_usd, Decimal("15.00"))

    def test_a_later_leg_alone_can_break_the_budget(self):
        # $5 + $22 = $27. The first leg is fine; the second exceeds what is left.
        result = self.check(split_plan(self.state, ["5.00", "22.00"]))
        self.assertFalse(result.valid)
        self.assertTrue(result.legs[0].valid)
        self.assertFalse(result.legs[1].valid)

    def test_plan_respects_budget_already_committed_this_month(self):
        state = make_state(self.config, committed="20.00")
        result = self.check(split_plan(state, ["3.00", "3.00"]), state=state)
        self.assertFalse(result.valid)
        self.assertIn("PLAN_EXCEEDS_REMAINING_BUDGET", result.violation_codes)

    def test_plan_exactly_at_the_remaining_authorization_is_allowed(self):
        state = make_state(self.config, committed="10.00")
        plan = split_plan(state, ["9.00", "6.00"])
        result = self.check(plan, state=state)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertEqual(result.total_amount_usd, Decimal("15.00"))
        self.assertEqual(result.remaining_after_usd, ZERO)

    def test_remaining_after_is_reported_correctly(self):
        result = self.check(split_plan(self.state, ["12.00", "8.00"]))
        self.assertEqual(result.total_amount_usd, Decimal("20.00"))
        self.assertEqual(result.remaining_after_usd, Decimal("5.00"))

    def test_shared_authorization_spans_asset_classes(self):
        # One equity leg and one crypto leg draw on the same $25, not $25 each.
        plan = split_plan(self.state, ["20.00", "20.00"])
        self.assertEqual(plan["legs"][0]["asset_class"], "ETF")
        self.assertEqual(plan["legs"][1]["asset_class"], "CRYPTO")
        result = self.check(plan)
        self.assertIn("PLAN_EXCEEDS_REMAINING_BUDGET", result.violation_codes)

    def test_cumulative_state_helper_does_not_mutate_the_original(self):
        derived = cumulative_state(self.state, Decimal("10.00"))
        self.assertEqual(derived.committed_usd, Decimal("10.00"))
        self.assertEqual(self.state.committed_usd, ZERO)
        self.assertEqual(derived.remaining_usd, Decimal("15.00"))

    def test_malformed_leg_amount_is_rejected(self):
        plan = split_plan(self.state)
        plan["legs"][0]["proposed_amount_usd"] = "not-a-number"
        result = self.check(plan)
        self.assertFalse(result.valid)
        self.assertIn("MALFORMED_NUMBER", result.violation_codes)


class LegIdentityTests(unittest.TestCase):
    """Every leg must stay separately approvable and separately idempotent."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)
        self.universe = make_crypto_universe()

    def check(self, plan, state=None):
        return validate_plan(
            plan, self.config, state or self.state, crypto_universe=self.universe
        )

    def test_each_leg_keeps_its_own_decision_id(self):
        result = self.check(split_plan(self.state))
        ids = [leg.decision_id for leg in result.legs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ids))

    def test_duplicate_leg_decision_ids_are_rejected(self):
        plan = split_plan(self.state)
        plan["legs"][1]["decision_id"] = plan["legs"][0]["decision_id"]
        result = self.check(plan)
        self.assertIn("DUPLICATE_LEG_DECISION_ID", result.violation_codes)

    def test_missing_leg_decision_id_is_rejected(self):
        plan = split_plan(self.state)
        plan["legs"][1]["decision_id"] = ""
        result = self.check(plan)
        self.assertIn("MISSING_DECISION_ID", result.violation_codes)

    def test_a_replayed_decision_id_is_rejected(self):
        plan = split_plan(self.state)
        used = plan["legs"][0]["decision_id"]
        state = make_state(self.config, acted=[used])
        result = self.check(plan, state=state)
        self.assertIn("DUPLICATE_DECISION_ID", result.violation_codes)

    def test_two_legs_buying_the_same_asset_are_rejected(self):
        plan = split_plan(self.state, ["10.00", "10.00"])
        plan["legs"][1] = crypto_decision(self.state, "10.00")
        plan["legs"][1]["decision_id"] = "test-leg1-dup"
        plan["legs"][1]["allocation_rationale"] = allocation_rationale("VTI")
        plan["legs"][0] = crypto_decision(self.state, "10.00")
        plan["legs"][0]["decision_id"] = "test-leg0-dup"
        plan["legs"][0]["allocation_rationale"] = allocation_rationale("BTC-USD")
        result = self.check(plan)
        self.assertIn("DUPLICATE_LEG_ASSET", result.violation_codes)


class SplitReasoningTests(unittest.TestCase):
    """A split must argue for itself; scattering is not allocating."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)
        self.universe = make_crypto_universe()

    def check(self, plan, state=None):
        return validate_plan(
            plan, self.config, state or self.state, crypto_universe=self.universe
        )

    def test_each_leg_must_carry_an_allocation_rationale(self):
        plan = split_plan(self.state)
        del plan["legs"][1]["allocation_rationale"]
        result = self.check(plan)
        self.assertIn("MISSING_ALLOCATION_RATIONALE", result.violation_codes)

    def test_incomplete_allocation_rationale_is_rejected(self):
        plan = split_plan(self.state)
        plan["legs"][0]["allocation_rationale"]["why_this_amount"] = "   "
        result = self.check(plan)
        self.assertIn("MISSING_ALLOCATION_RATIONALE", result.violation_codes)

    def test_allocation_rationale_must_name_a_sibling_leg(self):
        plan = split_plan(self.state)
        plan["legs"][0]["allocation_rationale"]["legs_compared_against"] = (
            "Compared against the general opportunity set."
        )
        result = self.check(plan)
        self.assertIn("ALLOCATION_RATIONALE_NAMES_NO_SIBLING", result.violation_codes)

    def test_split_requires_a_plan_sprawl_assessment(self):
        plan = split_plan(self.state)
        del plan["plan_sprawl_assessment"]
        result = self.check(plan)
        self.assertIn("MISSING_PLAN_SPRAWL_ASSESSMENT", result.violation_codes)

    def test_incomplete_sprawl_assessment_is_rejected(self):
        plan = split_plan(self.state)
        plan["plan_sprawl_assessment"]["why_not_concentrate_into_one"] = ""
        result = self.check(plan)
        self.assertIn("MISSING_PLAN_SPRAWL_ASSESSMENT", result.violation_codes)

    def test_split_requires_a_substantive_split_rationale(self):
        plan = split_plan(self.state)
        plan["split_rationale"] = "Diversification."
        result = self.check(plan)
        self.assertIn("MISSING_SPLIT_RATIONALE", result.violation_codes)

    def test_single_buy_needs_no_split_reasoning(self):
        result = self.check(single_buy_plan(self.state, "20.00"))
        self.assertTrue(result.valid, result.violation_codes)
        self.assertNotIn("MISSING_SPLIT_RATIONALE", result.violation_codes)
        self.assertNotIn("MISSING_PLAN_SPRAWL_ASSESSMENT", result.violation_codes)


class CombinedExposureTests(unittest.TestCase):
    """executed + pending + approved-but-unsubmitted + proposed <= $25."""

    def make_reconciliation(self, filled="0.00", pending="0.00"):
        return Reconciliation(
            month="2026-09",
            authorized_usd=Decimal("25.00"),
            local_committed_usd=ZERO,
            broker_filled_usd=Decimal(filled),
            broker_pending_usd=Decimal(pending),
            reconciled_committed_usd=Decimal(filled) + Decimal(pending),
            safe_remaining_usd=Decimal("25.00") - Decimal(filled) - Decimal(pending),
            orders_considered=0,
        )

    def test_all_four_quantities_are_counted(self):
        exposure = combined_exposure(
            self.make_reconciliation("5.00", "4.00"),
            approved_unsubmitted_usd=Decimal("6.00"),
            proposed_usd=Decimal("10.00"),
        )
        self.assertEqual(exposure.committed_usd, Decimal("15.00"))
        self.assertEqual(exposure.total_usd, Decimal("25.00"))
        self.assertEqual(exposure.remaining_usd, Decimal("10.00"))
        self.assertTrue(exposure.within_authorization)

    def test_overage_is_detected_and_measured(self):
        exposure = combined_exposure(
            self.make_reconciliation("10.00", "5.00"),
            approved_unsubmitted_usd=Decimal("5.00"),
            proposed_usd=Decimal("10.00"),
        )
        self.assertEqual(exposure.total_usd, Decimal("30.00"))
        self.assertFalse(exposure.within_authorization)
        self.assertEqual(exposure.overage_usd, Decimal("5.00"))

    def test_assert_raises_on_overage(self):
        exposure = combined_exposure(
            self.make_reconciliation("20.00"), proposed_usd=Decimal("10.00")
        )
        with self.assertRaises(AllocationError) as ctx:
            assert_plan_within_authorization(exposure)
        self.assertIn("exceeds", str(ctx.exception))

    def test_assert_passes_exactly_at_the_limit(self):
        exposure = combined_exposure(
            self.make_reconciliation("10.00"),
            approved_unsubmitted_usd=Decimal("5.00"),
            proposed_usd=Decimal("10.00"),
        )
        assert_plan_within_authorization(exposure)  # must not raise

    def test_an_approved_sibling_alone_can_exhaust_the_month(self):
        exposure = combined_exposure(
            self.make_reconciliation(), approved_unsubmitted_usd=Decimal("25.00")
        )
        self.assertEqual(exposure.remaining_usd, ZERO)
        self.assertTrue(exposure.within_authorization)
        with self.assertRaises(AllocationError):
            assert_plan_within_authorization(
                combined_exposure(
                    self.make_reconciliation(),
                    approved_unsubmitted_usd=Decimal("25.00"),
                    proposed_usd=Decimal("0.01"),
                )
            )


class SiblingReservationTests(unittest.TestCase):
    """Approved-but-unsubmitted legs are invisible to the broker. Count them."""

    def approval(self, decision_id, amount, month="2026-09"):
        return ApprovalRecord(
            decision_id=decision_id,
            decision_fingerprint="f" * 64,
            policy_fingerprint="p" * 64,
            asset="VTI",
            asset_class="ETF",
            asset_type="us_etf",
            action="BUY",
            side="buy",
            max_amount_usd=Decimal(amount),
            month=month,
            approved_at="2026-09-05T12:00:00Z",
            expires_at="2026-09-06T12:00:00Z",
            approved_by="tester",
        )

    def test_approved_but_unsubmitted_sibling_is_reserved(self):
        approvals = {"leg-a": self.approval("leg-a", "10.00")}
        total = sibling_reservations_usd(approvals, {}, "2026-09")
        self.assertEqual(total, Decimal("10.00"))

    def test_the_leg_being_executed_is_excluded(self):
        approvals = {
            "leg-a": self.approval("leg-a", "10.00"),
            "leg-b": self.approval("leg-b", "8.00"),
        }
        total = sibling_reservations_usd(approvals, {}, "2026-09", exclude_decision_id="leg-a")
        self.assertEqual(total, Decimal("8.00"))

    def test_already_submitted_siblings_are_not_double_counted(self):
        approvals = {
            "leg-a": self.approval("leg-a", "10.00"),
            "leg-b": self.approval("leg-b", "8.00"),
        }
        states = {"leg-a": ExecutionState.SUBMITTED}
        total = sibling_reservations_usd(approvals, states, "2026-09")
        self.assertEqual(total, Decimal("8.00"))

    def test_filled_siblings_are_not_double_counted(self):
        approvals = {"leg-a": self.approval("leg-a", "10.00")}
        states = {"leg-a": ExecutionState.FILLED}
        self.assertEqual(sibling_reservations_usd(approvals, states, "2026-09"), ZERO)

    def test_terminal_siblings_release_their_reservation(self):
        approvals = {"leg-a": self.approval("leg-a", "10.00")}
        for state in (ExecutionState.REJECTED, ExecutionState.CANCELLED,
                      ExecutionState.EXPIRED):
            with self.subTest(state=state):
                self.assertEqual(
                    sibling_reservations_usd(approvals, {"leg-a": state}, "2026-09"), ZERO
                )

    def test_a_reevaluation_required_sibling_releases_its_reservation(self):
        """A decision whose priced premise is gone reserves nothing.

        Left in APPROVED, such a leg goes on holding dollars against a
        purchase that can never legally happen, and the month silently
        shrinks. Once closed out, the reservation must go.
        """
        approvals = {"leg-a": self.approval("leg-a", "10.00")}
        states = {"leg-a": ExecutionState.REEVALUATION_REQUIRED}
        self.assertEqual(sibling_reservations_usd(approvals, states, "2026-09"), ZERO)

    def test_a_reapproval_required_sibling_still_reserves(self):
        """The same decision, at the same price, can still become APPROVED."""
        approvals = {"leg-a": self.approval("leg-a", "10.00")}
        states = {"leg-a": ExecutionState.REAPPROVAL_REQUIRED}
        self.assertEqual(
            sibling_reservations_usd(approvals, states, "2026-09"), Decimal("10.00")
        )

    def test_every_released_state_releases_and_nothing_else_does(self):
        approvals = {"leg-a": self.approval("leg-a", "10.00")}
        for state in ALL_STATES:
            with self.subTest(state=state):
                reserved = sibling_reservations_usd(
                    approvals, {"leg-a": state}, "2026-09"
                )
                released = state in RELEASED_STATES or state in IN_FLIGHT_STATES
                self.assertEqual(reserved, ZERO if released else Decimal("10.00"))

    def test_a_split_plans_other_legs_still_reserve_normally(self):
        """Closing one leg out must not disturb its siblings."""
        approvals = {
            "leg-a": self.approval("leg-a", "10.00"),
            "leg-b": self.approval("leg-b", "8.00"),
            "leg-c": self.approval("leg-c", "5.00"),
        }
        states = {"leg-a": ExecutionState.REEVALUATION_REQUIRED}
        self.assertEqual(
            sibling_reservations_usd(approvals, states, "2026-09"), Decimal("13.00")
        )
        self.assertEqual(
            sibling_reservations_usd(
                approvals, states, "2026-09", exclude_decision_id="leg-b"
            ),
            Decimal("5.00"),
        )

    def test_other_months_are_ignored(self):
        approvals = {"leg-a": self.approval("leg-a", "10.00", month="2026-08")}
        self.assertEqual(sibling_reservations_usd(approvals, {}, "2026-09"), ZERO)

    def test_no_approvals_reserves_nothing(self):
        self.assertEqual(sibling_reservations_usd({}, {}, "2026-09"), ZERO)


if __name__ == "__main__":
    unittest.main()
