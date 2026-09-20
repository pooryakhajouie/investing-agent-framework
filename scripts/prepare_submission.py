#!/usr/bin/env python3
"""Run preflight and, if it passes, mint a single-use submission ticket.

    python3 scripts/prepare_submission.py <decision_id> --snapshot broker.json

Creates nothing if preflight fails. Places no order. The ticket freezes the
exact order parameters, the quote, the decision fingerprint, the policy
fingerprint, the deterministic ref_id and a 5-minute expiry.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.execute_approved import load_snapshot  # noqa: E402
from src.approval import get_approval  # noqa: E402
from src.decision_logger import read_decisions  # noqa: E402
from src.execution import ExecutionState, preflight  # noqa: E402
from src.execution_store import (  # noqa: E402
    EVENT_PREFLIGHT, ExecutionRecord, load_executions, save_executions,
    transition, write_audit,
)
from src.models import usd  # noqa: E402
from src.state import load_budget_state, load_config  # noqa: E402
from src.submission import SubmissionError, create_ticket, load_tickets, save_tickets  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a submission ticket")
    parser.add_argument("decision_id")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--account", default="<agentic-account>")
    args = parser.parse_args()

    config = load_config()
    budget_state = load_budget_state(config)

    decision = next((r for r in read_decisions() if r.get("decision_id") == args.decision_id), None)
    if decision is None:
        print("No decision %s found." % args.decision_id, file=sys.stderr)
        return 2

    approval = get_approval(args.decision_id)
    executions = load_executions()
    record = executions.get(args.decision_id) or ExecutionRecord(decision_id=args.decision_id)

    snapshot = load_snapshot(args.snapshot)
    result = preflight(decision, approval, config, budget_state, snapshot,
                       execution_state=record.state)
    write_audit(EVENT_PREFLIGHT, decision_id=args.decision_id,
                detail={"ok": result.ok, "blockers": result.codes})

    print("=" * 72)
    print("SUBMISSION TICKET PREPARATION — %s" % args.decision_id)
    print("=" * 72)
    if not result.ok:
        print("Preflight FAILED. No ticket was created.")
        for code, message in result.blockers:
            print("  x [%s] %s" % (code, message))
        return 1

    try:
        ticket = create_ticket(decision, approval, result, snapshot, args.account)
    except SubmissionError as exc:
        print("Ticket refused: %s" % exc, file=sys.stderr)
        return 3

    tickets = load_tickets()
    tickets[ticket.ticket_id] = ticket
    save_tickets(tickets)

    if record.state == ExecutionState.APPROVED:
        transition(record, ExecutionState.PRE_EXECUTION_VALIDATED, reason="ticket minted")
        executions[args.decision_id] = record
        save_executions(executions)

    print("Ticket %s created, expires %s" % (ticket.ticket_id, ticket.expires_at))
    print("  asset      : %s (%s)" % (ticket.asset, ticket.asset_class))
    print("  amount     : %s" % usd(ticket.amount_usd))
    print("  settled cash: %s" % usd(ticket.settled_cash_usd))
    print("  ref_id     : %s" % ticket.ref_id)
    print("  fingerprint: %s" % ticket.ticket_fingerprint[:32])
    print("\nOrder parameters that WOULD be sent:")
    print(json.dumps(ticket.order_params, indent=2))
    print("\nNothing was sent. Next: scripts/submit_approved.py %s" % args.decision_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
