#!/usr/bin/env python3
"""Close out an approved-but-unsubmitted decision, releasing its reservation.

    # what would change, writing nothing
    python3 scripts/close_stale_decision.py <decision_id> --ground expired --dry-run

    # the approval's own TTL or month boundary has passed
    python3 scripts/close_stale_decision.py <decision_id> --ground expired

    # a live preflight says the priced premise is gone
    python3 scripts/close_stale_decision.py <decision_id> --ground preflight \
        --snapshot state/broker_snapshot.json

    # the owner is explicitly not doing this
    python3 scripts/close_stale_decision.py <decision_id> --ground abandoned \
        --note "why, in the owner's own words"

An approval reserves part of the month's authorization from the moment it is
granted until the purchase is submitted, because neither the broker nor the
local ledger can see it. That reservation is correct — right up until the
decision stops being executable at all, at which point it is just a silent
reduction of the month's authorization that nobody can spend and nobody
notices.

This is the supported way to say so. It is **not** approval, it is **not**
execution, and it **deletes nothing**: the approval record, the decision log
entry and every audit event stay exactly where they are. The decision moves to
``REEVALUATION_REQUIRED``, from which there is deliberately no path back to
``APPROVED`` — buying the same asset again needs a new evaluation, a new
``decision_id`` and a new human approval, at whatever the price is then.

Three grounds, each checkable rather than asserted:

    expired    the approval fails re-verification on its TTL or month boundary
    preflight  a live preflight reports a decision-invalidating blocker
               (price drift beyond tolerance, or a stale/absent quote)
    abandoned  the owner says so, in writing, in --note

Time passing on its own is not a ground. Neither is wanting the money back.

Idempotent: running it twice is a no-op the second time. Read-only against the
broker — the snapshot is gathered elsewhere with read-only tools. Refused
outright inside a scheduled run.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.execute_approved import load_snapshot  # noqa: E402
from src.approval import get_approval, verify_approval  # noqa: E402
from src.decision_logger import read_decisions  # noqa: E402
from src.execution import (  # noqa: E402
    CLOSURE_GROUND_ABANDONED,
    CLOSURE_GROUND_EXPIRED,
    CLOSURE_GROUND_PREFLIGHT,
    plan_closure,
    preflight,
)
from src.execution_store import (  # noqa: E402
    EVENT_DECISION_CLOSED,
    EVENT_PREFLIGHT,
    load_executions,
    save_executions,
    transition,
    write_audit,
)
from src.scheduling import (  # noqa: E402
    SCHEDULED_POST_RUN_MARKER,
    SCHEDULED_RUN_MARKER,
)
from src.state import load_budget_state, load_config  # noqa: E402

GROUNDS = {
    "expired": CLOSURE_GROUND_EXPIRED,
    "preflight": CLOSURE_GROUND_PREFLIGHT,
    "abandoned": CLOSURE_GROUND_ABANDONED,
}


def refuse_under_scheduler() -> bool:
    """A scheduled run may not close a decision out, for the same reason it may
    not approve one: moving the pipeline is a human act, whoever is watching."""
    return any(
        os.environ.get(marker) == "1"
        for marker in (SCHEDULED_RUN_MARKER, SCHEDULED_POST_RUN_MARKER)
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decision_id")
    parser.add_argument("--ground", required=True, choices=sorted(GROUNDS))
    parser.add_argument("--snapshot", help="read-only broker snapshot, for --ground preflight")
    parser.add_argument("--note", default="", help="required for --ground abandoned")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change and write nothing")
    args = parser.parse_args(argv)

    if refuse_under_scheduler():
        print("refused: closing a decision out is a human act and this is a "
              "scheduled run", file=sys.stderr)
        return 4

    ground = GROUNDS[args.ground]

    executions = load_executions()
    record = executions.get(args.decision_id)
    if record is None:
        print("No execution record for %s." % args.decision_id, file=sys.stderr)
        return 2

    decision = next(
        (r for r in read_decisions() if r.get("decision_id") == args.decision_id), None
    )
    approval = get_approval(args.decision_id)

    print("=" * 72)
    print("CLOSE OUT — %s" % args.decision_id)
    print("=" * 72)
    print("  asset            : %s" % (record.asset or "?"))
    print("  amount           : %s" % record.amount_usd)
    print("  month            : %s" % record.month)
    print("  current state    : %s" % record.state)
    print("  approval         : %s" % (approval.approval_id if approval else "NONE"))
    print("  ground           : %s" % ground)

    # --- establish the ground, rather than taking it on trust -------------
    codes = []
    if ground == CLOSURE_GROUND_EXPIRED:
        if approval is None or decision is None:
            print("\ncannot re-verify: %s is missing."
                  % ("the approval" if approval is None else "the decision record"),
                  file=sys.stderr)
            return 2
        verdict = verify_approval(approval, decision)
        codes = list(verdict.codes)
        print("  re-verification  : %s" % ("VALID" if verdict.ok else "INVALID"))
        for code, message in verdict.blockers:
            print("    x [%s] %s" % (code, message))
    elif ground == CLOSURE_GROUND_PREFLIGHT:
        if decision is None:
            print("\nNo decision %s in the log; cannot run preflight."
                  % args.decision_id, file=sys.stderr)
            return 2
        if not args.snapshot:
            print("\n--ground preflight requires --snapshot: a closure grounded in "
                  "a live preflight needs live data. An absent snapshot is not a "
                  "failed check.", file=sys.stderr)
            return 2
        config = load_config()
        budget_state = load_budget_state(config)
        snapshot = load_snapshot(args.snapshot)
        result = preflight(decision, approval, config, budget_state, snapshot,
                           execution_state=record.state)
        codes = list(result.codes)
        print("  preflight ok     : %s" % result.ok)
        for code, message in result.blockers:
            print("    x [%s] %s" % (code, message))
        if not args.dry_run:
            write_audit(EVENT_PREFLIGHT, decision_id=args.decision_id,
                        detail={"ok": result.ok, "blockers": codes,
                                "next_state": result.next_state,
                                "context": "close_stale_decision"})

    plan = plan_closure(record.state, ground, codes=codes, note=args.note)

    print("-" * 72)
    if not plan.ok:
        print("REFUSED. Nothing was changed.")
        for code, message in plan.refusals:
            print("  x [%s] %s" % (code, message))
        print("=" * 72)
        return 1

    if plan.already_closed:
        print("Already closed: %s" % plan.reason)
        print("No transition is needed, and none was made.")
        print("=" * 72)
        return 0

    print("WOULD TRANSITION" if args.dry_run else "TRANSITION")
    print("  %s -> %s" % (record.state, plan.target_state))
    print("  reason: %s" % plan.reason)
    print("  releases $%s of %s authorization back to the month"
          % (record.amount_usd, record.month))
    print("  the approval, the decision log entry and the audit trail are kept")

    if args.dry_run:
        print("\n--dry-run: nothing was written.")
        print("=" * 72)
        return 0

    previous_state = record.state
    transition(record, plan.target_state, reason=plan.reason)
    executions[args.decision_id] = record
    save_executions(executions)
    write_audit(
        EVENT_DECISION_CLOSED,
        decision_id=args.decision_id,
        detail={
            "ground": plan.ground,
            "codes": list(plan.codes),
            "reason": plan.reason,
            "from_state": previous_state,
            "to_state": plan.target_state,
            "released_usd": record.amount_usd,
            "month": record.month,
            "approval_id": record.approval_id,
            "approval_retained": True,
            "note": "the approval is superseded, not deleted, and may never be "
                    "reused: a purchase of this asset now needs a new decision "
                    "and a new approval",
        },
    )
    print("\nDone. %s is now %s." % (args.decision_id, record.state))
    print("It can no longer be approved or submitted.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
