#!/usr/bin/env python3
"""The bridge. Consumes a ticket, writes the write-ahead record, then submits.

    python3 scripts/submit_approved.py <decision_id>

With execution disabled (today) the submitter is DisabledSubmitter and this
refuses before any call. When live, the submitter is ManualHandoffSubmitter:
the write-ahead lands first, the exact parameters are written to
state/pending_submission.json, and exactly one MCP call is made by hand.

This script never retries and never loops.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.approval import get_approval  # noqa: E402
from src.decision_logger import read_decisions  # noqa: E402
from src.execution import ExecutionDisabled, ExecutionState  # noqa: E402
from src.execution_store import load_executions, save_executions  # noqa: E402
from src.state import load_config  # noqa: E402
from src.submission import (  # noqa: E402
    DisabledSubmitter,
    ManualHandoffSubmitter,
    SubmissionError,
    SubmissionHandoffRequired,
    TicketConsumed,
    load_tickets,
    save_tickets,
    submit_once,
    verify_ticket,
)
from src.execution_store import AUDIT_LOG_PATH  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit an approved, ticketed order")
    parser.add_argument("decision_id")
    parser.add_argument("--ticket-id")
    args = parser.parse_args()

    config = load_config()
    decision = next((r for r in read_decisions() if r.get("decision_id") == args.decision_id), None)
    if decision is None:
        print("No decision %s found." % args.decision_id, file=sys.stderr)
        return 2

    approval = get_approval(args.decision_id)
    tickets = load_tickets()
    candidates = [t for t in tickets.values()
                  if t.decision_id == args.decision_id and not t.is_consumed]
    if args.ticket_id:
        candidates = [t for t in candidates if t.ticket_id == args.ticket_id]
    if not candidates:
        print("No unconsumed ticket for %s. Run prepare_submission.py first."
              % args.decision_id, file=sys.stderr)
        return 2
    ticket = sorted(candidates, key=lambda t: t.created_at)[-1]

    blockers = verify_ticket(ticket, decision, approval, config)
    print("=" * 72)
    print("SUBMISSION — %s / ticket %s" % (args.decision_id, ticket.ticket_id))
    print("=" * 72)
    if blockers:
        print("REFUSED before any broker call:")
        for code, message in blockers:
            print("  x [%s] %s" % (code, message))
        return 1

    executions = load_executions()
    record = executions.get(args.decision_id)
    if record is None:
        print("No execution record for %s." % args.decision_id, file=sys.stderr)
        return 2

    submitter = DisabledSubmitter() if config.execution_mode == "DRY_RUN" else ManualHandoffSubmitter()
    print("submitter: %s" % submitter.name)

    try:
        response = submit_once(ticket, record, submitter, tickets,
                               audit_path=AUDIT_LOG_PATH)
    except SubmissionHandoffRequired as exc:
        save_executions(executions)
        print("\nWRITE-AHEAD COMPLETE. State is now %s." % record.state)
        print(exc)
        print("\nAfter the single MCP call, run:")
        print("  python3 scripts/record_submission.py %s --response resp.json" % args.decision_id)
        print("If anything is unclear, run reconcile_submission.py instead. Never retry.")
        return 0
    except (ExecutionDisabled, TicketConsumed, SubmissionError) as exc:
        save_executions(executions)
        print("\nSUBMISSION REFUSED / HALTED: %s" % exc)
        print("Execution record state: %s" % record.state)
        if record.state == ExecutionState.SUBMISSION_UNCERTAIN:
            print("The write-ahead is durable. Reconcile before doing anything else.")
        return 1

    save_executions(executions)
    print("submitter returned: %r" % (response,))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
