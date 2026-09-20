#!/usr/bin/env python3
"""Run an allocation plan through the deterministic guardrails.

Stage 7. A plan is one of WAIT, SINGLE_BUY or SPLIT_BUY_PLAN. Every BUY leg
inside it is validated by the ordinary single-decision validator, against a
budget state that already reflects the legs ahead of it.

    python3 scripts/validate_plan.py plan.json
    cat plan.json | python3 scripts/validate_plan.py -
    python3 scripts/validate_plan.py plan.json --log

``--log`` appends every leg to logs/decisions.jsonl as an ordinary decision,
each keeping its own decision_id. It changes no budget state and executes
nothing: there is no order path in this repository.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.allocation import cumulative_state, validate_plan  # noqa: E402
from src.decision_logger import build_record, log_decision  # noqa: E402
from src.guardrails import normalize  # noqa: E402
from src.models import PLAN_WAIT, ValidationResult, usd  # noqa: E402
from src.state import (  # noqa: E402
    ConfigError,
    CorruptStateError,
    load_budget_state,
    load_config,
    save_last_evaluation,
)


def _as_validation(plan_result) -> ValidationResult:
    """Adapt a PlanValidation to the ValidationResult build_record expects."""
    return ValidationResult(
        valid=plan_result.valid,
        executable=False,
        violations=list(plan_result.violations),
        warnings=list(plan_result.warnings),
        checks_run=list(plan_result.checks_run),
        execution_status=plan_result.execution_status,
    )


def render(result) -> str:
    lines = []
    lines.append("=" * 68)
    lines.append("PLAN VALIDATION — %s" % ("PASSED" if result.valid else "REJECTED"))
    lines.append("=" * 68)
    lines.append("plan_id          : %s" % (result.plan_id or "(missing)"))
    lines.append("plan_type        : %s" % (result.plan_type or "(missing)"))
    lines.append("legs             : %d" % result.leg_count)
    lines.append("combined amount  : %s" % usd(result.total_amount_usd))
    lines.append("remaining before : %s" % usd(result.remaining_before_usd))
    lines.append("remaining after  : %s" % usd(result.remaining_after_usd))
    if result.legs:
        lines.append("-" * 68)
        lines.append("LEGS")
        for leg in result.legs:
            lines.append(
                "  [%d] %-10s %-8s %-18s %s  %s"
                % (
                    leg.index,
                    leg.ticker or "?",
                    leg.asset_class or "?",
                    leg.position_type or "?",
                    usd(leg.amount_usd),
                    "ok" if leg.valid else "REJECTED",
                )
            )
            lines.append("        decision_id: %s" % (leg.decision_id or "(missing)"))
            lines.append(
                "        checked against %s already committed by earlier legs"
                % usd(leg.preceding_committed_usd)
            )
    lines.append("-" * 68)
    lines.append("plan checks (%d)" % len(result.checks_run))
    if result.violations:
        lines.append("")
        lines.append("VIOLATIONS (%d) — this plan is INVALID:" % len(result.violations))
        for violation in result.violations:
            lines.append("  x [%s] %s" % (violation.code, violation.message))
    if result.warnings:
        lines.append("")
        lines.append("WARNINGS (%d):" % len(result.warnings))
        for warning in result.warnings:
            lines.append("  ! %s" % warning)
    lines.append("-" * 68)
    lines.append("executable       : %s" % result.executable)
    lines.append("execution_status : %s" % result.execution_status)
    lines.append("=" * 68)
    return "\n".join(lines)


def summary_block(result, plan) -> str:
    """The short human-facing summary, in the shapes CLAUDE.md section 11 allows."""
    if not result.valid:
        return (
            "DECISION: REJECTED BY GUARDRAILS\n"
            "Remaining monthly authorization: %s\n"
            "Reason: %s" % (usd(result.remaining_before_usd),
                            "; ".join(sorted(set(result.violation_codes))))
        )
    if result.plan_type == PLAN_WAIT:
        thesis = str((plan.get("decision") or {}).get("thesis", ""))[:300]
        return (
            "DECISION: WAIT\n"
            "Remaining monthly authorization: %s\n"
            "Reason: %s\n"
            "(DRY RUN — no order was placed.)" % (usd(result.remaining_before_usd), thesis)
        )

    lines = ["DECISION: %s" % result.plan_type]
    for leg in result.legs:
        lines.append(
            "Asset: %s (%s, %s)  Amount: %s  decision_id: %s"
            % (leg.ticker, leg.asset_class, leg.position_type,
               usd(leg.amount_usd), leg.decision_id)
        )
    lines.append("Combined amount: %s" % usd(result.total_amount_usd))
    lines.append("Remaining monthly authorization: %s" % usd(result.remaining_after_usd))
    lines.append("(DRY RUN — no order was placed. Each leg needs its own approval.)")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", help="path to a plan JSON file, or - for stdin")
    parser.add_argument(
        "--log", action="store_true",
        help="append each leg to logs/decisions.jsonl (still dry run; no order)")
    parser.add_argument("--json", action="store_true", help="emit the raw result as JSON")
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.read() if args.plan == "-" else open(args.plan).read()
        plan = json.loads(raw)
    except (OSError, ValueError) as exc:
        print("could not read plan: %s" % exc, file=sys.stderr)
        return 2

    try:
        config = load_config()
        state = load_budget_state(config)
    except (ConfigError, CorruptStateError) as exc:
        print("state error (failing closed): %s" % exc, file=sys.stderr)
        return 2

    result = validate_plan(plan, config, state)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(render(result))
        print()
        print(summary_block(result, plan))

    if args.log:
        if not result.valid:
            print("\nNOT logged: the plan is invalid.", file=sys.stderr)
            return 1
        logged = []
        if result.plan_type == PLAN_WAIT:
            decision = dict(plan.get("decision") or {})
            decision["plan_id"] = result.plan_id
            decision["plan_type"] = result.plan_type
            proposal, _ = normalize(decision)
            wait_result = validate_plan({"plan_type": PLAN_WAIT,
                                         "plan_id": result.plan_id,
                                         "decision": decision}, config, state)
            record = build_record(proposal, _as_validation(wait_result), state, decision)
            log_decision(record)
            logged.append(proposal.decision_id)
        else:
            # Each leg is logged as an ordinary decision, keeping its own
            # decision_id, and its budget arithmetic is computed against what
            # the legs ahead of it already consumed.
            for leg, raw_leg in zip(result.legs, plan.get("legs", [])):
                decision = dict(raw_leg)
                decision["plan_id"] = result.plan_id
                decision["plan_type"] = result.plan_type
                decision["plan_leg_index"] = leg.index
                decision["plan_leg_count"] = result.leg_count
                proposal, _ = normalize(decision)
                leg_state = cumulative_state(state, leg.preceding_committed_usd)
                record = build_record(proposal, leg.result, leg_state, decision)
                log_decision(record)
                logged.append(leg.decision_id)
        save_last_evaluation({
            "schema_version": 1,
            "status": "COMPLETED",
            "plan_id": result.plan_id,
            "plan_type": result.plan_type,
            "month": state.month,
            "decision_ids": logged,
            "proposed_amount": str(result.total_amount_usd),
            "monthly_budget_remaining": str(result.remaining_after_usd),
            "validation_valid": result.valid,
            "execution_status": result.execution_status,
        })
        print("\nLogged %d leg(s) to logs/decisions.jsonl: %s" % (len(logged), ", ".join(
            str(x) for x in logged)))
        print("Budget state was NOT changed: nothing was executed (dry run).")
        print("Each leg still requires its own separate human approval.")

    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
