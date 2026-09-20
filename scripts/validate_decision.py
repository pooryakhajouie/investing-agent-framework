#!/usr/bin/env python3
"""Run a proposed decision through the deterministic guardrails.

This is the authoritative gate. A decision that does not pass here is invalid,
regardless of how confident the model was.

    # validate a decision file
    python3 scripts/validate_decision.py decision.json

    # validate from stdin
    cat decision.json | python3 scripts/validate_decision.py -

    # validate AND append to logs/decisions.jsonl (still dry run — no order)
    python3 scripts/validate_decision.py decision.json --log

    # built-in demonstration against fake data (touches nothing real)
    python3 scripts/validate_decision.py --demo
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import guardrails  # noqa: E402
from src.decision_logger import build_record, log_decision, new_decision_id  # noqa: E402
from src.models import DECISION_BUY, ZERO, usd  # noqa: E402
from src.state import (  # noqa: E402
    DEFAULT_LAST_EVAL_PATH,
    BudgetState,
    ConfigError,
    CorruptStateError,
    current_month,
    load_budget_state,
    load_config,
    save_last_evaluation,
)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(result, proposal, state) -> str:
    lines = []
    lines.append("=" * 68)
    lines.append("GUARDRAIL VALIDATION — %s" % ("PASSED" if result.valid else "REJECTED"))
    lines.append("=" * 68)
    lines.append("decision_id      : %s" % (proposal.decision_id or "(missing)"))
    lines.append("decision         : %s" % (proposal.decision or "(missing)"))
    if proposal.decision == DECISION_BUY or proposal.proposed_amount_usd != ZERO:
        lines.append("ticker           : %s" % (proposal.ticker or "(none)"))
        lines.append("proposed amount  : %s" % usd(proposal.proposed_amount_usd))
    lines.append("month            : %s" % state.month)
    lines.append("authorized       : %s" % usd(state.authorized_budget_usd))
    lines.append("already committed: %s" % usd(state.committed_usd))
    lines.append("remaining        : %s" % usd(state.remaining_usd))
    lines.append("-" * 68)
    lines.append("checks run (%d): %s" % (len(result.checks_run), ", ".join(result.checks_run)))
    if result.violations:
        lines.append("")
        lines.append("VIOLATIONS (%d) — this proposal is INVALID:" % len(result.violations))
        for violation in result.violations:
            lines.append("  ✗ [%s] %s" % (violation.code, violation.message))
    if result.warnings:
        lines.append("")
        lines.append("WARNINGS (%d) — not blocking, but the thesis must address them:" % len(result.warnings))
        for warning in result.warnings:
            lines.append("  ! %s" % warning)
    lines.append("-" * 68)
    lines.append("executable       : %s" % result.executable)
    lines.append("execution_status : %s" % result.execution_status)
    lines.append("=" * 68)
    return "\n".join(lines)


def summary_block(result, proposal, state) -> str:
    """The short human-facing summary the evaluation prompt asks for."""
    if not result.valid:
        return (
            "DECISION: REJECTED BY GUARDRAILS\n"
            "Remaining monthly authorization: %s\n"
            "Reason: %s" % (usd(state.remaining_usd), "; ".join(result.violation_codes))
        )
    if proposal.decision == DECISION_BUY:
        return (
            "DECISION: BUY\n"
            "Ticker: %s\n"
            "Amount: %s\n"
            "Reason: %s\n"
            "Remaining monthly authorization: %s\n"
            "(DRY RUN — no order was placed.)"
            % (
                proposal.ticker,
                usd(proposal.proposed_amount_usd),
                str(proposal.raw.get("thesis", ""))[:300],
                usd(state.remaining_usd - proposal.proposed_amount_usd),
            )
        )
    return (
        "DECISION: WAIT\n"
        "Remaining monthly authorization: %s\n"
        "Reason: %s" % (usd(state.remaining_usd), str(proposal.raw.get("thesis", ""))[:300])
    )


# --------------------------------------------------------------------------
# Demo
# --------------------------------------------------------------------------

_DEMO_NARRATIVE = {
    "thesis": "FAKE DEMO DATA. Broad, low-cost, non-leveraged index exposure is the "
              "default long-term holding when nothing else clears the bar.",
    "value_creation": "FAKE DEMO DATA. Aggregate earnings growth of the underlying index.",
    "valuation_reasoning": "FAKE DEMO DATA. Index multiple near its own long-run median.",
    "timing_reason": "FAKE DEMO DATA. No company-specific event risk applies to a broad index.",
    "alternatives_considered": [
        {"symbol": "ALTA", "provenance": "OPEN_DISCOVERY", "why_not": "FAKE. Higher expense ratio."},
        {"symbol": "BTC-USD", "provenance": "CURRENT_HOLDING", "why_not": "FAKE. Already a large exposure."},
    ],
    "portfolio_exposure": "FAKE DEMO DATA. This theme is currently a small share of the portfolio.",
    "risks": "FAKE DEMO DATA. Broad market drawdown risk.",
    "invalidation": "FAKE DEMO DATA. A permanent change in index construction.",
    "evidence": ["FAKE DEMO DATA - not retrieved from any tool"],
}

DEMO_BUY = dict(
    {
        "decision_id": "demo-buy-0001",
        "decision": "BUY",
        "action": "buy",
        "side": "buy",
        "asset_class": "ETF",
        "position_type": "NEW_POSITION",
        "classification": "PROMISING",
        "ticker": "EXMPL",
        "security_name": "Example Broad Market Index ETF",
        "asset_type": "us_etf",
        "exchange": "NYSEARCA",
        "proposed_amount_usd": "25.00",
        "current_price_usd": "412.50",
        "quote_timestamp": "2099-01-15T15:30:00Z",
        "fractional_eligible": True,
        "investment_horizon_months": 60,
        "monthly_budget_before_usd": "25.00",
        "monthly_budget_after_usd": "0.00",
        "confidence": "MEDIUM",
    },
    **_DEMO_NARRATIVE
)

DEMO_CRYPTO = dict(
    {
        "decision_id": "demo-crypto-0001",
        "decision": "BUY",
        "action": "buy",
        "side": "buy",
        "asset_class": "CRYPTO",
        "position_type": "NEW_POSITION",
        "classification": "PROMISING",
        "ticker": "BTC-USD",
        "security_name": "Bitcoin",
        "asset_type": "crypto",
        "proposed_amount_usd": "25.00",
        "current_price_usd": "80000.00",
        "quote_timestamp": "2099-01-15T15:30:00Z",
        "investment_horizon_months": 60,
        "monthly_budget_before_usd": "25.00",
        "monthly_budget_after_usd": "0.00",
        "confidence": "MEDIUM",
    },
    **_DEMO_NARRATIVE
)

DEMO_CASES = [
    ("A. Full $25.00 equity buy from an untouched budget", DEMO_BUY, {}),
    ("B. $25.01 - one cent over the monthly authorization",
     DEMO_BUY, {"decision_id": "demo-over", "proposed_amount_usd": "25.01",
                "monthly_budget_after_usd": "-0.01"}),
    ("C. A SELL, which is never permitted",
     DEMO_BUY, {"decision_id": "demo-sell", "side": "sell", "action": "sell"}),
    ("D. A leveraged ETF (3x), identified by ticker and name",
     DEMO_BUY, {"decision_id": "demo-lev", "ticker": "TQQQ",
                "security_name": "ProShares UltraPro QQQ 3X Shares"}),
    ("E. An options position",
     DEMO_BUY, {"decision_id": "demo-opt", "asset_type": "option", "uses_options": True,
                "strike_price": "400", "expiration_date": "2099-06-19"}),
    ("F. Margin",
     DEMO_BUY, {"decision_id": "demo-margin", "uses_margin": True}),
    ("G. A zero-dollar buy",
     DEMO_BUY, {"decision_id": "demo-zero", "proposed_amount_usd": "0.00",
                "monthly_budget_after_usd": "25.00"}),
    ("H. A negative amount",
     DEMO_BUY, {"decision_id": "demo-neg", "proposed_amount_usd": "-5.00",
                "monthly_budget_after_usd": "30.00"}),
    ("I. A penny stock at $0.42",
     DEMO_BUY, {"decision_id": "demo-penny", "ticker": "PNNY", "asset_type": "us_common_stock",
                "asset_class": "EQUITY", "current_price_usd": "0.42", "exchange": "NASDAQ",
                "security_name": "Penny Example Corp"}),
    ("J. A 6-month holding period (this is not a trading agent)",
     DEMO_BUY, {"decision_id": "demo-short", "investment_horizon_months": 6}),
    ("K. A buy the model itself classified TOO_EXPENSIVE",
     DEMO_BUY, {"decision_id": "demo-contra", "classification": "TOO_EXPENSIVE"}),
    ("L. Supported crypto (BTC-USD) within budget", DEMO_CRYPTO, {}),
    ("M. A crypto pair Robinhood does not support",
     DEMO_CRYPTO, {"decision_id": "demo-badcoin", "ticker": "MOONCOIN-USD",
                   "security_name": "Mooncoin", "current_price_usd": "0.05"}),
    ("N. A halted crypto pair",
     DEMO_CRYPTO, {"decision_id": "demo-halted", "ticker": "TRUMP-USD",
                   "security_name": "OFFICIAL TRUMP", "current_price_usd": "2.30"}),
    ("O. A crypto SELL",
     DEMO_CRYPTO, {"decision_id": "demo-crsell", "side": "sell"}),
    ("P. Adding to a holding while misstating the cost-basis direction",
     DEMO_BUY, {"decision_id": "demo-avg", "position_type": "EXISTING_POSITION",
                "classification": "ADD_CANDIDATE", "current_price_usd": "412.50",
                "cost_basis_analysis": {"average_cost_usd": "300.00",
                                        "raises_or_lowers_average": "LOWERS",
                                        "economic_meaning": "FAKE DEMO DATA."}}),
    ("Q. A well-formed WAIT",
     {"decision_id": "demo-wait", "decision": "WAIT", "confidence": "MEDIUM",
      "monthly_budget_remaining_usd": "25.00",
      "thesis": "FAKE DEMO DATA. No candidate currently clears the quality-and-price bar.",
      "timing_reason": "FAKE DEMO DATA. Re-evaluate after the next round of earnings.",
      "alternatives_considered": [{"symbol": "EXMPL", "why_not": "FAKE DEMO DATA."}],
      "best_existing_position_candidate": {"symbol": "EXMPL", "why_not": "FAKE DEMO DATA."},
      "best_new_equity_candidate": {"symbol": "NEWCO", "why_not": "FAKE DEMO DATA."},
      "best_crypto_candidate": {"symbol": "ETH-USD", "why_not": "FAKE DEMO DATA."},
      "evidence": ["FAKE DEMO DATA - not retrieved from any tool"]}, {}),
    ("R. A WAIT that skips the crypto bucket",
     {"decision_id": "demo-wait-thin", "decision": "WAIT", "confidence": "MEDIUM",
      "monthly_budget_remaining_usd": "25.00",
      "thesis": "FAKE DEMO DATA.", "timing_reason": "FAKE DEMO DATA.",
      "alternatives_considered": [{"symbol": "EXMPL", "why_not": "FAKE."}],
      "best_existing_position_candidate": {"symbol": "EXMPL", "why_not": "FAKE."},
      "best_new_equity_candidate": {"symbol": "NEWCO", "why_not": "FAKE."},
      "evidence": ["FAKE DEMO DATA"]}, {}),
]


def run_demo(config) -> int:
    """Exercise the validator against fabricated data. Touches no real state."""
    state = BudgetState(
        month=current_month(),
        authorized_budget_usd=config.monthly_budget_usd,
        committed_usd=ZERO,
        acted_decision_ids=["demo-already-used"],
        last_updated="",
        path="<in-memory demo state>",
    )

    print("#" * 68)
    print("# GUARDRAIL DEMONSTRATION — ALL DATA BELOW IS FABRICATED.")
    print("# No Robinhood call is made. No file is written. No order is placed.")
    print("#" * 68)
    print()
    print("Demo budget state: %s authorized for %s, %s committed, %s remaining."
          % (usd(state.authorized_budget_usd), state.month,
             usd(state.committed_usd), usd(state.remaining_usd)))
    print()

    failures = 0
    for title, base, override in DEMO_CASES:
        raw = copy.deepcopy(base)
        raw.update(override)
        proposal, _ = guardrails.normalize(raw)
        result = guardrails.validate(raw, config, state)
        verdict = "PASS" if result.valid else "REJECT"
        print("%-58s -> %s" % (title, verdict))
        if not result.valid:
            for violation in result.violations:
                print("        ✗ %s: %s" % (violation.code, violation.message))
        for warning in result.warnings:
            print("        ! %s" % warning)
        print()

    # Two purchases summing to exactly the budget, then one cent more.
    print("-" * 68)
    print("Cumulative budget walk across asset classes (fabricated):")
    half = (config.monthly_budget_usd / 2).quantize(Decimal("0.01"))
    walk_state = state
    for index, amount in enumerate([half, config.monthly_budget_usd - half, Decimal("0.01")]):
        # Alternate equity / crypto to show the $25 is ONE shared authorization.
        raw = copy.deepcopy(DEMO_BUY if index % 2 == 0 else DEMO_CRYPTO)
        raw["decision_id"] = "demo-walk-%d" % index
        raw["proposed_amount_usd"] = str(amount)
        raw["monthly_budget_before_usd"] = str(walk_state.remaining_usd)
        raw["monthly_budget_after_usd"] = str(walk_state.remaining_usd - amount)
        result = guardrails.validate(raw, config, walk_state)
        print("  %-6s purchase %s with %s remaining -> %s"
              % (raw["asset_class"], usd(amount), usd(walk_state.remaining_usd),
                 "PASS" if result.valid else "REJECT"))
        for violation in result.violations:
            print("        ✗ %s: %s" % (violation.code, violation.message))
        if result.valid:
            from src.state import commit_purchase

            walk_state = commit_purchase(walk_state, raw["decision_id"], amount)

    print("-" * 68)
    print("Replay protection: re-validating an already-acted decision id.")
    raw = copy.deepcopy(DEMO_BUY)
    raw["decision_id"] = "demo-already-used"
    result = guardrails.validate(raw, config, state)
    print("  -> %s (%s)" % ("PASS" if result.valid else "REJECT", ", ".join(result.violation_codes)))

    print("-" * 68)
    print("Execution gate:")
    try:
        guardrails.assert_execution_allowed(config)
        print("  !! assert_execution_allowed did NOT raise — this is a bug.")
        failures += 1
    except guardrails.LiveTradingDisabled as exc:
        print("  assert_execution_allowed raised as expected: %s" % exc)

    print("=" * 68)
    print("Demo complete. live_trading=%s. No order was placed." % config.live_trading)
    return 1 if failures else 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a proposed investment decision")
    parser.add_argument("decision_file", nargs="?", help="path to a decision JSON file, or '-' for stdin")
    parser.add_argument("--log", action="store_true", help="append the result to logs/decisions.jsonl")
    parser.add_argument("--demo", action="store_true", help="run the fabricated-data demonstration")
    parser.add_argument("--json", action="store_true", help="emit the validation result as JSON")
    parser.add_argument("--new-id", action="store_true", help="print a fresh decision id and exit")
    args = parser.parse_args()

    if args.new_id:
        print(new_decision_id())
        return 0

    try:
        config = load_config()
    except ConfigError as exc:
        print("CONFIG ERROR: %s" % exc, file=sys.stderr)
        return 2

    if config.live_trading:
        print(
            "REFUSING TO RUN: config.live_trading is true. Version 1 is dry-run only.",
            file=sys.stderr,
        )
        return 4

    if args.demo:
        return run_demo(config)

    if not args.decision_file:
        parser.error("provide a decision file, '-' for stdin, or --demo")

    if args.decision_file == "-":
        text = sys.stdin.read()
    else:
        try:
            with open(args.decision_file, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            print("Could not read %s: %s" % (args.decision_file, exc), file=sys.stderr)
            return 2

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        print("Decision file is not valid JSON: %s" % exc, file=sys.stderr)
        return 2

    try:
        state = load_budget_state(config)
    except CorruptStateError as exc:
        print("STATE ERROR (failing closed): %s" % exc, file=sys.stderr)
        return 3

    proposal, _ = guardrails.normalize(raw)
    result = guardrails.validate(raw, config, state)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(render(result, proposal, state))

    if args.log:
        record = build_record(proposal, result, state, raw_decision=raw)
        log_decision(record)
        save_last_evaluation(
            {
                "schema_version": 1,
                "status": "COMPLETED",
                "decision_id": record.get("decision_id"),
                "timestamp": record.get("timestamp"),
                "month": state.month,
                "decision": record.get("decision"),
                "ticker": record.get("ticker"),
                "proposed_amount": record.get("proposed_amount"),
                "monthly_budget_remaining": record.get("monthly_budget_before"),
                "validation_valid": result.valid,
                "violations": result.violation_codes,
                "execution_status": result.execution_status,
            },
            DEFAULT_LAST_EVAL_PATH,
        )
        print("\nLogged to logs/decisions.jsonl and state/last_evaluation.json.")
        print("Budget state was NOT changed: nothing was executed (dry run).")

    print()
    print(summary_block(result, proposal, state))
    return 0 if result.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
