#!/usr/bin/env python3
"""Run every pre-execution check for an approved decision — then refuse.

    python3 scripts/execute_approved.py <decision_id> [--snapshot broker.json]

This is the future executor. Today it always ends in EXECUTION REFUSED, because
all three switches are closed and `src.execution.execute()` raises
unconditionally. Running it is safe: it places nothing, previews nothing, and
calls no Robinhood tool.

The broker snapshot is supplied as a JSON file rather than fetched here, so that
the executor itself performs no broker I/O. A future live runner would gather it
with READ-ONLY tools (get_accounts, get_portfolio, get_equity_orders,
get_crypto_orders, get_equity_quotes / get_crypto_quotes, get_equity_tradability
/ get_currency_pairs) and write it to that file.

Omitting the snapshot is not a shortcut: it fails closed as BROKER_UNREADABLE.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.approval import get_approval  # noqa: E402
from src.decision_logger import read_decisions  # noqa: E402
from src.execution import (  # noqa: E402
    BrokerSnapshot,
    ExecutionDisabled,
    ExecutionState,
    build_order_request,
    execute,
    execution_gate_blockers,
    preflight,
)
from src.execution_store import (  # noqa: E402
    EVENT_EXECUTION_REFUSED,
    EVENT_PREFLIGHT,
    ExecutionRecord,
    load_executions,
    save_executions,
    transition,
    write_audit,
)
from src.models import money_str, parse_money, usd  # noqa: E402
from src.state import load_budget_state, load_config  # noqa: E402


def _dt(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _dec(value):
    return None if value in (None, "") else parse_money(value)


def load_snapshot(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return BrokerSnapshot(
        as_of=_dt(raw.get("as_of")) or datetime.now(timezone.utc),
        account_is_agentic=bool(raw.get("account_is_agentic")),
        account_masked=str(raw.get("account_masked") or "????"),
        equity_orders=raw.get("equity_orders"),
        crypto_orders=raw.get("crypto_orders"),
        buying_power_usd=_dec(raw.get("buying_power_usd")),
        quote_price_usd=_dec(raw.get("quote_price_usd")),
        quote_timestamp=_dt(raw.get("quote_timestamp")),
        tradable=raw.get("tradable"),
        fractional_tradable=raw.get("fractional_tradable"),
        account_type_tradable=raw.get("account_type_tradable"),
        crypto_pair_halted=raw.get("crypto_pair_halted"),
        crypto_min_order_size=_dec(raw.get("crypto_min_order_size")),
        read_errors=list(raw.get("read_errors") or []),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-execution checks (execution disabled)")
    parser.add_argument("decision_id")
    parser.add_argument("--snapshot", help="path to a read-only broker snapshot JSON")
    args = parser.parse_args()

    config = load_config()
    budget_state = load_budget_state(config)

    decision = None
    for record in read_decisions():
        if record.get("decision_id") == args.decision_id:
            decision = record
            break
    if decision is None:
        print("No decision %s found." % args.decision_id, file=sys.stderr)
        return 2

    approval = get_approval(args.decision_id)
    executions = load_executions()
    record = executions.get(args.decision_id)
    state_name = record.state if record else ExecutionState.PROPOSED

    snapshot = load_snapshot(args.snapshot)
    result = preflight(decision, approval, config, budget_state, snapshot,
                       execution_state=state_name)

    print("=" * 72)
    print("PRE-EXECUTION CHECKS — %s" % args.decision_id)
    print("=" * 72)
    print("  execution_mode  : %s" % config.execution_mode)
    print("  agent_enabled   : %s" % config.agent_enabled)
    print("  live_trading    : %s" % config.live_trading)
    print("  execution state : %s" % state_name)
    print("  approval        : %s" % (approval.approval_id if approval else "NONE"))
    print("-" * 72)
    print("  checks run (%d)" % len(result.checks))
    if result.reconciliation:
        rec = result.reconciliation
        print("  reconciled      : filled %s + pending %s -> max additional order %s"
              % (usd(rec.broker_filled_usd), usd(rec.broker_pending_usd),
                 usd(rec.max_additional_order_usd)))
    if result.blockers:
        print("\n  BLOCKERS (%d):" % len(result.blockers))
        for code, message in result.blockers:
            print("    x [%s] %s" % (code, message))
    for warning in result.warnings:
        print("    ! %s" % warning)
    print("-" * 72)
    print("  preflight ok    : %s" % result.ok)
    print("  next state      : %s" % result.next_state)
    print("=" * 72)

    write_audit(EVENT_PREFLIGHT, decision_id=args.decision_id,
                detail={"ok": result.ok, "blockers": result.codes,
                        "next_state": result.next_state})

    if result.ok and snapshot is not None:
        try:
            request = build_order_request(decision, snapshot, account_number="<agentic-account>")
            print("\nThe order that WOULD be sent (nothing was sent):")
            print(json.dumps(request, indent=2))
        except ExecutionDisabled as exc:
            print("\nOrder could not be constructed: %s" % exc)

    # --- the wall ---
    print()
    try:
        execute()
        print("!! execute() returned. This is a bug.", file=sys.stderr)
        return 9
    except ExecutionDisabled as exc:
        print("EXECUTION REFUSED")
        print("  %s" % exc)
        gate = execution_gate_blockers(config)
        for code, message in gate:
            print("  x [%s] %s" % (code, message))
        write_audit(EVENT_EXECUTION_REFUSED, decision_id=args.decision_id,
                    detail={"reason": str(exc), "gate": [c for c, _ in gate]})

    print("\nNo order was placed, previewed, or reviewed.")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
