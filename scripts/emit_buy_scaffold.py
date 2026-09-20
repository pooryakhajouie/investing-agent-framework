#!/usr/bin/env python3
"""Print the canonical BUY leg scaffold, and the calendar numbers it may not invent.

    python3 scripts/emit_buy_scaffold.py                 # equity, SINGLE_BUY
    python3 scripts/emit_buy_scaffold.py --kind crypto
    python3 scripts/emit_buy_scaffold.py --plan-type SPLIT_BUY_PLAN
    python3 scripts/emit_buy_scaffold.py --calendar-only

An evaluation that is going to recommend a BUY runs this first and starts from
what it prints. Two things come out of it, and both exist because a scheduled
run got them wrong on 2026-09-17:

**The key structure.** The payload that day used its own spellings for the
nested blocks — ``positions_held`` for ``position_count``,
``quote_and_valuation`` for ``current_quote_and_valuation`` — because the only
document carrying the machine-readable template was one a scheduled run never
reads. The guardrails rejected all of it. The scaffold is generated from
:mod:`src.decision_schema`, which is the same contract the guardrails validate,
so a payload that keeps these keys cannot fail that way again.

**The two calendar numbers.** ``days_remaining_in_month`` and
``tradable_sessions_remaining`` are checked against the project's exchange
calendar. That day's run computed 13 and 9 by hand against an actual 14 and 10,
and earned two ``OPTIONALITY_MISREPORTED`` violations for it. These come
straight from the functions the guardrail compares against. **Copy them
verbatim. Do not recompute them.**

Reads nothing, writes nothing, and reaches no network. Prints and exits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.decision_schema import (  # noqa: E402
    PROMPT_BLOCK_BEGIN,
    PROMPT_FILES,
    TEMPLATE_KINDS,
    extract_prompt_block,
    optionality_calendar,
    prompt_block,
    recommendation_template,
    render,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sync_prompts() -> int:
    """Rewrite the generated block in every prompt that carries it.

    The prompts are the only thing a model reads, and a hand-edited copy of a
    machine contract is a copy that will be wrong eventually. This makes the
    code the source and the prompts the output.
    """
    generated = prompt_block()
    changed = []
    for relative in PROMPT_FILES:
        path = os.path.join(REPO_ROOT, relative)
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        existing = extract_prompt_block(text)
        if existing is None:
            print("%s carries no %s marker; add it first"
                  % (relative, PROMPT_BLOCK_BEGIN), file=sys.stderr)
            return 1
        if existing == generated:
            print("  unchanged  %s" % relative)
            continue
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text.replace(existing, generated))
        changed.append(relative)
        print("  rewrote    %s" % relative)
    print("%d prompt(s) updated" % len(changed))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", default="equity", choices=sorted(TEMPLATE_KINDS))
    parser.add_argument("--plan-type", default="SINGLE_BUY",
                        choices=("SINGLE_BUY", "SPLIT_BUY_PLAN"))
    parser.add_argument("--calendar-only", action="store_true",
                        help="print only the calendar block")
    parser.add_argument("--json", action="store_true",
                        help="print the template alone, with no commentary")
    parser.add_argument("--prompt-block", action="store_true",
                        help="print the block the prompts embed")
    parser.add_argument("--sync-prompts", action="store_true",
                        help="rewrite that block in every prompt that carries it")
    args = parser.parse_args(argv)

    if args.sync_prompts:
        return sync_prompts()
    if args.prompt_block:
        print(prompt_block())
        return 0

    calendar = optionality_calendar()

    if args.json:
        print(render(recommendation_template(kind=args.kind,
                                             plan_type=args.plan_type)))
        return 0

    print("=" * 70)
    print("CANONICAL CALENDAR — copy these verbatim into monthly_optionality")
    print("=" * 70)
    print("  days_remaining_in_month     : %d" % calendar["days_remaining_in_month"])
    print("  tradable_sessions_remaining : %d" % calendar["tradable_sessions_remaining"])
    print("  final equity opportunity    : %s" % calendar["final_equity_opportunity"])
    print("  project date                : %s" % calendar["project_date"])
    print()
    print("These come from the same functions the guardrails check against.")
    print("A hand-computed value earns OPTIONALITY_MISREPORTED and the")
    print("recommendation cannot be promoted.")

    if args.calendar_only:
        return 0

    print()
    print("=" * 70)
    print("CANONICAL BUY PAYLOAD — %s, %s" % (args.kind, args.plan_type))
    print("=" * 70)
    print("Keep every key. Replace every placeholder value. Omit decision_id,")
    print("approved, approval, approved_by, user_approved, execution_state and")
    print("fingerprint — promotion mints the identifier, and only")
    print("scripts/approve_decision.py creates an approval.")
    print()
    print(render(recommendation_template(kind=args.kind,
                                         plan_type=args.plan_type)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
