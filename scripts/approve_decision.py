#!/usr/bin/env python3
"""Approve ONE exact decision. Human-only.

    python3 scripts/approve_decision.py <decision_id>

This is the ONLY path that creates an approval. It is deliberately separate from
every evaluation code path.

**Claude must never run this script.** CLAUDE.md forbids it. Be honest about
what that control is worth: nothing in a local repository can cryptographically
prove that a human, rather than an agent, typed at the keyboard. The protections
are layered instead —

  1. this script refuses to run without a TTY unless --i-am-not-a-tty is passed,
     which is itself recorded loudly in the audit log;
  2. it requires a verbatim challenge phrase containing the ticker and amount;
  3. every approval is fingerprinted and audit-logged;
  4. and execution is disabled regardless, by three independent switches.

The last one is the protection that actually holds today.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.approval import (
    challenge_phrase,  # noqa: E402
    ApprovalError,
    create_approval,
    fingerprint,
    load_approvals,
    policy_fingerprint,
    save_approvals,
    verify_approval,
)
from src.decision_logger import read_decisions  # noqa: E402
from src.execution import InvalidTransition  # noqa: E402
from src.execution_store import (  # noqa: E402
    EVENT_APPROVAL_GRANTED,
    EVENT_APPROVAL_REVOKED,
    ExecutionRecord,
    ExecutionState,
    load_executions,
    plan_transitions_to_approved,
    save_executions,
    transition,
    write_audit,
)
from src.guardrails import validate  # noqa: E402
from src.models import usd, parse_money  # noqa: E402
from src.state import current_month, load_budget_state, load_config  # noqa: E402


def find_decision(decision_id, path=None):
    for record in read_decisions() if path is None else json.load(open(path)):
        if isinstance(record, dict) and record.get("decision_id") == decision_id:
            return record
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Approve one exact decision")
    parser.add_argument("decision_id")
    parser.add_argument("--decision-file", help="approve from a JSON file instead of the log")
    parser.add_argument("--ttl-hours", type=int, default=None)
    parser.add_argument("--i-am-not-a-tty", action="store_true",
                        help="allow approval without a terminal (recorded in the audit log)")
    args = parser.parse_args()

    config = load_config()
    state = load_budget_state(config)

    if args.decision_file:
        with open(args.decision_file, encoding="utf-8") as handle:
            decision = json.load(handle)
        if decision.get("decision_id") != args.decision_id:
            print("decision file is for %s, not %s" % (decision.get("decision_id"), args.decision_id),
                  file=sys.stderr)
            return 2
    else:
        decision = find_decision(args.decision_id)

    if decision is None:
        print("No decision %s found in logs/decisions.jsonl." % args.decision_id, file=sys.stderr)
        return 2

    if decision.get("decision") != "BUY":
        print("Only a BUY can be approved; %s is a %s." % (args.decision_id, decision.get("decision")),
              file=sys.stderr)
        return 2

    result = validate(decision, config, state)
    if not result.valid:
        print("REFUSING: this decision does not pass the guardrails:", file=sys.stderr)
        for violation in result.violations:
            print("  x [%s] %s" % (violation.code, violation.message), file=sys.stderr)
        return 3

    amount = parse_money(decision.get("proposed_amount_usd", decision.get("proposed_amount", "0")))
    ticker = str(decision.get("ticker") or decision.get("symbol") or "").upper()

    print("=" * 72)
    print("YOU ARE APPROVING ONE PURCHASE")
    print("=" * 72)
    print("  Action           : BUY")
    print("  Asset            : %s  (%s)" % (ticker, decision.get("security_name") or ""))
    print("  Asset class      : %s / %s" % (decision.get("asset_class"), decision.get("asset_type")))
    print("  Position         : %s" % decision.get("position_type"))
    # Not "MAXIMUM": the fingerprint binds proposed_amount_usd exactly, so a
    # decision for any other amount — smaller included — fails as
    # DECISION_MODIFIED. Calling it a maximum invited the reasonable but wrong
    # belief that a cheaper purchase was covered by this approval.
    print("  EXACT amount     : %s" % usd(amount))
    print("  Calendar month   : %s" % state.month)
    print("  Remaining auth.  : %s  ->  %s after"
          % (usd(state.remaining_usd), usd(state.remaining_usd - amount)))
    print("  Decision ID      : %s" % args.decision_id)
    print("  Payload SHA-256  : %s" % fingerprint(decision))
    print("  Policy SHA-256   : %s" % policy_fingerprint()[:32])
    print("  Classification   : %s   confidence %s"
          % (decision.get("classification"), decision.get("confidence")))
    print("-" * 72)
    print("  Thesis: %s" % (str(decision.get("thesis") or "")[:400]))
    print("-" * 72)
    print("  This approval authorizes ONLY this decision id, this asset, this asset")
    print("  class, and AT MOST this dollar amount, in this calendar month. Any change")
    print("  to the decision voids it. It expires within 24 hours.")
    print("=" * 72)

    # ------------------------------------------------------------------
    # Establish that the ledger CAN accept this approval before anything is
    # written, and tell the human what will happen to any prior approval.
    # Persisting an approval the state machine would then reject is what left
    # state/approvals.json and state/executions.json disagreeing.
    # ------------------------------------------------------------------
    executions = load_executions()
    record = executions.get(args.decision_id)
    prior_state = record.state if record else ExecutionState.PROPOSED

    try:
        planned_path = plan_transitions_to_approved(prior_state)
    except InvalidTransition as exc:
        print("REFUSING: %s cannot be approved from state %s.\n  %s"
              % (args.decision_id, prior_state, exc), file=sys.stderr)
        return 6

    prior_approval = load_approvals().get(args.decision_id)
    prior_verdict = None
    if prior_approval is not None:
        prior_verdict = verify_approval(prior_approval, decision)

    print("  Ledger state     : %s" % prior_state)
    if prior_approval is not None:
        if prior_verdict is not None and prior_verdict.ok:
            print("  Prior approval   : %s — STILL VALID; it will be REPLACED"
                  % prior_approval.approval_id)
        else:
            codes = ", ".join(prior_verdict.codes) if prior_verdict else "UNVERIFIABLE"
            print("  Prior approval   : %s — ALREADY INVALID (%s)"
                  % (prior_approval.approval_id, codes))
    if len(planned_path) > 1:
        print("  Re-approval path : %s -> %s" % (prior_state, " -> ".join(planned_path)))
    print("=" * 72)

    challenge = challenge_phrase(ticker, amount)
    if not sys.stdin.isatty() and not args.i_am_not_a_tty:
        print("\nRefusing: no terminal detected. Approval must be typed by a person.\n"
              "If you genuinely need non-interactive approval, pass --i-am-not-a-tty; "
              "it will be recorded in the audit log.", file=sys.stderr)
        return 4

    print("\nType exactly:  %s" % challenge)
    try:
        typed = input("> ").strip()
    except EOFError:
        typed = ""
    if typed != challenge:
        print("Phrase did not match. No approval was created.", file=sys.stderr)
        return 5

    try:
        approval = create_approval(
            decision,
            approved_by="local-cli%s" % ("-non-tty" if args.i_am_not_a_tty else ""),
            ttl_hours=args.ttl_hours,
        )
    except ApprovalError as exc:
        print("APPROVAL ERROR: %s" % exc, file=sys.stderr)
        return 3

    if record is None:
        record = ExecutionRecord(
            decision_id=approval.decision_id,
            decision_fingerprint=approval.decision_fingerprint,
            month=approval.month,
            asset=approval.asset,
            amount_usd=str(approval.max_amount_usd),
        )

    # Apply the whole planned path in memory first. An APPROVED record that is
    # being re-approved passes through REAPPROVAL_REQUIRED, so the history keeps
    # an explicit record that the earlier approval was invalidated rather than
    # silently overwritten.
    for target in planned_path:
        if target == ExecutionState.REAPPROVAL_REQUIRED:
            if prior_verdict is not None and not prior_verdict.ok:
                why = "prior approval %s invalidated (%s)" % (
                    prior_approval.approval_id, ", ".join(prior_verdict.codes))
            elif prior_approval is not None:
                why = "prior approval %s superseded by a new approval" % prior_approval.approval_id
            else:
                why = "record was APPROVED with no approval on file"
            transition(record, target, reason=why, approval_id=None)
            write_audit(
                EVENT_APPROVAL_REVOKED,
                decision_id=approval.decision_id,
                detail={
                    "revoked_approval_id": getattr(prior_approval, "approval_id", None),
                    "reason": why,
                    "codes": prior_verdict.codes if prior_verdict else [],
                },
            )
        else:
            transition(record, target, reason="approved via CLI",
                       approval_id=approval.approval_id)

    # Ledger first, then the approval artifact. Either half-write fails closed:
    # an APPROVED record with no approval verifies as NOT_APPROVED, and an
    # approval whose record is not APPROVED cannot reach preflight.
    executions[approval.decision_id] = record
    save_executions(executions)

    approvals = load_approvals()
    approvals[approval.decision_id] = approval
    save_approvals(approvals)

    write_audit(
        EVENT_APPROVAL_GRANTED,
        decision_id=approval.decision_id,
        detail={
            "approval_id": approval.approval_id,
            "asset": approval.asset,
            "max_amount_usd": str(approval.max_amount_usd),
            "expires_at": approval.expires_at,
            "decision_fingerprint": approval.decision_fingerprint,
            "non_interactive": bool(args.i_am_not_a_tty),
        },
    )

    print("\nAPPROVED. approval_id=%s expires=%s" % (approval.approval_id, approval.expires_at))
    print("Execution remains disabled (execution_mode=%s, agent_enabled=%s)."
          % (config.execution_mode, config.agent_enabled))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
