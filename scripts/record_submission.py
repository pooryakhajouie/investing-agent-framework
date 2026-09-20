#!/usr/bin/env python3
"""Record a broker response after the single MCP call.

    python3 scripts/record_submission.py <decision_id> --response resp.json

Moves SUBMISSION_UNCERTAIN -> SUBMITTED once an order id is known. It records
that an order EXISTS; it does not decide whether it filled. That is
reconcile_submission.py's job, because the immediate response is not evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution import ExecutionState  # noqa: E402
from src.execution_store import (  # noqa: E402
    AUDIT_LOG_PATH, load_executions, save_executions,
)
from src.submission import (  # noqa: E402
    SubmissionError, ingest_response, load_tickets, normalize_broker_response,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a broker order response")
    parser.add_argument("decision_id")
    parser.add_argument("--response", required=True)
    args = parser.parse_args()

    executions = load_executions()
    record = executions.get(args.decision_id)
    if record is None:
        print("No execution record for %s." % args.decision_id, file=sys.stderr)
        return 2
    if record.state != ExecutionState.SUBMISSION_UNCERTAIN:
        print("Record is in %s, not SUBMISSION_UNCERTAIN. Refusing." % record.state,
              file=sys.stderr)
        return 1

    tickets = [t for t in load_tickets().values() if t.decision_id == args.decision_id]
    if not tickets:
        print("No ticket found for %s." % args.decision_id, file=sys.stderr)
        return 2
    ticket = sorted(tickets, key=lambda t: t.created_at)[-1]

    with open(args.response, encoding="utf-8") as handle:
        raw = json.load(handle)

    try:
        response = normalize_broker_response(raw, ticket)
    except SubmissionError as exc:
        print("Broker response is unusable: %s" % exc, file=sys.stderr)
        print("Record stays SUBMISSION_UNCERTAIN. Reconcile; do NOT resubmit.",
              file=sys.stderr)
        return 3

    ingest_response(record, response, audit_path=AUDIT_LOG_PATH)
    save_executions(executions)
    print("Recorded broker order %s (state %s). Record is now %s."
          % (response.order_id, response.state, record.state))
    print("Next: python3 scripts/reconcile_submission.py %s --orders orders.json"
          % args.decision_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
