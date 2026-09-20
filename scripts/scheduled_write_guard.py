#!/usr/bin/env python3
"""PreToolUse hook: confine an unattended scheduled run's writes to its own output.

An unattended run legitimately produces exactly three kinds of file:

    reports/latest.md              the daily digest
    reports/YYYY-MM-DD_HHMM.md     the detailed audit record
    research/<SYMBOL>.md           standing per-candidate research

plus two the shell runner writes itself (``logs/scheduled/`` and
``logs/scheduled_usage.jsonl``). Nothing else. This hook denies any
Write/Edit/NotebookEdit outside that set before it happens.

## Why a hook rather than an allowlist entry

The obvious approach — ``--allowedTools "Write(reports/**)"`` — does not work.
Tested against Claude Code 2.1.263: a path-scoped rule matches *nothing*, so it
denied writes to the very directory it named, while bare ``Write`` allowed them
everywhere. A specifier on a file tool is therefore not a usable restriction
here, and the scoping has to happen in a hook.

## Why it is conditional

Enforcement runs only when ``RH_AGENT_SCHEDULED_RUN=1`` is in the environment,
which ``scripts/scheduled_evaluation.sh`` sets before invoking Claude Code.
Interactive sessions and ordinary development in this repository are untouched —
a blanket write restriction would make the repo unmaintainable.

A scheduled run cannot clear the marker to escape: the value lives in the
environment of the CLI process that spawns this hook, and the run's Bash access
is restricted to two specific read-only commands. And if the marker were somehow
absent, the postflight tree check in ``src/scheduling.py`` still reports every
out-of-scope write as a safety failure after the fact. Prevention here,
detection there; neither is asked to be the only control.

## Failure posture

This hook fails **closed** while a scheduled run is in progress: if it cannot
work out what path a tool call targets, it denies the call. A denied write costs
a scheduled run one paragraph of its report. An unnoticed write outside scope is
a hole in the report-only guarantee.

Reads nothing but its stdin and the environment. Writes nothing. Approves
nothing.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scheduling import (  # noqa: E402
    PROMOTION_SNAPSHOT_MARKER,
    PROMOTION_SNAPSHOT_PATH,
    SCHEDULED_RUN_MARKER,
    SNAPSHOT_REFRESH_MARKER,
    WRITABLE_DURING_SCHEDULED_RUN,
    is_writable_during_promotion_snapshot,
    is_writable_during_scheduled_run,
    is_writable_during_snapshot_refresh,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tool inputs that name a file, in the order worth checking.
PATH_FIELDS = ("file_path", "notebook_path", "path", "filePath")

# Tools this hook is registered for. A tool that writes but is not listed here
# is denied outright rather than waved through -- see `_decide`.
GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def allow() -> dict:
    """Say nothing and let the call through."""
    return {}


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def scope_summary() -> str:
    return ", ".join(WRITABLE_DURING_SCHEDULED_RUN)


def relative_to_repo(target: str) -> str:
    """``target`` as a repo-relative path, or '' when it is outside the repo."""
    absolute = os.path.abspath(
        target if os.path.isabs(target) else os.path.join(REPO_ROOT, target)
    )
    # realpath both sides so a symlink cannot smuggle a path back in or out.
    absolute = os.path.realpath(absolute)
    root = os.path.realpath(REPO_ROOT)
    if absolute == root:
        return ""
    prefix = root + os.sep
    if not absolute.startswith(prefix):
        return ""
    return absolute[len(prefix):].replace(os.sep, "/")


def _decide(payload: dict) -> dict:
    tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return deny(
            "This unattended scheduled run may only write to: %s. The %s call "
            "carried no readable input, so it cannot be shown to be in scope."
            % (scope_summary(), tool or "tool")
        )

    if tool not in GUARDED_TOOLS:
        # The hook is registered for the writing tools. Anything else reaching
        # it is unexpected; deny rather than guess at its semantics.
        return deny(
            "This unattended scheduled run may only write to: %s. %r is not a "
            "recognised file-writing tool, so this call is denied by default."
            % (scope_summary(), tool)
        )

    target = ""
    for field in PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value.strip():
            target = value.strip()
            break

    if not target:
        return deny(
            "This unattended scheduled run may only write to: %s. The %s call "
            "named no file path, so it cannot be shown to be in scope."
            % (scope_summary(), tool)
        )

    rel = relative_to_repo(target)
    if not rel:
        return deny(
            "This unattended scheduled run may write only inside the repository, "
            "and only to: %s. %s targets %s, which is outside it."
            % (scope_summary(), tool, target)
        )

    # A snapshot refresh is a different, narrower job: one file, nothing else.
    if os.environ.get(SNAPSHOT_REFRESH_MARKER) == "1":
        if is_writable_during_snapshot_refresh(rel):
            return allow()
        return deny(
            "Denied: a snapshot-refresh run may write only "
            "state/broker_snapshot.json, and %s is not it. This run exists to "
            "record settled cash and nothing else." % rel
        )

    # The promotion gather is narrower still: one file, and not the gate's.
    if os.environ.get(PROMOTION_SNAPSHOT_MARKER) == "1":
        if is_writable_during_promotion_snapshot(rel):
            return allow()
        return deny(
            "Denied: a promotion-snapshot gather may write only %s, and %s is "
            "not it. This run exists to record the broker facts promotion "
            "re-checks, and nothing else." % (PROMOTION_SNAPSHOT_PATH, rel)
        )

    if is_writable_during_scheduled_run(rel):
        return allow()

    return deny(
        "Denied: an unattended scheduled run may not write %s.\n"
        "It may write only to: %s.\n"
        "Your three destinations are the audit record (reports/<stamp>.md), the "
        "digest (reports/latest.md) and research notes (research/<SYMBOL>.md). "
        "If a recommendation seems to need a change elsewhere, that is the "
        "signal to stop and say so in the report instead."
        % (rel, scope_summary())
    )


def main(argv=None) -> int:
    # Not a scheduled run: this hook has no opinion about interactive work.
    if (os.environ.get(SCHEDULED_RUN_MARKER) != "1"
            and os.environ.get(SNAPSHOT_REFRESH_MARKER) != "1"
            and os.environ.get(PROMOTION_SNAPSHOT_MARKER) != "1"):
        return 0

    try:
        raw = sys.stdin.read()
    except OSError:
        raw = ""

    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = None

    if not isinstance(payload, dict):
        # Fail closed: an unreadable payload during a scheduled run is denied.
        print(
            json.dumps(
                deny(
                    "This unattended scheduled run may only write to: %s. The "
                    "tool call could not be read, so it is denied."
                    % scope_summary()
                )
            )
        )
        return 0

    decision = _decide(payload)
    if decision:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
