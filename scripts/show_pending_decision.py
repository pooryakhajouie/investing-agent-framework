#!/usr/bin/env python3
"""Show the decisions that are actually awaiting human approval right now.

Read-only. Touches no broker, changes no state.

    python3 scripts/show_pending_decision.py              # actionable only
    python3 scripts/show_pending_decision.py --history    # + historical records
    python3 scripts/show_pending_decision.py --all        # + every month
    python3 scripts/show_pending_decision.py --decision-id dec_abc123

By default this answers one question — *what, if anything, is waiting for me to
approve it?* — and nothing else. The ledger accumulates superseded, withdrawn,
expired and filled records; six cancelled SNDK proposals scrolling above the one
live one is how a human ends up approving the wrong decision_id. Historical
records are still reachable, behind a flag, because closing a decision out is a
disposition and not a deletion.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.approval import load_approvals, verify_approval  # noqa: E402
from src.decision_logger import read_decisions  # noqa: E402
from src.execution import VALID_TRANSITIONS, ExecutionState  # noqa: E402
from src.execution_store import load_executions  # noqa: E402
from src.models import usd  # noqa: E402
from src.state import current_month, load_budget_state, load_config  # noqa: E402

# The states from which a human approval is the next step. Kept as an explicit
# set *and* cross-checked against the state machine below, so that adding a
# state to the machine cannot silently change what this script advertises.
AWAITING_APPROVAL_STATES = (
    ExecutionState.PROPOSED,
    ExecutionState.REAPPROVAL_REQUIRED,
)

NOTHING_PENDING = "No decisions awaiting approval."


def awaits_approval(state: str) -> bool:
    """Can this decision still become APPROVED?

    Asked of the state machine rather than answered with a second hand-kept
    list, so a decision that has been withdrawn, expired, rejected or sent back
    for re-evaluation stops being advertised as approvable the moment the
    machine says it is not.
    """
    return ExecutionState.APPROVED in VALID_TRANSITIONS.get(state, ())


def buy_decisions():
    out = []
    for record in read_decisions():
        if record.get("decision") == "BUY" and (record.get("validation_result") or {}).get("valid"):
            out.append(record)
    return out


def pending_reason(record, exec_state, approval, now=None):
    """Why this decision is awaiting approval, or None if it is not.

    Two cases count, and the second is the one that made this worth writing.
    A record sitting in ``APPROVED`` whose approval has been invalidated — by a
    policy-fingerprint change, expiry, or an edited payload — is *not* approved
    in any sense that matters, and it needs a fresh human approval exactly like
    a ``PROPOSED`` one. Hiding it because the ledger says ``APPROVED`` is how a
    disarmed-then-armed repository looks ready when it is not.

    ``now`` is injectable so that callers — tests especially — can evaluate an
    approval's TTL against a fixed instant instead of the wall clock.
    """
    if exec_state in AWAITING_APPROVAL_STATES and awaits_approval(exec_state):
        if approval is None:
            return "awaiting its first human approval"
        verdict = verify_approval(approval, record, now=now)
        if verdict.ok:
            return None
        return "prior approval %s is invalid (%s)" % (
            approval.approval_id, ", ".join(verdict.codes))
    if exec_state == ExecutionState.APPROVED:
        if approval is None:
            return "ledger says APPROVED but no approval is on file"
        verdict = verify_approval(approval, record, now=now)
        if not verdict.ok:
            return "approval %s no longer valid (%s) — needs re-approval" % (
                approval.approval_id, ", ".join(verdict.codes))
    return None


def show(record, exec_state, approval, executions, reason=None):
    did = record.get("decision_id")
    print("decision_id    : %s" % did)
    print("state          : %s" % exec_state)
    if reason:
        print("awaiting       : %s" % reason)
    if record.get("plan_id"):
        print("plan           : %s  leg %s"
              % (record.get("plan_id"), record.get("leg_index", "?")))
    print("asset          : %s (%s / %s)"
          % (record.get("ticker"), record.get("asset_class"), record.get("asset_type")))
    print("position       : %s" % record.get("position_type"))
    print("amount         : $%s" % record.get("proposed_amount"))
    print("classification : %s   confidence %s"
          % (record.get("classification"), record.get("confidence")))
    print("thesis         : %s" % (str(record.get("thesis") or "")[:200]))
    print("approval       : %s"
          % ("%s, expires %s" % (approval.approval_id, approval.expires_at)
             if approval else "NONE — this is a recommendation, not permission"))
    stored = executions.get(did)
    if stored is not None and stored.history:
        last = stored.history[-1]
        print("last transition: %s -> %s at %s"
              % (last.get("from"), last.get("to"), last.get("at")))
        print("  reason       : %s" % last.get("reason", ""))
    print("-" * 72)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show decisions awaiting human approval")
    parser.add_argument("--decision-id")
    parser.add_argument("--history", action="store_true",
                        help="also show closed records: cancelled, superseded, "
                             "expired, rejected, filled")
    parser.add_argument("--all", action="store_true",
                        help="--history plus decisions from other months")
    args = parser.parse_args()

    show_history = args.history or args.all

    config = load_config()
    state = load_budget_state(config)
    approvals = load_approvals()
    executions = load_executions()
    month = current_month()

    records = buy_decisions()
    if args.decision_id:
        records = [r for r in records if r.get("decision_id") == args.decision_id]
    elif not args.all:
        records = [r for r in records if r.get("month") == month]

    pending, closed = [], []
    for record in records:
        did = record.get("decision_id")
        exec_state = executions[did].state if did in executions else ExecutionState.PROPOSED
        approval = approvals.get(did)
        reason = pending_reason(record, exec_state, approval)
        entry = (record, exec_state, approval, reason)
        (pending if reason else closed).append(entry)

    # Group pending legs of the same plan together, so a SPLIT_BUY_PLAN reads as
    # one plan with n legs rather than n unrelated proposals. Every leg is still
    # approved separately — there is no plan-level approval to be had.
    pending.sort(key=lambda e: (e[0].get("plan_id") or "",
                                e[0].get("leg_index") if e[0].get("leg_index") is not None else 0,
                                e[0].get("timestamp") or ""))

    # Nothing to approve and nothing asked for: say exactly that and stop.
    if not pending and not show_history and not args.decision_id:
        print(NOTHING_PENDING)
        return 0

    print("=" * 72)
    print("DECISIONS AWAITING APPROVAL")
    print("=" * 72)
    print("execution_mode : %s" % config.execution_mode)
    print("agent_enabled  : %s" % config.agent_enabled)
    print("month          : %s   remaining authorization %s"
          % (state.month, usd(state.remaining_usd)))
    print("-" * 72)

    if not pending:
        print(NOTHING_PENDING)
        print("(WAIT decisions never require approval — nothing is executed.)")
        print("-" * 72)

    plans_seen = set()
    for record, exec_state, approval, reason in pending:
        plan_id = record.get("plan_id")
        if plan_id and plan_id not in plans_seen:
            plans_seen.add(plan_id)
            legs = [e for e in pending if e[0].get("plan_id") == plan_id]
            print("PLAN %s — %d pending leg(s); EVERY leg needs its own approval"
                  % (plan_id, len(legs)))
        show(record, exec_state, approval, executions, reason)

    # "Not pending" covers two different things and conflating them misleads:
    # a decision that is closed out, and one that is already validly approved
    # and waiting on a later step. Neither needs approval; only the first is
    # history.
    approved_already = [e for e in closed if e[1] == ExecutionState.APPROVED]
    history = [e for e in closed if e[1] != ExecutionState.APPROVED]

    if approved_already:
        print()
        print("ALREADY APPROVED — no approval needed; next step is preflight")
        print("-" * 72)
        for record, exec_state, approval, _ in approved_already:
            show(record, exec_state, approval, executions)

    if history and show_history:
        print()
        print("=" * 72)
        print("HISTORICAL — cannot be approved, shown for the record")
        print("=" * 72)
        for record, exec_state, approval, _ in history:
            show(record, exec_state, approval, executions)
    elif history:
        print("%d historical record(s) hidden; pass --history to see them."
              % len(history))

    if pending:
        print("To approve one:  python3 scripts/approve_decision.py <decision_id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
