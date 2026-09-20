#!/usr/bin/env python3
"""Promote the latest scheduled BUY recommendation to a PROPOSED decision.

    python3 scripts/promote_latest_recommendation.py --snapshot broker.json
    python3 scripts/promote_latest_recommendation.py --dry-run   # check, write nothing

A scheduled run may recommend a purchase but may not mint a ``decision_id``,
write ``logs/decisions.jsonl``, approve anything, or submit anything. Its
recommendation therefore sits outside the decision pipeline: readable, and
impossible to act on. This script is the one deliberate, human-invoked step that
carries it in — and it stops exactly where the pipeline already began, at a
**PROPOSED** decision awaiting the unchanged human approval flow.

**This script does not approve anything.** It creates the thing you then approve
with ``scripts/approve_decision.py``, which is unchanged: same TTL, same
verbatim challenge phrase, same audit log. Nothing here can submit an order —
there is still no submission path in this repository.

## What it re-checks before minting anything

Everything the recommendation relied on is re-established from scratch:

1. the recommendation is the **newest** valid report, and is not stale;
2. the digest's ACTION banner and the machine payload **agree**;
3. the **monthly authorization**, reconciled against the broker;
4. **broker orders and pending activity**;
5. **settled cash**, cash-only — buying power is not a funding basis;
6. a **refreshed quote**, rejected if the move exceeds the existing slippage
   tolerance (2% equity / 5% crypto) or the quote is stale;
7. **tradability**, including crypto halt state and minimum order size;
8. **every normal guardrail**, re-run from the payload.

Items 3-8 are :func:`src.execution.preflight`, called rather than
reimplemented. Because the execution switches are permanently closed, preflight
always blocks; promotion partitions those blockers and requires that *the only
reasons this could not execute are the closed switches and the not-yet-given
approval*. The gate codes' **absence** is itself a blocker — promotion will not
run against an armed execution path.

## Two callers, and only one of them is a model

It refuses outright when ``RH_AGENT_SCHEDULED_RUN=1`` — that is the Claude
process of a scheduled run, and a model must never advance its own
recommendation into the pipeline.

The runner may promote after that process has exited, with
``RH_AGENT_SCHEDULED_POST_RUN=1``. That is deterministic code re-running every
guardrail rather than a judgement, and it still mints only a **PROPOSED**
decision: no approval, no ticket, no execution authority. Both markers set at
once means the model's own environment, and is refused.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(REPO_ROOT, "reports")
DIGEST_PATH = os.path.join(REPORTS_DIR, "latest.md")
RECOMMENDATIONS_DIR = os.path.join(REPORTS_DIR, "recommendations")

from src import guardrails  # noqa: E402
from src.allocation import sibling_reservations_usd  # noqa: E402
from src.approval import (  # noqa: E402
    challenge_phrase,
    fingerprint,
    get_approval,
    load_approvals,
)
from src.execution_store import load_executions  # noqa: E402
from src.decision_logger import build_record, log_decision, new_decision_id  # noqa: E402
from src.execution import ExecutionState, preflight  # noqa: E402
from src.models import ZERO, money_str, parse_money, usd  # noqa: E402
from src.promotion import (  # noqa: E402
    MAX_RECOMMENDATION_AGE_HOURS,
    PromotionError,
    build_promoted_decision,
    check_banner_agreement,
    check_freshness,
    classify_preflight,
    price_move_pct,
    quote_age_limit,
    slippage_tolerance,
    snapshot_coverage,
    snapshot_from_dict,
    snapshot_problems,
    validate_recommendation,
)
from src.reporting import parse_action_banner  # noqa: E402
from src.scheduling import (  # noqa: E402
    SCHEDULED_POST_RUN_MARKER,
    SCHEDULED_RUN_MARKER,
)
from src.state import (  # noqa: E402
    DEFAULT_LAST_EVAL_PATH,
    ConfigError,
    CorruptStateError,
    load_budget_state,
    load_config,
    load_crypto_universe,
    save_last_evaluation,
    utc_now,
)

_REPORT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}\.md$")
_DIGEST_POINTER_RE = re.compile(r"reports/(\d{4}-\d{2}-\d{2}_\d{4}\.md)")


def newest_audit_record() -> str:
    """Basename of the newest timestamped audit record, or ''."""
    names = [
        os.path.basename(path)
        for path in glob.glob(os.path.join(REPORTS_DIR, "*.md"))
        if _REPORT_RE.match(os.path.basename(path))
    ]
    return sorted(names)[-1] if names else ""


def newest_recommendation() -> str:
    paths = sorted(glob.glob(os.path.join(RECOMMENDATIONS_DIR, "*.json")))
    return paths[-1] if paths else ""


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def banner_line(text: str) -> str:
    banner = parse_action_banner(text)
    return banner.line if banner else "(no ACTION line)"


def report(title: str, check) -> None:
    print("  %s: %s" % (title, "ok" if check.ok else "BLOCKED"))
    for code, message in check.blockers:
        print("    x [%s] %s" % (code, message))
    for warning in check.warnings:
        print("    ! %s" % warning)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", help="read-only broker snapshot JSON")
    parser.add_argument("--recommendation", help="override the recommendation file")
    parser.add_argument("--digest", default=DIGEST_PATH)
    parser.add_argument("--dry-run", action="store_true",
                        help="run every check and write nothing")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    # --- 0. who is calling? ----------------------------------------------
    #
    # Two different callers, and only one of them is a model.
    #
    #   RH_AGENT_SCHEDULED_RUN=1        the Claude process of a scheduled run.
    #                                   REFUSED. A model must never advance its
    #                                   own recommendation into the pipeline.
    #   RH_AGENT_SCHEDULED_POST_RUN=1   the runner shell, after that process has
    #                                   exited. ALLOWED: this is deterministic
    #                                   code re-running every guardrail, not a
    #                                   judgement, and it still only mints a
    #                                   PROPOSED decision.
    #
    # Both set at once would mean the model's own environment, so that is
    # refused too — the narrower rule wins.
    scheduled_model = os.environ.get(SCHEDULED_RUN_MARKER) == "1"
    scheduled_post = os.environ.get(SCHEDULED_POST_RUN_MARKER) == "1"
    if scheduled_model:
        print(
            "refusing: %s=1 means this is the model process of an unattended run. "
            "Promotion mints a pending decision, which a model may not do for its "
            "own recommendation — the same reason a scheduled run may not approve "
            "anything. The runner performs promotion after this process exits, "
            "with %s=1." % (SCHEDULED_RUN_MARKER, SCHEDULED_POST_RUN_MARKER),
            file=sys.stderr)
        return 4
    if scheduled_post:
        print("running as the scheduled post-run step (%s=1): every guardrail is "
              "re-checked and at most a PROPOSED decision is minted."
              % SCHEDULED_POST_RUN_MARKER)

    now = utc_now()
    print("=" * 70)
    print("PROMOTE LATEST SCHEDULED RECOMMENDATION")
    print("=" * 70)

    # --- 1. the digest and its ACTION banner ----------------------------
    if not os.path.exists(args.digest):
        print("no digest at %s" % args.digest, file=sys.stderr)
        return 2
    with open(args.digest, "r", encoding="utf-8") as handle:
        digest_text = handle.read()
    banner = parse_action_banner(digest_text)
    print("digest:  %s" % os.path.relpath(args.digest, REPO_ROOT))
    print("action:  %s" % banner_line(digest_text))

    if banner is None or banner.kind != "BUY":
        print()
        print("Nothing to promote — the latest scheduled evaluation recommends no")
        print("purchase. This is a normal, frequent outcome, not an error.")
        return 0

    # --- 2. the machine-readable recommendation -------------------------
    path = args.recommendation or newest_recommendation()
    if not path or not os.path.exists(path):
        print()
        print("The digest announces a purchase but there is no machine-readable",
              file=sys.stderr)
        print("recommendation under reports/recommendations/ to promote.", file=sys.stderr)
        print("A scheduled run that recommends a BUY must write one; without it",
              file=sys.stderr)
        print("the payload would have to be reconstructed from prose, and this",
              file=sys.stderr)
        print("script will not invent a decision.", file=sys.stderr)
        return 2
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        print("could not read %s: %s" % (path, exc), file=sys.stderr)
        return 2
    print("payload: %s" % os.path.relpath(path, REPO_ROOT))

    problems = validate_recommendation(payload)
    if problems:
        print()
        for problem in problems:
            print("  x %s" % problem, file=sys.stderr)
        return 1

    # --- 3. still the newest, and fresh ---------------------------------
    pointer = _DIGEST_POINTER_RE.search(digest_text)
    freshness = check_freshness(
        payload, newest_audit_record(),
        pointer.group(1) if pointer else None, now)
    print()
    print("checks:")
    report("recommendation is current and newest", freshness)

    # --- 4. banner agrees with the payload ------------------------------
    agreement = check_banner_agreement(banner, payload)
    report("ACTION banner matches the payload", agreement)

    # --- 5. config and budget -------------------------------------------
    try:
        config = load_config()
        budget_state = load_budget_state(config)
    except (ConfigError, CorruptStateError) as exc:
        print("  x state/config error (failing closed): %s" % exc, file=sys.stderr)
        return 3
    print("  month %s: %s authorized, %s committed, %s remaining" % (
        budget_state.month, usd(budget_state.authorized_budget_usd),
        usd(budget_state.committed_usd), usd(budget_state.remaining_usd)))

    # --- 6. the snapshot -------------------------------------------------
    #
    # Read once, then narrowed per leg: the account-level facts are shared, the
    # quote and tradability are not. A two-leg plan re-checked against one
    # leg's price would be checking the wrong thing twice.
    raw_snapshot = None
    if args.snapshot:
        try:
            raw_snapshot = read_json(args.snapshot)
        except (OSError, ValueError) as exc:
            print("  x could not read the snapshot: %s" % exc, file=sys.stderr)
            return 2
        wrong = snapshot_problems(raw_snapshot)
        if wrong:
            print()
            print("  x %s is not a promotion snapshot:"
                  % os.path.relpath(args.snapshot, REPO_ROOT), file=sys.stderr)
            for problem in wrong:
                print("      %s" % problem, file=sys.stderr)
            print("    Refusing rather than promoting against a partial view of "
                  "the account.", file=sys.stderr)
            return 2
    else:
        print("  ! no --snapshot given: the broker re-checks cannot run and")
        print("    promotion will fail closed as BROKER_UNREADABLE.")

    legs = payload["legs"]
    is_crypto_plan = any(
        str(leg.get("asset_class") or "").upper() == "CRYPTO" for leg in legs)

    try:
        crypto_universe = load_crypto_universe() if is_crypto_plan else None
    except Exception as exc:  # noqa: BLE001 - fail closed on a bad snapshot file
        print("  x crypto universe unreadable (failing closed): %s" % exc,
              file=sys.stderr)
        return 3

    # --- 7. per leg: guardrails + preflight ------------------------------
    minted: list = []
    fatal = False

    # Dollars held by legs a human already approved this month but that have not
    # been submitted. The broker cannot see them and the local ledger does not
    # record them, so without this a promotion could hand out authorization that
    # is already spoken for.
    approvals = load_approvals()
    execution_states = {
        decision_id: record.state
        for decision_id, record in load_executions().items()
    }
    approved_siblings = sibling_reservations_usd(
        approvals, execution_states, budget_state.month)
    if approved_siblings > ZERO:
        print("  approved-but-unsubmitted siblings this month: %s"
              % usd(approved_siblings))

    for index, leg in enumerate(legs):
        ticker = str(leg.get("ticker") or "?")
        amount = parse_money(leg.get("proposed_amount_usd"))
        decision_id = new_decision_id()

        snapshot = None
        if raw_snapshot is not None:
            try:
                snapshot = snapshot_from_dict(raw_snapshot, now, symbol=ticker)
            except PromotionError as exc:
                print("  x could not read the snapshot for %s: %s"
                      % (ticker, exc), file=sys.stderr)
                return 2
        decision = build_promoted_decision(
            leg, decision_id, str(payload.get("source_report")),
            str(payload.get("generated_at")))

        print()
        print("-" * 70)
        print("leg %d/%d: %s %s  (candidate decision_id %s)"
              % (index + 1, len(legs), ticker, usd(amount), decision_id))

        if snapshot is not None:
            crypto_here = str(leg.get("asset_class") or "").upper() == "CRYPTO"
            for field in snapshot_coverage(snapshot, crypto_here):
                print("  ! snapshot is missing %s" % field)

        # every normal guardrail, from the payload as it will be logged
        result = guardrails.validate(
            decision, config, budget_state,
            crypto_universe=crypto_universe, now=now)
        print("  guardrails: %s (%d checks)" % (
            "valid" if result.valid else "INVALID", len(result.checks_run)))
        for violation in result.violations:
            print("    x [%s] %s" % (violation.code, violation.message))
        for warning in result.warnings:
            print("    ! %s" % warning)

        # the quote move since the recommendation
        if snapshot is not None and snapshot.quote_price_usd is not None:
            move = price_move_pct(leg.get("current_price_usd"), snapshot.quote_price_usd)
            crypto_leg = str(leg.get("asset_class") or "").upper() == "CRYPTO"
            tolerance = slippage_tolerance(crypto_leg)
            if move is None:
                print("  ! price move could not be computed from the payload")
            else:
                verdict = "within" if abs(move) <= tolerance else "BEYOND"
                print("  quote: %s -> %s  (%+.2f%%, %s the %.1f%% tolerance, "
                      "quote age limit %ds)" % (
                          leg.get("current_price_usd"),
                          money_str(snapshot.quote_price_usd), move, verdict,
                          tolerance, quote_age_limit(crypto_leg)))

        pre = preflight(
            decision, None, config, budget_state, snapshot,
            execution_state=ExecutionState.PROPOSED, now=now,
            crypto_universe=crypto_universe,
            sibling_reservations_usd=approved_siblings)
        promotion = classify_preflight(pre)
        print("  preflight: %s" % ("ok" if promotion.ok else "BLOCKED"))
        if promotion.expected:
            print("    (expected and ignored: %s)" % ", ".join(sorted(set(promotion.expected))))
        for code, message in promotion.blockers:
            print("    x [%s] %s" % (code, message))
        for warning in promotion.warnings:
            print("    ! %s" % warning)

        leg_ok = (result.valid and promotion.ok and freshness.ok and agreement.ok
                  and get_approval(decision_id) is None)
        if not leg_ok:
            fatal = True
            print("  -> NOT PROMOTED")
            continue

        minted.append((decision, result, ticker, amount))
        print("  -> promotable")

    print()
    print("=" * 70)
    if fatal or not minted:
        print("NOTHING WAS PROMOTED. No decision_id was written, no approval was")
        print("created, and logs/decisions.jsonl is unchanged.")
        return 1

    if args.dry_run:
        print("--dry-run: every check passed. Nothing was written.")
        for decision, _, ticker, amount in minted:
            print("  would mint %s for %s %s"
                  % (decision["decision_id"], ticker, usd(amount)))
        return 0

    # --- 8. mint the normal PROPOSED decision(s) -------------------------
    for decision, result, ticker, amount in minted:
        proposal, _ = guardrails.normalize(decision)
        record = build_record(proposal, result, budget_state, raw_decision=decision)
        log_decision(record)
        save_last_evaluation(
            {
                "schema_version": 1,
                "status": "COMPLETED",
                "decision_id": record.get("decision_id"),
                "timestamp": record.get("timestamp"),
                "month": budget_state.month,
                "decision": record.get("decision"),
                "ticker": record.get("ticker"),
                "proposed_amount": record.get("proposed_amount"),
                "monthly_budget_remaining": record.get("monthly_budget_before"),
                "validation_valid": result.valid,
                "violations": result.violation_codes,
                "execution_status": result.execution_status,
                "promoted_from": decision.get("promoted_from"),
            },
            DEFAULT_LAST_EVAL_PATH,
        )
        print("PROMOTED  %s  %s %s" % (decision["decision_id"], ticker, usd(amount)))
        print("  fingerprint: %s" % fingerprint(decision))
        print("  state:       PROPOSED — not approved, not submitted")

    print()
    print("Nothing is approved. Each leg needs its own separate human approval,")
    print("in its own interactive session, with its own typed challenge phrase:")
    print()
    for decision, _, ticker, amount in minted:
        print("  python3 scripts/approve_decision.py %s" % decision["decision_id"])
        print("      you will be asked to type:  %s" % challenge_phrase(ticker, amount))
    print()
    print("Execution remains disabled: the three switches are closed and there is")
    print("no order-submission path in this repository.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
