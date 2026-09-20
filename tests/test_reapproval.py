"""Regression tests for the re-approval lifecycle and the pending-decision view.

Both cover the same concrete incident, 2026-09-18:

``dec_0000000000000001`` — a $10.00 SNDK BUY — was approved while the repository
was **disarmed**. The documented first-live-purchase runbook requires arming
*before* approving, because ``config.json`` is part of the policy-fingerprint
surface. So arming necessarily invalidates that approval and a fresh human
approval must be created afterwards.

Two defects blocked that:

1. ``scripts/approve_decision.py`` wrote the new approval to
   ``state/approvals.json`` and only afterwards attempted
   ``transition(record, APPROVED)``. The record was already ``APPROVED`` and
   there is no ``APPROVED -> APPROVED`` edge, so the transition raised *after*
   the approval had been persisted — leaving the approval store and the
   execution ledger disagreeing, recoverable only by hand-editing JSON.
2. ``scripts/show_pending_decision.py`` listed six cancelled predecessors
   alongside the live decision, which is how a human approves the wrong
   ``decision_id``.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.approval import (
    POLICY_SURFACE_FILES,
    ApprovalRecord,
    create_approval,
    fingerprint,
    policy_fingerprint,
    verify_approval,
)
from src.execution import ExecutionState, InvalidTransition
from src.execution_store import (
    ExecutionRecord,
    REAPPROVAL_PATH,
    plan_transitions_to_approved,
    transition,
)
from src.state import REPO_ROOT
from tests.helpers import buy_decision, make_config, make_state

NOW = datetime(2026, 9, 18, 16, 19, 15, tzinfo=timezone.utc)


def sndk_decision():
    """A synthetic decision in the shipped shape: a $10.00 SNDK equity BUY."""
    state = make_state(make_config(), month="2026-09")
    return buy_decision(
        state, "10.00",
        decision_id="dec_0000000000000001",
        month="2026-09",
        ticker="SNDK",
        security_name="SanDisk Corporation",
        asset_class="EQUITY",
        asset_type="us_common_stock",
        exchange="NASDAQ",
        current_price_usd="1717.06",
    )


def armed_policy_root():
    """A copy of the policy surface with the three execution switches armed.

    Real arming is a human step and CLAUDE.md forbids the model performing it,
    so the fingerprint change is reproduced against a temporary tree. The
    repository under test stays disarmed.
    """
    tmp = tempfile.mkdtemp()
    for relative in POLICY_SURFACE_FILES:
        dest = os.path.join(tmp, relative)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(REPO_ROOT, relative), dest)
    import json
    path = os.path.join(tmp, "config.json")
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    config["execution_mode"] = "APPROVAL_REQUIRED"
    config["agent_enabled"] = True
    config["live_trading"] = True
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
    return tmp


# ==========================================================================
# 1. The state machine
# ==========================================================================


class TransitionPlanningTests(unittest.TestCase):

    def test_a_fresh_proposal_goes_straight_to_approved(self):
        self.assertEqual(
            plan_transitions_to_approved(ExecutionState.PROPOSED),
            (ExecutionState.APPROVED,),
        )

    def test_an_already_approved_record_routes_through_reapproval_required(self):
        """The 2026-09-18 case. APPROVED -> APPROVED is not a legal edge."""
        self.assertEqual(
            plan_transitions_to_approved(ExecutionState.APPROVED),
            (ExecutionState.REAPPROVAL_REQUIRED, ExecutionState.APPROVED),
        )
        self.assertEqual(REAPPROVAL_PATH,
                         (ExecutionState.REAPPROVAL_REQUIRED, ExecutionState.APPROVED))

    def test_reapproval_required_goes_back_to_approved_directly(self):
        self.assertEqual(
            plan_transitions_to_approved(ExecutionState.REAPPROVAL_REQUIRED),
            (ExecutionState.APPROVED,),
        )

    def test_planning_is_pure_and_persists_nothing(self):
        """The whole point: the caller can check legality before writing."""
        record = ExecutionRecord(decision_id="d1", state=ExecutionState.APPROVED)
        plan_transitions_to_approved(record.state)
        self.assertEqual(record.state, ExecutionState.APPROVED)
        self.assertEqual(record.history, [])

    def test_a_closed_decision_can_never_be_planned_back_to_approved(self):
        for state in (ExecutionState.CANCELLED, ExecutionState.FILLED,
                      ExecutionState.REJECTED, ExecutionState.EXPIRED,
                      ExecutionState.EXECUTION_FAILED,
                      ExecutionState.REEVALUATION_REQUIRED,
                      ExecutionState.SUBMITTED,
                      ExecutionState.SUBMISSION_UNCERTAIN):
            with self.subTest(state=state):
                with self.assertRaises(InvalidTransition):
                    plan_transitions_to_approved(state)

    def test_the_model_cannot_mark_a_record_approved_without_an_approval(self):
        """'Only the human approval path may produce an APPROVED record.'"""
        record = ExecutionRecord(decision_id="dec_x")
        with self.assertRaises(InvalidTransition) as caught:
            transition(record, ExecutionState.APPROVED, audit_path=None)
        self.assertIn("approval_id", str(caught.exception))
        self.assertEqual(record.state, ExecutionState.PROPOSED)
        self.assertEqual(record.history, [])

    def test_an_approval_id_already_on_the_record_satisfies_the_guard(self):
        record = ExecutionRecord(decision_id="dec_x", approval_id="apr_1")
        transition(record, ExecutionState.APPROVED, audit_path=None)
        self.assertEqual(record.state, ExecutionState.APPROVED)


# ==========================================================================
# 2. The 2026-09-18 incident, end to end
# ==========================================================================


class ArmingInvalidatesThenReapprovalSucceedsTests(unittest.TestCase):
    """Arm -> old approval dies -> fresh approval lands, with no hand-editing."""

    def setUp(self):
        self.decision = sndk_decision()
        self.approval = create_approval(self.decision, approved_by="local-cli", now=NOW)
        self.record = ExecutionRecord(
            decision_id=self.decision["decision_id"],
            decision_fingerprint=self.approval.decision_fingerprint,
            month="2026-09",
            asset="SNDK",
            amount_usd="10.00",
        )
        transition(self.record, ExecutionState.APPROVED, audit_path=None,
                   approval_id=self.approval.approval_id)
        self.armed_root = armed_policy_root()

    def tearDown(self):
        shutil.rmtree(self.armed_root, ignore_errors=True)

    def test_the_approval_is_valid_while_the_repository_stays_disarmed(self):
        verdict = verify_approval(self.approval, self.decision,
                                  now=NOW + timedelta(minutes=5))
        self.assertTrue(verdict.ok, verdict.blockers)

    def test_arming_changes_the_policy_fingerprint_and_voids_the_approval(self):
        """A genuine policy change must still invalidate an old approval."""
        self.assertNotEqual(policy_fingerprint(self.armed_root),
                            policy_fingerprint(REPO_ROOT))
        verdict = verify_approval(self.approval, self.decision,
                                  now=NOW + timedelta(minutes=5),
                                  repo_root=self.armed_root)
        self.assertFalse(verdict.ok)
        self.assertIn("POLICY_CHANGED", verdict.codes)

    def test_the_documented_lifecycle_completes_without_editing_json(self):
        """PROPOSED -> APPROVED -> REAPPROVAL_REQUIRED -> APPROVED."""
        planned = plan_transitions_to_approved(self.record.state)
        self.assertEqual(planned, REAPPROVAL_PATH)

        fresh = create_approval(self.decision, approved_by="local-cli",
                                now=NOW + timedelta(hours=1))
        transition(self.record, ExecutionState.REAPPROVAL_REQUIRED,
                   audit_path=None, approval_id=None,
                   reason="prior approval %s invalidated (POLICY_CHANGED)"
                          % self.approval.approval_id)
        transition(self.record, ExecutionState.APPROVED, audit_path=None,
                   approval_id=fresh.approval_id, reason="approved via CLI")

        self.assertEqual(self.record.state, ExecutionState.APPROVED)
        self.assertEqual(self.record.approval_id, fresh.approval_id)
        self.assertNotEqual(fresh.approval_id, self.approval.approval_id)

        # The history keeps the invalidation explicit rather than silently
        # overwriting the first approval.
        moves = [(h["from"], h["to"]) for h in self.record.history]
        self.assertEqual(moves, [
            (ExecutionState.PROPOSED, ExecutionState.APPROVED),
            (ExecutionState.APPROVED, ExecutionState.REAPPROVAL_REQUIRED),
            (ExecutionState.REAPPROVAL_REQUIRED, ExecutionState.APPROVED),
        ])
        self.assertIn("POLICY_CHANGED", self.record.history[1]["reason"])

    def test_the_old_approval_never_authorizes_the_armed_purchase(self):
        """Even after a clean re-approval, the superseded id is not a key."""
        stale = self.approval
        verdict = verify_approval(stale, self.decision,
                                  now=NOW + timedelta(hours=1),
                                  repo_root=self.armed_root)
        self.assertFalse(verdict.ok)

    def test_a_decision_payload_change_also_still_voids_the_approval(self):
        modified = dict(self.decision, proposed_amount_usd="5.00")
        self.assertNotEqual(fingerprint(modified), fingerprint(self.decision))
        verdict = verify_approval(self.approval, modified, now=NOW)
        self.assertFalse(verdict.ok)
        self.assertIn("DECISION_MODIFIED", verdict.codes)


class NoApprovalIsPersistedUnlessTheTransitionWillSucceedTests(unittest.TestCase):
    """The ordering defect itself: plan first, write second."""

    def read_script(self):
        with open(os.path.join(REPO_ROOT, "scripts", "approve_decision.py"),
                  encoding="utf-8") as handle:
            return handle.read()

    def test_the_script_plans_the_transition_before_creating_the_approval(self):
        source = self.read_script()
        plan_at = source.index("plan_transitions_to_approved(")
        create_at = source.index("create_approval(")
        save_at = source.index("save_approvals(approvals)")
        self.assertLess(plan_at, create_at,
                        "the transition must be planned before an approval is minted")
        self.assertLess(plan_at, save_at,
                        "the transition must be planned before an approval is persisted")

    def test_the_ledger_is_written_before_the_approval_artifact(self):
        """Either half-write fails closed; this is the documented order."""
        source = self.read_script()
        self.assertLess(source.index("save_executions(executions)"),
                        source.index("save_approvals(approvals)"))

    def test_a_terminal_record_is_refused_before_anything_is_minted(self):
        for state in (ExecutionState.CANCELLED, ExecutionState.FILLED):
            with self.subTest(state=state):
                with self.assertRaises(InvalidTransition):
                    plan_transitions_to_approved(state)


# ==========================================================================
# 3. The pending-decision view
# ==========================================================================


def load_pending_script():
    import importlib.util
    path = os.path.join(REPO_ROOT, "scripts", "show_pending_decision.py")
    spec = importlib.util.spec_from_file_location("_show_pending", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PendingDecisionViewTests(unittest.TestCase):
    """Built from the real 2026-09 history: one live decision, six closed."""

    def setUp(self):
        self.mod = load_pending_script()
        self.decision = sndk_decision()
        self.approval = create_approval(self.decision, approved_by="local-cli", now=NOW)
        # Every TTL judgement is made against a fixed instant. Reading the wall
        # clock made these tests pass on the day they were written and fail the
        # next morning, when the 24-hour approval had quietly expired.
        self.at = NOW + timedelta(minutes=5)

    def install_ledger(self, exec_state, approval, decision=None):
        """Point the script at a synthetic ledger instead of the live one.

        These tests must not read ``state/approvals.json`` or
        ``state/executions.json``. Those files belong to the operational system:
        they change whenever a real approval is granted or the repository is
        armed, which made this suite fail for reasons that had nothing to do
        with the code under test — and they are absent entirely from the
        sanitized public tree.
        """
        decision = decision or self.decision
        did = decision["decision_id"]
        record = ExecutionRecord(
            decision_id=did, state=exec_state, month="2026-09",
            asset="SNDK", amount_usd="10.00",
            approval_id=getattr(approval, "approval_id", None),
        )
        record.history = [{"from": ExecutionState.PROPOSED, "to": exec_state,
                           "at": "2026-01-16T09:30:00Z", "reason": "synthetic fixture"}]
        self.mod.buy_decisions = lambda: [decision]
        self.mod.load_approvals = lambda: ({did: approval} if approval else {})
        self.mod.load_executions = lambda: {did: record}

    def far_future_approval(self):
        """A synthetic approval whose TTL cannot lapse during the test run."""
        return ApprovalRecord.from_dict(dict(
            self.approval.to_dict(),
            expires_at="2099-01-01T00:00:00Z",
            policy_fingerprint=policy_fingerprint(REPO_ROOT),
        ))

    def test_a_fresh_proposal_with_no_approval_is_pending(self):
        reason = self.mod.pending_reason(
            self.decision, ExecutionState.PROPOSED, None, now=self.at)
        self.assertIsNotNone(reason)
        self.assertIn("first human approval", reason)

    def test_a_reapproval_required_record_is_pending(self):
        reason = self.mod.pending_reason(
            self.decision, ExecutionState.REAPPROVAL_REQUIRED, None, now=self.at)
        self.assertIsNotNone(reason)

    def test_a_validly_approved_record_is_not_pending(self):
        reason = self.mod.pending_reason(
            self.decision, ExecutionState.APPROVED, self.approval, now=self.at)
        self.assertIsNone(reason)

    def test_an_approved_record_whose_approval_went_stale_is_pending_again(self):
        """The armed-repository case: APPROVED in the ledger, not in reality."""
        stale = ApprovalRecord.from_dict(
            dict(self.approval.to_dict(), policy_fingerprint="0" * 64))
        reason = self.mod.pending_reason(
            self.decision, ExecutionState.APPROVED, stale, now=self.at)
        self.assertIsNotNone(reason)
        self.assertIn("re-approval", reason)

    def test_an_approved_record_with_no_approval_on_file_is_pending(self):
        reason = self.mod.pending_reason(
            self.decision, ExecutionState.APPROVED, None, now=self.at)
        self.assertIsNotNone(reason)

    def test_closed_states_are_never_pending(self):
        for state in (ExecutionState.CANCELLED, ExecutionState.FILLED,
                      ExecutionState.REJECTED, ExecutionState.EXPIRED,
                      ExecutionState.REEVALUATION_REQUIRED,
                      ExecutionState.EXECUTION_FAILED):
            with self.subTest(state=state):
                self.assertIsNone(
                    self.mod.pending_reason(self.decision, state, None,
                                            now=self.at))

    def test_a_cancelled_predecessor_is_hidden_by_default(self):
        """The 2026-09 shape: withdrawn proposals must not crowd the live one."""
        self.install_ledger(ExecutionState.CANCELLED, None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self._run([])
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertNotIn("CANCELLED", out)
        self.assertEqual(out.strip(), self.mod.NOTHING_PENDING)

    def test_nothing_pending_prints_one_concise_line(self):
        self.install_ledger(ExecutionState.CANCELLED, None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self._run([])
        self.assertEqual(rc, 0)
        out = buf.getvalue().strip()
        self.assertEqual(out, self.mod.NOTHING_PENDING)
        self.assertNotIn("=" * 10, out)

    def test_an_already_approved_decision_is_not_labelled_historical(self):
        """It needs no approval, but it is not history either."""
        self.install_ledger(ExecutionState.APPROVED, self.far_future_approval())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self._run(["--decision-id", self.decision["decision_id"]])
        out = buf.getvalue()
        self.assertIn("ALREADY APPROVED", out)
        self.assertNotIn("HISTORICAL", out)

    def test_history_reveals_the_closed_records(self):
        self.install_ledger(ExecutionState.CANCELLED, None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self._run(["--history"])
        out = buf.getvalue()
        self.assertIn("HISTORICAL", out)
        self.assertIn("CANCELLED", out)

    def _run(self, argv):
        old = sys.argv
        sys.argv = ["show_pending_decision.py"] + argv
        try:
            return self.mod.main()
        finally:
            sys.argv = old


if __name__ == "__main__":
    unittest.main()
