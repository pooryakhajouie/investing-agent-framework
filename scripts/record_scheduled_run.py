#!/usr/bin/env python3
"""Record the outcome of one scheduled report-only run.

A run produces three tiers, and this script checks all three:

    reports/YYYY-MM-DD_HHMM.md   the detailed audit record
    reports/latest.md            the ~1,000-word daily digest a human reads
    research/*.md                standing per-candidate research notes

``latest.md`` used to be a byte copy of the audit record, which is why the daily
report grew to 4,800 words and stopped being read. It is now a separate, much
shorter document with its own enforced contract (:mod:`src.reporting`) — and the
contract requires every decision-relevant element, so brevity can never be the
reason the decision, the authorization, the five-way comparison, the allocation,
the confidence or the change triggers went missing.

If a run writes no digest at all, one is derived mechanically from the audit
record so ``latest.md`` is never silently yesterday's file. If a run writes an
*invalid* digest it is left in place and reported — an imperfect human-written
summary beats a machine extract, and the loud failure is what gets it fixed.

Writes only ``reports/latest.md`` (when the run wrote none), ``research/INDEX.md``
and ``logs/scheduled_usage.jsonl``. Approves nothing. Executes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from src.research_notes import (  # noqa: E402
    RESEARCH_DIR,
    RESEARCH_INDEX,
    load_all,
    render_index,
    validate_directory,
)
from src.lessons import (  # noqa: E402
    LESSONS_DIRNAME,
    citable,
    load_lessons,
    render_lessons_index,
    validate_lessons_directory,
)
from src.models import money_str  # noqa: E402
from src.state import load_budget_state, load_config  # noqa: E402
from src.notifications import actionable_buy  # noqa: E402
from src.promotion import validate_recommendation  # noqa: E402
from src.scheduling import (  # noqa: E402
    LATEST_REPORT,
    append_usage,
    derive_digest,
    extract_decision,
    missing_report_sections,
    parse_usage,
    validate_concise_report,
)


def read(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(), None
    except OSError as exc:
        return None, str(exc)


def check_detail_report(path: str, problems: list):
    """The audit record: full sections, and a declared decision."""
    if not os.path.exists(path):
        problems.append("no audit record was written to %s" % path)
        return "", None

    text, error = read(path)
    if text is None:
        problems.append("could not read the audit record: %s" % error)
        return "", None

    decision = extract_decision(text)
    if decision is None:
        problems.append(
            "the audit record does not declare a decision; expected a line reading "
            "exactly 'DECISION: WAIT', 'DECISION: SINGLE_BUY' or "
            "'DECISION: SPLIT_BUY_PLAN'"
        )
    missing = missing_report_sections(text)
    if missing:
        problems.append(
            "the audit record is missing sections: %s" % ", ".join(missing)
        )
    return text, decision


def check_digest(digest_path: str, detail_text: str, detail_rel: str, problems: list):
    """The daily digest: concise, but complete in every required element."""
    if not os.path.exists(digest_path):
        problems.append(
            "the run wrote no digest to %s; deriving one from the audit record so "
            "latest.md is not stale" % digest_path
        )
        if not detail_text:
            return None
        derived = derive_digest(detail_text, detail_rel)
        try:
            os.makedirs(os.path.dirname(digest_path), exist_ok=True)
            with open(digest_path, "w", encoding="utf-8") as handle:
                handle.write(derived)
            print("wrote a derived digest to %s" % digest_path)
        except OSError as exc:
            problems.append("could not write a derived digest: %s" % exc)
            return None
        text = derived
    else:
        text, error = read(digest_path)
        if text is None:
            problems.append("could not read the digest: %s" % error)
            return None

    # The specific regression worth naming: latest.md as a copy of the detail.
    if detail_text and text.strip() == detail_text.strip():
        problems.append(
            "reports/latest.md is a byte copy of the audit record, not a digest. "
            "It must be a separate ~800-1,200 word summary; the audit record keeps "
            "the depth."
        )

    check = validate_concise_report(text)
    print(
        "digest: %d words, %d table rows, decision %s"
        % (check.word_count, check.table_rows, check.decision or "NONE")
    )
    for warning in check.warnings:
        print("digest warning: %s" % warning)
    for violation in check.violations:
        problems.append("digest: %s" % violation)
    return check


def refresh_research_index(research_dir: str, problems: list):
    """Validate the notes and regenerate their index."""
    if not os.path.isdir(research_dir):
        print("research: no research/ directory yet")
        return

    bad = validate_directory(research_dir)
    for path, note_problems in sorted(bad.items()):
        for problem in note_problems:
            problems.append("research note: %s" % problem)

    notes = load_all(research_dir)
    index_path = os.path.join(research_dir, os.path.basename(RESEARCH_INDEX))
    try:
        with open(index_path, "w", encoding="utf-8") as handle:
            handle.write(render_index(notes))
    except OSError as exc:
        problems.append("could not refresh the research index: %s" % exc)
        return
    stale = [n.symbol for n in notes if n.is_stale()]
    print(
        "research: %d note(s), %d stale, index refreshed"
        % (len(notes), len(stale))
    )
    if stale:
        print("research: stale theses to revisit — %s" % ", ".join(sorted(stale)))


def refresh_lessons_index(research_dir: str, problems: list) -> None:
    """Validate the historical lessons and regenerate their index.

    A lesson that fails validation is reported rather than silently kept: an
    inadmissible lesson is worse than no lesson, because a later evaluation
    might cite it.
    """
    lessons_dir = os.path.join(research_dir, LESSONS_DIRNAME)
    if not os.path.isdir(lessons_dir):
        return

    bad = validate_lessons_directory(lessons_dir)
    for path, lesson_problems in sorted(bad.items()):
        for problem in lesson_problems:
            problems.append("lesson: %s" % problem)

    entries = load_lessons(lessons_dir)
    try:
        with open(os.path.join(lessons_dir, "INDEX.md"), "w", encoding="utf-8") as h:
            h.write(render_lessons_index(entries))
    except OSError as exc:
        problems.append("could not refresh the lessons index: %s" % exc)
        return

    citable_now = citable([lesson for _, lesson in entries])
    print("lessons: %d recorded, %d citable today" % (len(entries), len(citable_now)))


def check_recommendation(path, decision, problems: list) -> None:
    """A BUY must leave a promotable payload; a WAIT must leave none.

    This is the seam between "the run recommended something" and "a human can
    act on it". If a run declares a BUY and writes no payload, the
    recommendation is unactionable and says so loudly rather than looking fine.
    If a run declares WAIT and writes one anyway, something is confused about
    its own decision and that is worth failing on.
    """
    is_buy = decision in ("SINGLE_BUY", "SPLIT_BUY_PLAN")
    exists = bool(path) and os.path.exists(path)

    if is_buy and not exists:
        problems.append(
            "the run declared %s but wrote no recommendation payload to %s. "
            "Without it the recommendation cannot be promoted, and a payload "
            "cannot be reconstructed from prose." % (decision, path))
        return
    if not is_buy and exists:
        problems.append(
            "the run declared %s but wrote a recommendation payload at %s. Only "
            "a BUY writes one." % (decision or "WAIT", path))
        return
    if not exists:
        return

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        problems.append("could not read the recommendation payload: %s" % exc)
        return

    found = validate_recommendation(payload)
    for problem in found:
        problems.append("recommendation: %s" % problem)
    if not found:
        legs = payload.get("legs") or []
        print("recommendation: %d leg(s), promotable — no decision_id minted"
              % len(legs))


def emit_notification(decision, recommendation_path, path, problems) -> None:
    """Write the actionable-decision notification, if this run produced one.

    A WAIT writes nothing. It is the most common and most correct outcome, and
    a notification the owner receives every weekday stops being a notification
    within a week — taking the one that mattered down with it.

    Only a BUY interrupts anyone, and its contents come from the machine-
    readable recommendation and the budget ledger rather than from the digest's
    prose, so the tickers, amounts and authorization announced are the ones that
    would actually be promoted.
    """
    if not path or decision not in ("SINGLE_BUY", "SPLIT_BUY_PLAN"):
        return
    if not recommendation_path or not os.path.exists(recommendation_path):
        return
    try:
        with open(recommendation_path, "r", encoding="utf-8") as handle:
            legs = (json.load(handle) or {}).get("legs") or []
    except (OSError, ValueError) as exc:
        problems.append("could not read the recommendation for the notification: %s" % exc)
        return
    if not legs:
        return

    try:
        remaining = money_str(load_budget_state(load_config()).remaining_usd)
    except Exception as exc:  # noqa: BLE001 - never lose the alert over this
        problems.append("could not read the authorization for the notification: %s" % exc)
        remaining = "0.00"
    first = legs[0] if isinstance(legs[0], dict) else {}
    confidence = str(first.get("confidence") or "")

    note = actionable_buy(decision, legs, remaining, confidence)
    if note is None:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(note.to_dict(), handle, indent=2, ensure_ascii=False)
        print("notification: [%s] %s" % (note.kind, note.title))
    except OSError as exc:
        problems.append("could not write the notification: %s" % exc)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="the detailed audit record")
    parser.add_argument("--digest", default=LATEST_REPORT, help="reports/latest.md")
    parser.add_argument("--research-dir", default=RESEARCH_DIR)
    parser.add_argument("--emit-notification",
                        help="write the actionable-decision notification here")
    parser.add_argument("--recommendation",
                        help="reports/recommendations/<stamp>.json, BUY only")
    parser.add_argument("--raw", help="claude --output-format json result file")
    parser.add_argument("--started-at", default="")
    parser.add_argument("--finished-at", default="")
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--exit-code", type=int, default=None)
    parser.add_argument("--slot", default="am")
    args = parser.parse_args(argv)

    problems: list = []

    detail_text, decision = check_detail_report(args.report, problems)
    detail_rel = os.path.relpath(args.report, REPO_ROOT)

    digest_check = check_digest(args.digest, detail_text, detail_rel, problems)
    if decision is None and digest_check is not None:
        decision = digest_check.decision

    refresh_research_index(args.research_dir, problems)
    refresh_lessons_index(args.research_dir, problems)
    check_recommendation(args.recommendation, decision, problems)
    emit_notification(decision, args.recommendation, args.emit_notification,
                      problems)

    # Usage metadata is best-effort and never invented.
    payload = None
    if args.raw and os.path.exists(args.raw):
        payload, _ = read(args.raw)

    record = parse_usage(
        payload,
        started_at=args.started_at,
        finished_at=args.finished_at,
        duration_seconds=args.duration,
        exit_code=args.exit_code,
        report=detail_rel,
        decision=decision,
    )
    append_usage(record)

    if record.available:
        bits = []
        if record.total_cost_usd is not None:
            bits.append("cost ~$%.4f" % float(record.total_cost_usd))
        if record.input_tokens is not None:
            bits.append("in %s tok" % record.input_tokens)
        if record.output_tokens is not None:
            bits.append("out %s tok" % record.output_tokens)
        if record.num_turns is not None:
            bits.append("%s turns" % record.num_turns)
        print("usage: " + ", ".join(bits))
    else:
        print("usage: not reported by the CLI for this run (recorded as unavailable)")

    print("decision: %s" % (decision or "NONE DECLARED"))

    for problem in problems:
        print("problem: %s" % problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
