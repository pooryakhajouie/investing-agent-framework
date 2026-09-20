#!/usr/bin/env python3
"""Show the agent's current authorization, state, and decision history.

Read-only by default. Does not touch Robinhood.

    python3 scripts/check_status.py
    python3 scripts/check_status.py --json
    python3 scripts/check_status.py --init      # create budget state (safe, refuses to clobber)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.decision_logger import decisions_for_month, read_decisions, summarize  # noqa: E402
from src.models import usd  # noqa: E402
from src.state import (  # noqa: E402
    CRYPTO_UNIVERSE_MAX_AGE_DAYS,
    DEFAULT_BUDGET_PATH,
    DEFAULT_WATCHLIST_PATH,
    ConfigError,
    CorruptStateError,
    CryptoUniverseError,
    load_crypto_universe,
    current_month,
    fresh_state,
    load_budget_state,
    load_config,
    load_last_evaluation,
    save_budget_state,
)


def do_init(force: bool) -> int:
    config = load_config()
    if os.path.exists(DEFAULT_BUDGET_PATH) and os.path.getsize(DEFAULT_BUDGET_PATH) > 0 and not force:
        print(
            "Refusing to overwrite existing budget state at %s.\n"
            "Inspect it first. Re-run with --force only if you are deliberately resetting."
            % DEFAULT_BUDGET_PATH
        )
        return 1
    state = fresh_state(config)
    save_budget_state(state, DEFAULT_BUDGET_PATH)
    print("Initialized %s for %s at %s" % (DEFAULT_BUDGET_PATH, state.month, usd(state.authorized_budget_usd)))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Investing agent status")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--init", action="store_true", help="create initial budget state")
    parser.add_argument("--force", action="store_true", help="with --init, overwrite existing state")
    parser.add_argument("--history", type=int, default=5, help="how many recent decisions to show")
    args = parser.parse_args()

    if args.init:
        return do_init(args.force)

    try:
        config = load_config()
    except ConfigError as exc:
        print("CONFIG ERROR: %s" % exc, file=sys.stderr)
        return 2

    try:
        state = load_budget_state(config)
    except CorruptStateError as exc:
        print("STATE ERROR (failing closed): %s" % exc, file=sys.stderr)
        return 3

    try:
        universe = load_crypto_universe()
        universe_error = None
    except CryptoUniverseError as exc:
        universe = None
        universe_error = str(exc)

    month = current_month()
    month_records = decisions_for_month(month)
    all_records = read_decisions()
    last_eval = load_last_evaluation()

    if args.json:
        print(
            json.dumps(
                {
                    "config": config.to_dict(),
                    "budget_state": state.to_dict(),
                    "current_month": month,
                    "this_month": summarize(month_records),
                    "all_time": summarize(all_records),
                    "last_evaluation": last_eval,
                    "crypto_universe": (
                        {
                            "fetched_at": universe.fetched_at,
                            "age_days": universe.age_days(),
                            "pairs": len(universe.pairs),
                            "purchasable": len(universe.purchasable_symbols),
                        }
                        if universe
                        else {"error": universe_error}
                    ),
                },
                indent=2,
            )
        )
        return 0

    print("=" * 68)
    print("LONG-TERM INVESTING AGENT — STATUS")
    print("=" * 68)
    print("Strategy               : %s" % config.strategy)
    print("LIVE TRADING           : %s" % ("ENABLED (!!)" if config.live_trading else "DISABLED (dry run only)"))
    print("Selling allowed        : %s" % config.allow_selling)
    print("Options / margin / short: %s / %s / %s"
          % (config.allow_options, config.allow_margin, config.allow_shorting))
    print("Transfers allowed      : %s" % config.allow_transfers)
    print("Crypto allowed         : %s (direct Robinhood pairs only)" % config.allow_crypto)
    print("Min investment horizon : %d months" % config.min_investment_horizon_months)
    print("-" * 68)
    if universe is None:
        print("Crypto universe        : UNAVAILABLE — %s" % universe_error)
    else:
        age = universe.age_days()
        stale = age is not None and age > CRYPTO_UNIVERSE_MAX_AGE_DAYS
        print(
            "Crypto universe        : %d pairs, %d purchasable, fetched %s (%s)"
            % (
                len(universe.pairs),
                len(universe.purchasable_symbols),
                universe.fetched_at,
                "STALE — refresh it" if stale else "fresh",
            )
        )
    if os.path.exists(DEFAULT_WATCHLIST_PATH):
        try:
            with open(DEFAULT_WATCHLIST_PATH, encoding="utf-8") as handle:
                snap = json.load(handle)
            lists = snap.get("watchlists", [])
            print(
                "Watchlist snapshot     : %d lists, %d items, fetched %s"
                % (
                    len(lists),
                    sum(int(w.get("item_count") or 0) for w in lists),
                    snap.get("fetched_at", "?"),
                )
            )
        except (OSError, json.JSONDecodeError, ValueError):
            print("Watchlist snapshot     : present but unreadable")
    else:
        print("Watchlist snapshot     : not captured yet")
    print("-" * 68)
    print("Calendar month         : %s" % state.month)
    print("Authorized this month  : %s" % usd(state.authorized_budget_usd))
    print("Committed this month   : %s" % usd(state.committed_usd))
    print("REMAINING AUTHORIZATION: %s" % usd(state.remaining_usd))
    print("Decisions acted upon   : %d" % len(state.acted_decision_ids))
    print("State last updated     : %s" % (state.last_updated or "never"))
    if state.month != month:
        print("NOTE                   : state month differs from the current calendar month")
    print("-" * 68)

    stats = summarize(month_records)
    print("This month  : %d evaluations (%d BUY, %d WAIT, %d rejected by guardrails)"
          % (stats["evaluations"], stats["buy_decisions"], stats["wait_decisions"], stats["rejected"]))
    stats_all = summarize(all_records)
    print("All time    : %d evaluations (%d BUY, %d WAIT, %d rejected by guardrails)"
          % (stats_all["evaluations"], stats_all["buy_decisions"], stats_all["wait_decisions"], stats_all["rejected"]))
    print("-" * 68)

    recent = all_records[-args.history:] if args.history > 0 else []
    if not recent:
        print("No decisions logged yet.")
    else:
        print("Most recent %d decision(s):" % len(recent))
        for record in recent:
            valid = (record.get("validation_result") or {}).get("valid")
            mark = "ok  " if valid else "REJ "
            print(
                "  %s %s  %-4s %-6s %8s  %s"
                % (
                    mark,
                    (record.get("timestamp") or "?")[:19],
                    record.get("decision") or "?",
                    record.get("ticker") or "-",
                    "$" + str(record.get("proposed_amount") or "0.00"),
                    record.get("asset_class") or record.get("execution_status"),
                )
            )
    print("-" * 68)
    if last_eval:
        print("Last evaluation: %s" % (last_eval.get("status") or last_eval.get("decision")))
    print("Execution: NO ORDER HAS EVER BEEN SUBMITTED BY THIS AGENT (dry-run only; no order path exists).")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
