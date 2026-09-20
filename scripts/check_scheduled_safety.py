#!/usr/bin/env python3
"""Verify the report-only invariants for a scheduled evaluation.

    # before a run — may the scheduler start at all?
    python3 scripts/check_scheduled_safety.py --preflight --save-digest /tmp/d.json

    # after a run — did it change anything it must not have?
    python3 scripts/check_scheduled_safety.py --postflight --digest /tmp/d.json

Exit status is 0 when every invariant holds and 1 when any fails. The runner
treats a failure as fatal: a scheduled run that cannot prove it is report-only
does not get to write a report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scheduling import (  # noqa: E402
    postflight_safety,
    preflight_safety,
    run_digest,
)
from src.state import ConfigError, CorruptStateError, load_config  # noqa: E402


def render(label: str, check) -> str:
    lines = ["=" * 68, "SCHEDULED SAFETY — %s — %s" % (
        label, "PASSED" if check.ok else "FAILED"), "=" * 68]
    for name in check.checks:
        lines.append("  . %s" % name)
    if check.violations:
        lines.append("")
        lines.append("VIOLATIONS (%d):" % len(check.violations))
        for violation in check.violations:
            lines.append("  x %s" % violation)
    if check.warnings:
        lines.append("")
        for warning in check.warnings:
            lines.append("  ! %s" % warning)
    lines.append("=" * 68)
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--postflight", action="store_true")
    parser.add_argument("--save-digest", help="write the safety-surface digest here")
    parser.add_argument("--digest", help="compare against this saved digest")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.preflight:
        try:
            config = load_config()
        except (ConfigError, CorruptStateError) as exc:
            print("config error (failing closed): %s" % exc, file=sys.stderr)
            return 1
        check = preflight_safety(config)
        if args.save_digest:
            try:
                with open(args.save_digest, "w", encoding="utf-8") as handle:
                    json.dump(run_digest(), handle, indent=2, sort_keys=True)
            except OSError as exc:
                print("could not save digest: %s" % exc, file=sys.stderr)
                return 1
        label = "PREFLIGHT"
    else:
        if not args.digest:
            print("--postflight requires --digest", file=sys.stderr)
            return 1
        try:
            with open(args.digest, "r", encoding="utf-8") as handle:
                before = json.load(handle)
        except (OSError, ValueError) as exc:
            print("could not read digest: %s" % exc, file=sys.stderr)
            return 1
        check = postflight_safety(before, run_digest())
        label = "POSTFLIGHT"

    if args.json:
        print(json.dumps(check.to_dict(), indent=2))
    else:
        print(render(label, check))
    return 0 if check.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
