#!/usr/bin/env python3
"""Ask the broker what actually happened, and settle the record.

    python3 scripts/reconcile_submission.py <decision_id> --orders orders.json

`orders.json` is a read-only get_equity_orders / get_crypto_orders payload.
This is the only way out of SUBMISSION_UNCERTAIN, and the only thing that ever
marks a purchase FILLED and commits it to the monthly ledger.

A not-found order is NEVER read as "nothing happened".
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution import ExecutionState  # noqa: E402
from src.execution_store import (  # noqa: E402
    AUDIT_LOG_PATH, EVENT_FILL_RECONCILED, load_executions, save_executions,
    transition, write_audit,
)
from src.models import parse_money, usd  # noqa: E402
from src.state import (  # noqa: E402
    commit_purchase, load_budget_state, load_config, save_budget_state,
)
from src.submission import SubmissionError, load_tickets, reconcile_submission  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile a submitted order")
    parser.add_argument("decision_id")
    parser.add_argument("--orders", required=True)
    parser.add_argument("--commit", action="store_true",
                        help="on a confirmed fill, record the spend in state/budget.json")
    args = parser.parse_args()

    config = load_config()
    executions = load_executions()
    record = executions.get(args.decision_id)
    if record is None:
        print("No execution record for %s." % args.decision_id, file=sys.stderr)
        return 2

    tickets = [t for t in load_tickets().values() if t.decision_id == args.decision_id]
    if not tickets:
        print("No ticket found for %s." % args.decision_id, file=sys.stderr)
        return 2
    ticket = sorted(tickets, key=lambda t: t.created_at)[-1]

    with open(args.orders, encoding="utf-8") as handle:
        payload = json.load(handle)

    try:
        result = reconcile_submission(ticket, record, payload,
                                      asset_class_hint=ticket.asset_class or "EQUITY")
    except SubmissionError as exc:
        print("Reconciliation failed: %s" % exc, file=sys.stderr)
        return 3

    print("=" * 72)
    print("SUBMISSION RECONCILIATION — %s" % args.decision_id)
    print("=" * 72)
    print("  record state   : %s" % record.state)
    print("  ref_id         : %s" % ticket.ref_id)
    print("  order found    : %s" % result.found)
    if result.found:
        print("  broker state   : %s" % result.state)
        print("  filled         : %s of %s" % (usd(result.filled_usd), usd(result.requested_usd)))
    for note in result.notes:
        print("  ! %s" % note)

    write_audit(EVENT_FILL_RECONCILED, decision_id=args.decision_id,
                detail=result.to_dict(), path=AUDIT_LOG_PATH)

    if result.next_state is None:
        print("\nNo state change. The record stays %s." % record.state)
        print("Do NOT resubmit. Re-run this after the broker settles.")
        return 1

    if record.state == ExecutionState.SUBMISSION_UNCERTAIN and \
            result.next_state in (ExecutionState.FILLED, ExecutionState.PARTIALLY_FILLED):
        transition(record, ExecutionState.SUBMITTED, reason="order located by ref_id",
                   audit_path=AUDIT_LOG_PATH, broker_order_id=result.broker_order_id)
    transition(record, result.next_state, reason="reconciled against the broker",
               audit_path=AUDIT_LOG_PATH, broker_order_id=result.broker_order_id)
    save_executions(executions)
    print("\n  record state   : %s" % record.state)

    if args.commit and result.filled_usd > parse_money("0"):
        budget = load_budget_state(config)
        budget = commit_purchase(budget, args.decision_id, result.filled_usd)
        save_budget_state(budget)
        print("  committed %s to the %s ledger; remaining %s"
              % (usd(result.filled_usd), budget.month, usd(budget.remaining_usd)))
    elif result.filled_usd > parse_money("0"):
        print("  (pass --commit to record %s against the monthly authorization)"
              % usd(result.filled_usd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
