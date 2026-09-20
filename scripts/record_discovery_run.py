#!/usr/bin/env python3
"""Record and validate one weekly discovery run.

A discovery pass widens the candidate universe. It has less authority than the
weekday evaluation, and the one thing this script exists to enforce is that it
stayed inside that authority: **a discovery report that declares a decision,
proposes an allocation, mints a decision_id, or uses approval language is a
failure**, however good its research is.

It also validates any research notes the pass wrote and regenerates
``research/INDEX.md``.

Writes only ``research/INDEX.md`` and ``logs/discovery_usage.jsonl``. Approves
nothing. Executes nothing.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISCOVERY_USAGE_LOG = os.path.join(REPO_ROOT, "logs", "discovery_usage.jsonl")

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
from src.reporting import validate_discovery_report  # noqa: E402
from src.scheduling import append_usage, parse_usage  # noqa: E402


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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--research-dir", default=RESEARCH_DIR)
    parser.add_argument("--raw")
    parser.add_argument("--started-at", default="")
    parser.add_argument("--finished-at", default="")
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--exit-code", type=int, default=None)
    args = parser.parse_args(argv)

    problems: list = []

    # --- the discovery report ---------------------------------------------
    if not os.path.exists(args.report):
        problems.append("no discovery report was written to %s" % args.report)
        check = None
    else:
        try:
            with open(args.report, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            problems.append("could not read the discovery report: %s" % exc)
            text = ""
        check = validate_discovery_report(text) if text else None

    if check is not None:
        print("discovery report: %d words, %d table rows" % (
            check.word_count, check.table_rows))
        for warning in check.warnings:
            print("discovery warning: %s" % warning)
        for violation in check.violations:
            problems.append("discovery report: %s" % violation)
        if check.ok:
            print("discovery report: proposes nothing — contract satisfied")

    # --- research notes ---------------------------------------------------
    if os.path.isdir(args.research_dir):
        bad = validate_directory(args.research_dir)
        for path, note_problems in sorted(bad.items()):
            for problem in note_problems:
                problems.append("research note: %s" % problem)
        notes = load_all(args.research_dir)
        index_path = os.path.join(args.research_dir, os.path.basename(RESEARCH_INDEX))
        try:
            with open(index_path, "w", encoding="utf-8") as handle:
                handle.write(render_index(notes))
            print("research: %d note(s), index refreshed" % len(notes))
        except OSError as exc:
            problems.append("could not refresh the research index: %s" % exc)

    refresh_lessons_index(args.research_dir, problems)

    # --- usage, best-effort and never invented ----------------------------
    payload = None
    if args.raw and os.path.exists(args.raw):
        try:
            with open(args.raw, "r", encoding="utf-8") as handle:
                payload = handle.read()
        except OSError:
            payload = None

    record = parse_usage(
        payload,
        started_at=args.started_at,
        finished_at=args.finished_at,
        duration_seconds=args.duration,
        exit_code=args.exit_code,
        report=os.path.relpath(args.report, REPO_ROOT),
        decision="DISCOVERY_NO_DECISION",
    )
    append_usage(record, DISCOVERY_USAGE_LOG)

    if record.available:
        bits = []
        if record.total_cost_usd is not None:
            bits.append("cost ~$%.4f" % float(record.total_cost_usd))
        if record.num_turns is not None:
            bits.append("%s turns" % record.num_turns)
        print("usage: " + ", ".join(bits))
    else:
        print("usage: not reported by the CLI for this run (recorded as unavailable)")

    for problem in problems:
        print("problem: %s" % problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
