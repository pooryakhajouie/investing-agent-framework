"""Closing out an approved-but-unsubmitted decision, and releasing its dollars.

An approval reserves part of the month's authorization from the moment it is
granted until the purchase is submitted, because neither the broker nor the
local ledger can see it. That is correct — and it stays correct only while the
purchase might still happen.

The failure this covers: a decision is approved, the live preflight then finds
the price has moved beyond tolerance, no ticket is minted and no order is ever
submitted — and nothing transitions the record anywhere. It sits in ``APPROVED``
reserving dollars against a purchase that can never legally happen, and the
month silently shrinks with nothing in the system saying so.

The properties under test:

* an approved, unsubmitted decision *does* reserve its amount — that part was
  always right and must not regress;
* closing it out is grounded, not automatic: time passing is not a ground;
* the closed decision reserves nothing, can never be submitted, and cannot be
  re-approved;
* the approval record and the audit trail survive;
* running the close twice is safe;
* a new decision for the same asset can spend the released dollars, but only
  with its own decision id and its own approval;
* the other legs of a split plan are untouched.

Every fixture here is synthetic: ticker ``EXMP``, ``dec_synthetic_*`` ids,
``apr_synthetic_*`` approvals, month ``2026-01``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.allocation import sibling_reservations_usd  # noqa: E402
from src.approval import ApprovalRecord, save_approvals  # noqa: E402
from src.execution import (  # noqa: E402
    CLOSURE_GROUND_ABANDONED,
    CLOSURE_GROUND_EXPIRED,
    CLOSURE_GROUND_PREFLIGHT,
    CLOSURE_GROUNDS,
    DECISION_INVALIDATING_CODES,
    RELEASED_STATES,
    TERMINAL_STATES,
    ExecutionState,
    InvalidTransition,
    assert_transition,
    plan_closure,
)
from src.execution_store import (  # noqa: E402
    EVENT_DECISION_CLOSED,
    ExecutionRecord,
    load_executions,
    read_audit,
    save_executions,
    transition,
)
from src.models import ZERO  # noqa: E402

MONTH = "2026-01"
DECISION_ID = "dec_synthetic_0001"
APPROVAL_ID = "apr_synthetic_0001"
TICKER = "EXMP"
AMOUNT = "10.00"


def synthetic_approval(decision_id=DECISION_ID, amount=AMOUNT,
                       approval_id=APPROVAL_ID):
    return ApprovalRecord(
        decision_id=decision_id,
        decision_fingerprint="f" * 64,
        policy_fingerprint="p" * 64,
        asset=TICKER,
        asset_class="EQUITY",
        asset_type="us_common_stock",
        action="buy",
        side="buy",
        max_amount_usd=Decimal(amount),
        month=MONTH,
        approved_at="2026-01-15T12:00:00Z",
        expires_at="2026-01-16T12:00:00Z",
        approved_by="tester",
        approval_id=approval_id,
    )


def synthetic_record(decision_id=DECISION_ID, state=ExecutionState.APPROVED):
    return ExecutionRecord(
        decision_id=decision_id,
        state=state,
        decision_fingerprint="f" * 64,
        approval_id=APPROVAL_ID,
        month=MONTH,
        asset=TICKER,
        amount_usd=AMOUNT,
    )


class ReservationBeforeClosureTests(unittest.TestCase):
    """The behaviour that is correct and must not regress."""

    def test_an_approved_unsubmitted_decision_reserves_its_amount(self):
        approvals = {DECISION_ID: synthetic_approval()}
        states = {DECISION_ID: ExecutionState.APPROVED}
        self.assertEqual(
            sibling_reservations_usd(approvals, states, MONTH), Decimal("10.00")
        )

    def test_the_reservation_reduces_what_the_month_can_deploy(self):
        approvals = {DECISION_ID: synthetic_approval()}
        reserved = sibling_reservations_usd(
            approvals, {DECISION_ID: ExecutionState.APPROVED}, MONTH
        )
        self.assertEqual(Decimal("25.00") - reserved, Decimal("15.00"))


class ClosureGroundingTests(unittest.TestCase):
    """A close is grounded in an invalidation, never in convenience."""

    def test_price_drift_grounds_a_closure(self):
        plan = plan_closure(
            ExecutionState.APPROVED,
            CLOSURE_GROUND_PREFLIGHT,
            codes=["PRICE_MOVED_BEYOND_TOLERANCE"],
        )
        self.assertTrue(plan.ok, plan.refusals)
        self.assertEqual(plan.target_state, ExecutionState.REEVALUATION_REQUIRED)
        self.assertIn("PRICE_MOVED_BEYOND_TOLERANCE", plan.reason)

    def test_a_stale_quote_grounds_a_closure(self):
        plan = plan_closure(
            ExecutionState.APPROVED, CLOSURE_GROUND_PREFLIGHT, codes=["STALE_QUOTE"]
        )
        self.assertTrue(plan.ok, plan.refusals)

    def test_an_expired_approval_grounds_a_closure(self):
        plan = plan_closure(
            ExecutionState.APPROVED, CLOSURE_GROUND_EXPIRED,
            codes=["POLICY_CHANGED", "APPROVAL_EXPIRED"],
        )
        self.assertTrue(plan.ok, plan.refusals)
        self.assertEqual(plan.codes, ("APPROVAL_EXPIRED",))

    def test_a_policy_change_alone_does_not_close_a_decision_out(self):
        """That has its own, narrower lifecycle: re-approval."""
        plan = plan_closure(
            ExecutionState.APPROVED, CLOSURE_GROUND_PREFLIGHT, codes=["POLICY_CHANGED"]
        )
        self.assertFalse(plan.ok)
        self.assertIn("UNGROUNDED_CLOSURE", plan.refusal_codes)

    def test_time_passing_alone_is_not_a_ground(self):
        plan = plan_closure(ExecutionState.APPROVED, CLOSURE_GROUND_EXPIRED, codes=[])
        self.assertFalse(plan.ok)
        self.assertIn("UNGROUNDED_CLOSURE", plan.refusal_codes)

    def test_a_passing_preflight_is_not_a_ground(self):
        plan = plan_closure(ExecutionState.APPROVED, CLOSURE_GROUND_PREFLIGHT, codes=[])
        self.assertFalse(plan.ok)
        self.assertIn("UNGROUNDED_CLOSURE", plan.refusal_codes)

    def test_human_abandonment_must_be_written_down(self):
        bare = plan_closure(ExecutionState.APPROVED, CLOSURE_GROUND_ABANDONED)
        self.assertFalse(bare.ok)
        self.assertIn("ABANDONMENT_UNEXPLAINED", bare.refusal_codes)

        explained = plan_closure(
            ExecutionState.APPROVED, CLOSURE_GROUND_ABANDONED,
            note="not buying %s at this price" % TICKER,
        )
        self.assertTrue(explained.ok, explained.refusals)
        self.assertIn("not buying %s" % TICKER, explained.reason)

    def test_an_invented_ground_is_refused(self):
        plan = plan_closure(ExecutionState.APPROVED, "BECAUSE_I_SAID_SO")
        self.assertFalse(plan.ok)
        self.assertIn("UNKNOWN_CLOSURE_GROUND", plan.refusal_codes)

    def test_every_ground_is_one_of_the_three(self):
        self.assertEqual(
            set(CLOSURE_GROUNDS),
            {CLOSURE_GROUND_PREFLIGHT, CLOSURE_GROUND_EXPIRED,
             CLOSURE_GROUND_ABANDONED},
        )

    def test_a_possibly_live_order_is_never_closed_out(self):
        for state in (ExecutionState.SUBMITTED, ExecutionState.SUBMISSION_UNCERTAIN,
                      ExecutionState.PARTIALLY_FILLED):
            with self.subTest(state=state):
                plan = plan_closure(
                    state, CLOSURE_GROUND_PREFLIGHT,
                    codes=["PRICE_MOVED_BEYOND_TOLERANCE"],
                )
                self.assertFalse(plan.ok)
                self.assertIn("ORDER_MAY_EXIST", plan.refusal_codes)

    def test_closure_is_idempotent(self):
        for state in sorted(RELEASED_STATES):
            with self.subTest(state=state):
                plan = plan_closure(
                    state, CLOSURE_GROUND_PREFLIGHT, codes=["STALE_QUOTE"]
                )
                self.assertTrue(plan.ok)
                self.assertTrue(plan.already_closed)
                self.assertEqual(plan.target_state, "")

    def test_a_reapproval_required_decision_can_still_be_closed_out(self):
        plan = plan_closure(
            ExecutionState.REAPPROVAL_REQUIRED,
            CLOSURE_GROUND_PREFLIGHT,
            codes=["PRICE_MOVED_BEYOND_TOLERANCE"],
        )
        self.assertTrue(plan.ok, plan.refusals)
        self.assertEqual(plan.target_state, ExecutionState.REEVALUATION_REQUIRED)

    def test_the_invalidating_codes_are_about_the_price_not_the_paperwork(self):
        self.assertEqual(
            DECISION_INVALIDATING_CODES,
            frozenset({"PRICE_MOVED_BEYOND_TOLERANCE", "STALE_QUOTE",
                       "APPROVAL_EXPIRED", "APPROVAL_MONTH_ROLLED_OVER"}),
        )


class ClosedDecisionTests(unittest.TestCase):
    """What a closed decision can and cannot do afterwards."""

    def test_a_closed_decision_can_never_be_approved_again(self):
        with self.assertRaises(InvalidTransition):
            assert_transition(
                ExecutionState.REEVALUATION_REQUIRED, ExecutionState.APPROVED
            )

    def test_a_closed_decision_can_never_be_submitted(self):
        for target in (ExecutionState.PRE_EXECUTION_VALIDATED,
                       ExecutionState.SUBMITTED,
                       ExecutionState.SUBMISSION_UNCERTAIN,
                       ExecutionState.FILLED):
            with self.subTest(target=target):
                with self.assertRaises(InvalidTransition):
                    assert_transition(ExecutionState.REEVALUATION_REQUIRED, target)

    def test_a_closed_decision_releases_its_reservation(self):
        approvals = {DECISION_ID: synthetic_approval()}
        states = {DECISION_ID: ExecutionState.REEVALUATION_REQUIRED}
        self.assertEqual(sibling_reservations_usd(approvals, states, MONTH), ZERO)

    def test_released_states_are_the_terminal_ones_plus_reevaluation(self):
        self.assertEqual(
            RELEASED_STATES,
            frozenset(TERMINAL_STATES | {ExecutionState.REEVALUATION_REQUIRED}),
        )
        self.assertNotIn(ExecutionState.REAPPROVAL_REQUIRED, RELEASED_STATES)


class ClosurePersistenceTests(unittest.TestCase):
    """The transition is legal through the store, audited, and loses nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.exec_path = os.path.join(self.tmp, "executions.json")
        self.audit_path = os.path.join(self.tmp, "audit.jsonl")
        self.approvals_path = os.path.join(self.tmp, "approvals.json")

    def close(self, record, plan):
        transition(record, plan.target_state, reason=plan.reason,
                   audit_path=self.audit_path)
        save_executions({record.decision_id: record}, path=self.exec_path)

    def test_the_whole_lifecycle_end_to_end(self):
        approval = synthetic_approval()
        save_approvals({approval.decision_id: approval}, path=self.approvals_path)
        record = synthetic_record()
        save_executions({record.decision_id: record}, path=self.exec_path)

        def reserved():
            records = load_executions(path=self.exec_path)
            return sibling_reservations_usd(
                {approval.decision_id: approval},
                {k: v.state for k, v in records.items()},
                MONTH,
            )

        # before: $10.00 held, $15.00 deployable
        self.assertEqual(reserved(), Decimal("10.00"))

        plan = plan_closure(record.state, CLOSURE_GROUND_PREFLIGHT,
                            codes=["PRICE_MOVED_BEYOND_TOLERANCE"])
        self.assertTrue(plan.ok, plan.refusals)
        self.close(record, plan)

        # after: nothing held, the full authorization deployable again
        self.assertEqual(reserved(), ZERO)
        self.assertEqual(Decimal("25.00") - reserved(), Decimal("25.00"))

        # the approval is still on disk, unchanged
        with open(self.approvals_path, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertIn(approval.decision_id, stored["approvals"])
        self.assertEqual(
            stored["approvals"][approval.decision_id]["approval_id"], APPROVAL_ID
        )

        # and the record kept its own history rather than being rewritten
        reloaded = load_executions(path=self.exec_path)[approval.decision_id]
        self.assertEqual(reloaded.state, ExecutionState.REEVALUATION_REQUIRED)
        self.assertEqual(reloaded.approval_id, APPROVAL_ID)
        self.assertEqual(reloaded.history[-1]["from"], ExecutionState.APPROVED)
        self.assertIn("PRICE_MOVED_BEYOND_TOLERANCE", reloaded.history[-1]["reason"])

        events = read_audit(self.audit_path)
        self.assertTrue(
            any(e["event"] == "STATE_CHANGE"
                and e["detail"]["to"] == ExecutionState.REEVALUATION_REQUIRED
                for e in events)
        )

    def test_closing_twice_is_safe(self):
        record = synthetic_record()
        plan = plan_closure(record.state, CLOSURE_GROUND_PREFLIGHT,
                            codes=["PRICE_MOVED_BEYOND_TOLERANCE"])
        self.close(record, plan)
        with open(self.exec_path, encoding="utf-8") as handle:
            first = json.load(handle)["executions"]

        again = plan_closure(record.state, CLOSURE_GROUND_PREFLIGHT,
                             codes=["PRICE_MOVED_BEYOND_TOLERANCE"])
        self.assertTrue(again.ok)
        self.assertTrue(again.already_closed)
        self.assertEqual(again.target_state, "")

        # nothing further was written, so the history did not grow
        with open(self.exec_path, encoding="utf-8") as handle:
            second = json.load(handle)["executions"]
        self.assertEqual(
            first[record.decision_id]["history"],
            second[record.decision_id]["history"],
        )

    def test_the_released_dollars_need_a_new_decision_and_a_new_approval(self):
        """The old approval can never fund the replacement."""
        old = synthetic_approval()
        closed = synthetic_record(state=ExecutionState.REEVALUATION_REQUIRED)
        fresh = synthetic_record(decision_id="dec_synthetic_0002",
                                 state=ExecutionState.PROPOSED)
        fresh.approval_id = None

        states = {closed.decision_id: closed.state, fresh.decision_id: fresh.state}
        # The replacement is only PROPOSED, so nothing is reserved yet, and the
        # closed decision contributes nothing.
        self.assertEqual(
            sibling_reservations_usd({old.decision_id: old}, states, MONTH), ZERO
        )
        # And it cannot inherit the old approval: an APPROVED record demands an
        # approval id of its own.
        with self.assertRaises(InvalidTransition):
            transition(fresh, ExecutionState.APPROVED, audit_path=None)

        new_approval = synthetic_approval(decision_id=fresh.decision_id,
                                          approval_id="apr_synthetic_0002")
        transition(fresh, ExecutionState.APPROVED,
                   approval_id=new_approval.approval_id, audit_path=None)
        self.assertEqual(
            sibling_reservations_usd(
                {old.decision_id: old, fresh.decision_id: new_approval},
                {closed.decision_id: closed.state, fresh.decision_id: fresh.state},
                MONTH,
            ),
            Decimal("10.00"),
        )

    def test_a_split_plans_siblings_are_untouched_by_one_legs_closure(self):
        legs = {
            "leg-a": synthetic_approval("leg-a", "10.00"),
            "leg-b": synthetic_approval("leg-b", "8.00"),
        }
        states = {"leg-a": ExecutionState.APPROVED, "leg-b": ExecutionState.APPROVED}
        self.assertEqual(
            sibling_reservations_usd(legs, states, MONTH), Decimal("18.00")
        )
        states["leg-a"] = ExecutionState.REEVALUATION_REQUIRED
        self.assertEqual(
            sibling_reservations_usd(legs, states, MONTH), Decimal("8.00")
        )


class ClosureScriptTests(unittest.TestCase):
    """The CLI itself: grounded, idempotent, and out of a scheduler's reach."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import scripts.close_stale_decision as cli

        self.cli = cli

    def run_cli(self, *argv, env=None):
        previous = {}
        for key, value in (env or {}).items():
            previous[key] = os.environ.get(key)
            os.environ[key] = value
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer), redirect_stderr(buffer):
                code = self.cli.main(list(argv))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return code, buffer.getvalue()

    def test_a_scheduled_run_may_never_close_a_decision_out(self):
        from src.scheduling import (
            FORBIDDEN_SCRIPTS,
            SCHEDULED_POST_RUN_MARKER,
            SCHEDULED_RUN_MARKER,
        )

        self.assertIn("close_stale_decision.py", FORBIDDEN_SCRIPTS)
        for marker in (SCHEDULED_RUN_MARKER, SCHEDULED_POST_RUN_MARKER):
            with self.subTest(marker=marker):
                code, _ = self.run_cli(
                    DECISION_ID, "--ground", "expired", env={marker: "1"}
                )
                self.assertEqual(code, 4)

    def test_it_refuses_an_unknown_decision_without_writing(self):
        code, _ = self.run_cli("dec_synthetic_nonexistent", "--ground", "expired")
        self.assertEqual(code, 2)

    def test_it_places_nothing_and_approves_nothing(self):
        with open(self.cli.__file__, encoding="utf-8") as handle:
            body = handle.read()
        for forbidden in ("place_equity_order", "place_crypto_order",
                          "review_equity_order", "preview_crypto_order",
                          "create_approval", "save_approvals", "approve_decision"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_the_audit_event_records_that_the_approval_is_kept(self):
        self.assertEqual(EVENT_DECISION_CLOSED, "DECISION_CLOSED")
        with open(self.cli.__file__, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("approval_retained", body)


if __name__ == "__main__":
    unittest.main()
