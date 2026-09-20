#!/usr/bin/env python3
"""Decide whether today's scheduled evaluation is worth running.

    python3 scripts/check_capital_gate.py --snapshot broker.json
    python3 scripts/check_capital_gate.py --snapshot broker.json --json
    python3 scripts/check_capital_gate.py --snapshot broker.json --emit-notification n.json

Runs *before* Claude is invoked. The expensive evaluation is worth its cost only
on days when something could actually be bought, and two conditions rule that
out by arithmetic alone:

* the month's authorization is spent — a success, and silent;
* settled cash cannot fund the smallest purchase the broker accepts — a
  request addressed to the owner, and not silent.

Exit codes are the interface the runner scripts use:

    0   EVALUATE            — go ahead and invoke Claude
    10  AUTHORIZATION_EXHAUSTED — skip, quietly, successfully
    11  FUNDING_REQUIRED    — skip, and a notification was emitted
    12  BROKER_UNREADABLE   — skip, and say so; never treated as funded

10, 11 and 12 are *not* failures of the job. The launchd schedule stays
installed and the next run re-evaluates from scratch, so a new month or a
deposit reactivates evaluation with no human action.

Reads only. Approves nothing, executes nothing, and calls no broker tool: the
snapshot is gathered by the caller with read-only tools.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.allocation import sibling_reservations_usd  # noqa: E402
from src.approval import load_approvals  # noqa: E402
from src.capital_gate import (  # noqa: E402
    GATE_AUTHORIZATION_EXHAUSTED,
    GATE_BROKER_UNREADABLE,
    GATE_EVALUATE,
    GATE_FUNDING_REQUIRED,
    authorization_only_gate,
    evaluate_gate,
    settled_cash_from_snapshot,
)
from src.execution_store import load_executions  # noqa: E402
from src.market_calendar import final_opportunity, project_date  # noqa: E402
from src.models import usd  # noqa: E402
from src.notifications import funding_required, month_end_funding  # noqa: E402
from src.state import (  # noqa: E402
    ConfigError,
    CorruptStateError,
    current_month,
    load_budget_state,
    load_config,
    utc_now,
)

EXIT_CODES = {
    GATE_EVALUATE: 0,
    GATE_AUTHORIZATION_EXHAUSTED: 10,
    GATE_FUNDING_REQUIRED: 11,
    GATE_BROKER_UNREADABLE: 12,
}

#: How close to the month's final tradable moment the funding reminder fires.
MONTH_END_REMINDER_DAYS = 3


def near_month_end(now=None) -> bool:
    """True within a few days of the month's final equity opportunity."""
    now = now or utc_now()
    today = project_date(now)
    final = final_opportunity(today.year, today.month, "EQUITY")
    return 0 <= (final.date() - today).days <= MONTH_END_REMINDER_DAYS


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", help="read-only broker snapshot JSON")
    parser.add_argument("--authorization-only", action="store_true",
                        help="phase one: decide from the ledger alone, no broker data")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--emit-notification",
                        help="write the notification, if any, to this path")
    args = parser.parse_args(argv)

    try:
        config = load_config()
        budget_state = load_budget_state(config)
    except (ConfigError, CorruptStateError) as exc:
        print("state/config error (failing closed): %s" % exc, file=sys.stderr)
        return EXIT_CODES[GATE_BROKER_UNREADABLE]

    snapshot = None
    if args.snapshot and os.path.exists(args.snapshot):
        try:
            with open(args.snapshot, "r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, ValueError) as exc:
            print("could not read the snapshot: %s" % exc, file=sys.stderr)

    month = current_month()
    reservations = sibling_reservations_usd(
        load_approvals(),
        {did: record.state for did, record in load_executions().items()},
        month,
    )

    if args.authorization_only:
        # Phase one costs nothing and needs no broker data: an exhausted month
        # is decided from the ledger alone. This is what keeps the commonest
        # skip free — no snapshot refresh, no Claude invocation at all.
        gate = authorization_only_gate(
            month=month,
            remaining_authorization_usd=budget_state.remaining_usd,
            reservations_usd=reservations,
        )
    else:
        gate = evaluate_gate(
            month=month,
            remaining_authorization_usd=budget_state.remaining_usd,
            snapshot=snapshot,
            reservations_usd=reservations,
        )

    notification = None
    if gate.outcome == GATE_FUNDING_REQUIRED:
        notification = funding_required(gate)
    elif gate.outcome == GATE_AUTHORIZATION_EXHAUSTED and near_month_end():
        # The month is done. One reminder, near the end, so next month starts
        # able to act. The budget figure comes from config.json, never a literal.
        #
        # Cash must be *known* for this: phase one does not read the broker, and
        # a reminder that asserts "the account holds $0.00" when it holds the
        # full budget is worse than no reminder — it asks for a deposit that is
        # not needed, which is exactly how a real alert gets ignored. When cash
        # is unknown the reminder is withheld, and the runner refreshes the
        # snapshot near month end so it is knowable.
        cash = (gate.settled_cash_usd
                if gate.settled_cash_usd is not None
                else settled_cash_from_snapshot(snapshot))
        if cash is None:
            print("month-end reminder withheld: settled cash is unknown, and a "
                  "reminder that guesses the balance is worse than none")
        else:
            notification = month_end_funding(month, config.monthly_budget_usd, cash)

    if args.json:
        print(json.dumps(
            {"gate": gate.to_dict(),
             "notification": notification.to_dict() if notification else None},
            indent=2, ensure_ascii=False))
    else:
        print("=" * 68)
        print("CAPITAL GATE — %s" % gate.outcome)
        print("=" * 68)
        print("month                    : %s" % gate.month)
        print("remaining authorization  : %s" % usd(gate.remaining_authorization_usd))
        print("settled cash             : %s" % (
            usd(gate.settled_cash_usd) if gate.settled_cash_usd is not None
            else "UNKNOWN"))
        print("deployable today         : %s" % usd(gate.deployable_usd))
        for note in gate.notes:
            print("  ! %s" % note)
        print("-" * 68)
        print(gate.reason)
        if notification:
            print("-" * 68)
            print("notification: [%s] %s" % (notification.kind, notification.title))

    if notification and args.emit_notification:
        try:
            with open(args.emit_notification, "w", encoding="utf-8") as handle:
                json.dump(notification.to_dict(), handle, indent=2, ensure_ascii=False)
        except OSError as exc:
            print("could not write the notification: %s" % exc, file=sys.stderr)

    return EXIT_CODES[gate.outcome]


if __name__ == "__main__":
    raise SystemExit(main())
