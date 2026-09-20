#!/usr/bin/env python3
"""Show an approval and re-verify it against the current decision and policy.

    python3 scripts/show_approval.py <decision_id>

Read-only.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.approval import fingerprint, get_approval, policy_fingerprint, verify_approval  # noqa: E402
from src.decision_logger import read_decisions  # noqa: E402
from src.execution_store import load_executions  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Show and re-verify an approval")
    parser.add_argument("decision_id")
    args = parser.parse_args()

    approval = get_approval(args.decision_id)
    if approval is None:
        print("No approval exists for %s." % args.decision_id)
        print("A BUY recommendation is not permission to execute.")
        return 1

    decision = None
    for record in read_decisions():
        if record.get("decision_id") == args.decision_id:
            decision = record
            break

    executions = load_executions()
    record = executions.get(args.decision_id)

    print("=" * 72)
    print("APPROVAL %s" % approval.approval_id)
    print("=" * 72)
    for key, value in approval.to_dict().items():
        print("  %-22s %s" % (key, value))
    print("-" * 72)
    print("  execution state        %s" % (record.state if record else "PROPOSED"))
    print("  current policy sha     %s" % policy_fingerprint()[:32])
    if decision is not None:
        print("  current decision sha   %s" % fingerprint(decision))
        verdict = verify_approval(approval, decision)
        print("-" * 72)
        print("  RE-VERIFICATION: %s" % ("VALID" if verdict.ok else "INVALID"))
        for code, message in verdict.blockers:
            print("    x [%s] %s" % (code, message))
    else:
        print("  decision not found in the log; cannot re-verify")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
