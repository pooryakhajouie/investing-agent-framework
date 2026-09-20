"""Stage 7 — scheduled, report-only evaluations.

A scheduled run is deliberately weaker than an interactive one. It may look at
everything and recommend anything, but it may **change nothing**:

* it never enables execution;
* it never approves anything;
* it never exposes an order tool;
* it never submits anything;
* it never touches the three execution safety switches.

This module holds the part of that promise that can be *tested* rather than
merely documented: the invariants a report-only run must satisfy before it
starts and must still satisfy after it finishes. The shell runner in
``scripts/scheduled_evaluation.sh`` calls into it at both ends, so a run that
somehow mutated the safety surface fails loudly instead of silently.

Nothing here executes anything, and nothing here can approve anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Report structure lives in src.reporting; the legacy names are re-exported
# below so existing callers keep working.
from .reporting import (  # noqa: E402,F401
    CONCISE_REPORT_SECTIONS,
    DETAIL_REPORT_SECTIONS,
    ReportCheck,
    derive_digest,
    extract_decision,
    missing_concise_sections,
    missing_detail_sections,
    validate_concise_report,
    word_count,
)
from .research_notes import (  # noqa: E402,F401
    RESEARCH_INDEX,
    list_note_paths,
    validate_directory as validate_research_directory,
)
from .reporting import (  # noqa: E402,F401
    DISCOVERY_REPORT_SECTIONS,
    validate_discovery_report,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(REPO_ROOT, "reports")
LATEST_REPORT = os.path.join(REPORTS_DIR, "latest.md")
RESEARCH_DIR = os.path.join(REPO_ROOT, "research")
USAGE_LOG = os.path.join(REPO_ROOT, "logs", "scheduled_usage.jsonl")
SETTINGS_PATH = os.path.join(REPO_ROOT, ".claude", "settings.json")
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")

# The scheduler is allowed to run only in this mode.
REPORT_ONLY = "REPORT_ONLY"

# Files whose contents a report-only run must leave byte-identical. If any of
# these changes across a scheduled run, something went badly wrong and the run
# is reported as a safety failure regardless of what the report says.
IMMUTABLE_DURING_SCHEDULED_RUN = (
    "config.json",
    ".claude/settings.json",
    "INVESTMENT_POLICY.md",
    "DISCOVERY_POLICY.md",
    "CLAUDE.md",
    "src/guardrails.py",
    "src/models.py",
    "src/execution.py",
    "src/approval.py",
    "src/submission.py",
    "src/allocation.py",
    "src/market_calendar.py",
    # A run must not rewrite the rules of its own scheduling, either: the
    # instructions it was given, the runner that constrains its tools, the
    # module that checks these invariants, or the plists and installer that
    # decide when and with what environment it runs at all.
    "src/scheduling.py",
    "src/launchd.py",
    "src/reporting.py",
    "src/research_notes.py",
    "src/history.py",
    "src/lessons.py",
    "src/promotion.py",
    "src/capital_gate.py",
    "src/notifications.py",
    # The BUY contract itself, and the script that prints it. A run that could
    # rewrite the schema it is validated against could define its own way out.
    "src/decision_schema.py",
    "scripts/emit_buy_scaffold.py",
    "prompts/scheduled_evaluation.md",
    "prompts/weekly_discovery.md",
    "scripts/scheduled_evaluation.sh",
    "scripts/weekly_discovery.sh",
    "scripts/scheduled_write_guard.py",
    "scripts/ingest_history.py",
    "scripts/promote_latest_recommendation.py",
    "scripts/check_capital_gate.py",
    "scripts/notify.py",
    "scripts/refresh_broker_snapshot.sh",
    "scripts/refresh_promotion_snapshot.sh",
    "scheduler/com.robinhood-agent.discovery.plist",
    "scheduler/install.sh",
    "scheduler/com.robinhood-agent.evaluation.plist",
    "scheduler/com.robinhood-agent.evaluation-pm.plist",
)

# Order-placing and order-cancelling tools. A scheduled run must never have
# these available, and they must stay denied in .claude/settings.json.
ORDER_TOOLS = (
    "place_equity_order",
    "place_crypto_order",
    "place_option_order",
    "review_equity_order",
    "review_option_order",
    "preview_crypto_order",
    "cancel_equity_order",
    "cancel_crypto_order",
    "cancel_option_order",
    "exercise_option",
    "cancel_option_exercise",
)

# Account-mutating tools. Not trades, but still writes.
MUTATING_TOOLS = (
    "create_watchlist",
    "update_watchlist",
    "add_to_watchlist",
    "remove_from_watchlist",
    "add_option_to_watchlist",
    "remove_option_from_watchlist",
    "follow_watchlist",
    "unfollow_watchlist",
    "create_alert",
    "update_alert",
    "delete_alert",
    "mark_alerts_read",
    "create_scan",
    "update_scan_config",
    "update_scan_filters",
)

FORBIDDEN_TOOLS = ORDER_TOOLS + MUTATING_TOOLS

# Scripts a scheduled run must never invoke.
FORBIDDEN_SCRIPTS = (
    "approve_decision.py",
    "submit_approved.py",
    "record_submission.py",
    "execute_approved.py",
    "prepare_submission.py",
    # Promotion mints a pending decision, which advances the pipeline. That is a
    # human act for the same reason approval is: an unattended run must not be
    # able to move its own recommendation one step closer to execution.
    "promote_latest_recommendation.py",
    # Ingest writes personal financial history from gathered broker payloads.
    "ingest_history.py",
)

# State files a scheduled run must never create or modify.
FORBIDDEN_STATE_WRITES = (
    "state/approvals.json",
    "state/pending_submission.json",
    "state/executions.json",
    "state/budget.json",
)


class ScheduledSafetyError(Exception):
    """Raised when a report-only invariant is violated."""


# --------------------------------------------------------------------------
# Report paths
# --------------------------------------------------------------------------


def report_filename(now: Optional[datetime] = None) -> str:
    """``YYYY-MM-DD_HHMM.md`` for the given local time."""
    now = now or datetime.now()
    return now.strftime("%Y-%m-%d_%H%M") + ".md"


def report_path(now: Optional[datetime] = None, reports_dir: str = REPORTS_DIR) -> str:
    return os.path.join(reports_dir, report_filename(now))


# --------------------------------------------------------------------------
# The safety surface
# --------------------------------------------------------------------------


def file_digest(path: str) -> Optional[str]:
    """SHA-256 of a file, or None when it does not exist."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def surface_digest(repo_root: str = REPO_ROOT) -> Dict[str, Optional[str]]:
    """Digest every file a report-only run must leave untouched."""
    return {
        rel: file_digest(os.path.join(repo_root, rel))
        for rel in IMMUTABLE_DURING_SCHEDULED_RUN
    }


def surface_changes(
    before: Dict[str, Optional[str]], after: Dict[str, Optional[str]]
) -> List[str]:
    """Files whose digest moved between two snapshots."""
    changed = []
    for rel in sorted(set(before) | set(after)):
        if before.get(rel) != after.get(rel):
            changed.append(rel)
    return changed


# --------------------------------------------------------------------------
# Write scope: the only paths an unattended run may create or modify
# --------------------------------------------------------------------------
#
# An unattended run produces three kinds of file and nothing else. Everything
# outside this list must come out of a scheduled run byte-identical, including
# files no other check covers -- `state/`, `logs/decisions.jsonl`, `docs/`,
# `tests/`, and any path a run might invent.
#
# This is enforced in two independent places, because neither alone is enough:
#
#   prevention   a PreToolUse hook (scripts/scheduled_write_guard.py) denies a
#                Write/Edit outside these paths before it happens, but only
#                while the runner's marker env var is set, so interactive
#                sessions and ordinary development are untouched;
#   detection    postflight fingerprints the whole repository tree and reports
#                any out-of-scope create, modify or delete as a safety failure,
#                which holds even if the hook were bypassed or disabled.
#
# The path specifier form -- ``--allowedTools "Write(reports/**)"`` -- was
# tested against Claude Code 2.1.263 and matches nothing at all: a scoped rule
# denied writes to the very directory it named, while bare ``Write`` allowed
# them everywhere. So the allowlist cannot express this, and the hook is what
# actually does the scoping.

WRITABLE_DURING_SCHEDULED_RUN = (
    "reports/",                    # the daily digest and the audit records
    "research/",                   # notes, and research/lessons/
    "logs/scheduled/",             # per-run runner logs
    "logs/scheduled_usage.jsonl",  # approximate cost/token metadata
    "logs/discovery/",             # weekly discovery runner logs
    "logs/discovery_usage.jsonl",  # weekly discovery usage metadata
)

# Notably NOT writable, and each for its own reason:
#   state/portfolio_history.json  personal financial history, ingested only by
#                                 scripts/ingest_history.py, which a human runs
#   state/budget.json             the authorization ledger
#   logs/decisions.jsonl          the decision log, appended only by the logger
#   state/broker_snapshot.json    regenerated by the runner shell before the
#                                 model starts; the model must not be able to
#                                 rewrite the cash figure its own gate used
#   state/notifications.json      the suppression ledger, written by the runner
#                                 shell via scripts/notify.py -- never by the
#                                 model, which could otherwise silence an alert
#                                 about its own recommendation
# A scheduled run that touches any of them is a safety failure.

# The environment marker the runner sets. The write guard enforces only when it
# is present, and a run cannot clear it: the value lives in the environment of
# the CLI process that spawns the hook, not in anything the model can reach.
SCHEDULED_RUN_MARKER = "RH_AGENT_SCHEDULED_RUN"

# The second marker, set by the runner *after* the model process has exited, for
# the deterministic post-run steps. Promotion accepts it and refuses
# SCHEDULED_RUN_MARKER, which is what separates "the runner promoted a
# recommendation after re-checking every guardrail" from "a model advanced its
# own advice". Both set at once means the model's environment, and is refused.
SCHEDULED_POST_RUN_MARKER = "RH_AGENT_SCHEDULED_POST_RUN"

# A third marker, for the snapshot refresh — a separate, far narrower Claude
# process that reads two account tools and writes exactly one file.
#
# It needs its own mode because the two scopes are disjoint, not nested: the
# evaluation may write reports/ and research/ but must NEVER write the cash
# figure its own gate consumed, and the refresh may write only that figure and
# nothing else. Giving the refresh the evaluation's scope would let it write
# reports; adding the snapshot to the evaluation's scope would let the
# evaluation forge its own funding check. So each gets exactly what it needs.
SNAPSHOT_REFRESH_MARKER = "RH_AGENT_SNAPSHOT_REFRESH"

#: The only path a snapshot-refresh run may write.
BROKER_SNAPSHOT_PATH = "state/broker_snapshot.json"
WRITABLE_DURING_SNAPSHOT_REFRESH = (BROKER_SNAPSHOT_PATH,)

# A fourth marker, for the promotion snapshot — the read-only gather that runs
# after the model has exited and before the deterministic promotion step.
#
# It exists because the capital-gate snapshot deliberately does not answer
# promotion's questions. The morning gate asks one thing, "is there settled cash
# worth paying for an evaluation with", and is kept cheap on purpose: two
# account tools, a three-line prompt. Promotion asks a different and larger set
# — a refreshed quote per proposed asset, tradability, fractional eligibility,
# the account's own order history — and answering those in the morning gate
# would make the cheap check expensive every single day, including the ~90% of
# days that end in WAIT and never promote anything.
#
# So the two snapshots stay separate and neither weakens the other: the gate
# keeps its two tools, and this one is paid for only on the days a BUY is
# actually recommended.
PROMOTION_SNAPSHOT_MARKER = "RH_AGENT_PROMOTION_SNAPSHOT"

#: The only path a promotion-snapshot gather may write.
PROMOTION_SNAPSHOT_PATH = "state/promotion_snapshot.json"
WRITABLE_DURING_PROMOTION_SNAPSHOT = (PROMOTION_SNAPSHOT_PATH,)


def is_writable_during_promotion_snapshot(rel_path: str) -> bool:
    """True only for the promotion snapshot itself.

    Disjoint from both other scopes, for the same reason they are disjoint from
    each other: this gather may not write reports, research or the cash figure
    the morning gate consumed, and neither of those may forge this.
    """
    cleaned = (rel_path or "").strip().lstrip("./")
    return cleaned in WRITABLE_DURING_PROMOTION_SNAPSHOT


def is_writable_during_snapshot_refresh(rel_path: str) -> bool:
    """True only for the broker snapshot itself.

    Deliberately not expressed in terms of the scheduled-run allowlist: a
    refresh may not write reports, research, or logs, and a scheduled
    evaluation may not write this. Neither scope contains the other.
    """
    cleaned = (rel_path or "").strip().lstrip("./")
    return cleaned in WRITABLE_DURING_SNAPSHOT_REFRESH

# Not part of the repository's own contents, so not worth fingerprinting.
TREE_SCAN_EXCLUDED_DIRS = (
    ".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".mypy_cache",
    "node_modules", ".idea", ".vscode", "htmlcov", "scratch", "tmp",
)
TREE_SCAN_EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".local.json", ".egg-info")
TREE_SCAN_EXCLUDED_NAMES = (".DS_Store", "Thumbs.db")


def is_writable_during_scheduled_run(rel_path: str) -> bool:
    """Whether an unattended run may create or modify this repo-relative path."""
    normalized = (rel_path or "").replace(os.sep, "/").lstrip("./")
    if not normalized or normalized.startswith("../"):
        return False
    for allowed in WRITABLE_DURING_SCHEDULED_RUN:
        if allowed.endswith("/"):
            if normalized.startswith(allowed):
                return True
        elif normalized == allowed:
            return True
    return False


def _tree_skipped(name: str) -> bool:
    return (
        name in TREE_SCAN_EXCLUDED_NAMES
        or name.endswith(TREE_SCAN_EXCLUDED_SUFFIXES)
    )


def repo_tree(repo_root: str = REPO_ROOT) -> Dict[str, str]:
    """SHA-256 of every file in the repository worth watching."""
    tree: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [
            d for d in dirnames
            if d not in TREE_SCAN_EXCLUDED_DIRS and not d.endswith(".egg-info")
        ]
        for name in filenames:
            if _tree_skipped(name):
                continue
            absolute = os.path.join(dirpath, name)
            rel = os.path.relpath(absolute, repo_root).replace(os.sep, "/")
            digest = file_digest(absolute)
            if digest is not None:
                tree[rel] = digest
    return tree


def unauthorized_writes(
    before: Dict[str, str], after: Dict[str, str]
) -> List[str]:
    """Out-of-scope files a run created, modified or deleted."""
    findings: List[str] = []
    for rel in sorted(set(before) | set(after)):
        if is_writable_during_scheduled_run(rel):
            continue
        was, now = before.get(rel), after.get(rel)
        if was == now:
            continue
        if was is None:
            findings.append("%s was created" % rel)
        elif now is None:
            findings.append("%s was deleted" % rel)
        else:
            findings.append("%s was modified" % rel)
    return findings


# --------------------------------------------------------------------------
# The archive: the deeper record a short digest depends on
# --------------------------------------------------------------------------
#
# ``reports/latest.md`` is a ~1,000-word digest, not a copy of the run's work.
# That is only safe while the deeper material stays retrievable, so the two
# tiers underneath it are inventoried before and after every run:
#
#   reports/YYYY-MM-DD_HHMM.md   the detailed audit records
#   research/*.md                the standing per-candidate research
#
# A run may **add** to either and may **update** a note (that is the point of
# keeping them). It may not delete one, and it may not gut one -- a note that
# loses most of its body has lost research a future run would otherwise have
# reused. Both are reported as safety failures, exactly like a mutated
# guardrail, because "the summary got shorter" must never end up meaning "the
# analysis got lost".

# A rewritten archive file may lose at most this fraction of its words before
# it counts as gutted rather than edited.
ARCHIVE_SHRINK_TOLERANCE = 0.5

# latest.md is the digest, not an archive file: it is meant to be replaced
# wholesale every run.
ARCHIVE_EXCLUDED_BASENAMES = ("latest.md", "INDEX.md")


def _archive_entry(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    return {
        "sha": hashlib.sha256(raw).hexdigest(),
        "words": len(raw.decode("utf-8", "replace").split()),
    }


def archive_inventory(repo_root: str = REPO_ROOT) -> Dict[str, Dict[str, Any]]:
    """Fingerprint every detailed report and research note."""
    inventory: Dict[str, Dict[str, Any]] = {}
    for rel_dir in ("reports", "research"):
        directory = os.path.join(repo_root, rel_dir)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".md") or name in ARCHIVE_EXCLUDED_BASENAMES:
                continue
            entry = _archive_entry(os.path.join(directory, name))
            if entry is not None:
                inventory["%s/%s" % (rel_dir, name)] = entry
    return inventory


def run_digest(repo_root: str = REPO_ROOT) -> Dict[str, Any]:
    """Everything a postflight needs: protected surface, archive, whole tree."""
    return {
        "surface": surface_digest(repo_root),
        "archive": archive_inventory(repo_root),
        "tree": repo_tree(repo_root),
    }


def split_digest(saved: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Read either digest shape.

    Pre-digest snapshots were a flat ``{rel: sha}`` map of the protected surface
    only. Those still load, with an empty archive, so an in-flight run whose
    preflight predates this change still gets its surface checked.
    """
    if isinstance(saved, dict) and "surface" in saved:
        surface = saved.get("surface") or {}
        archive = saved.get("archive") or {}
        return (
            surface if isinstance(surface, dict) else {},
            archive if isinstance(archive, dict) else {},
        )
    return (saved if isinstance(saved, dict) else {}), {}


def digest_tree(saved: Any) -> Dict[str, str]:
    """The whole-tree fingerprint from a snapshot, or {} for an older shape."""
    if isinstance(saved, dict) and "tree" in saved:
        tree = saved.get("tree") or {}
        return tree if isinstance(tree, dict) else {}
    return {}


def archive_losses(
    before: Dict[str, Any], after: Dict[str, Any]
) -> List[str]:
    """Archive files a run deleted or gutted."""
    losses: List[str] = []
    for rel in sorted(before):
        old = before.get(rel) or {}
        new = after.get(rel)
        if new is None:
            losses.append("%s was deleted" % rel)
            continue
        old_words = old.get("words") or 0
        new_words = new.get("words") or 0
        if old_words and new_words < old_words * ARCHIVE_SHRINK_TOLERANCE:
            losses.append(
                "%s shrank from %d to %d words" % (rel, old_words, new_words)
            )
    return losses


@dataclass
class SafetyCheck:
    """The verdict on whether a report-only run may proceed."""

    ok: bool
    violations: List[str] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": list(self.violations),
            "warnings": list(self.warnings),
            "checks": list(self.checks),
        }


def check_switches(config: Any) -> Tuple[List[str], List[str]]:
    """The three execution switches must all be closed."""
    checks = ["execution_mode_is_dry_run", "agent_disabled", "live_trading_disabled"]
    violations = []
    mode = getattr(config, "execution_mode", None)
    if mode != "DRY_RUN":
        violations.append(
            "execution_mode is %r, but a scheduled run requires DRY_RUN" % mode
        )
    if getattr(config, "agent_enabled", False):
        violations.append("agent_enabled is true; a scheduled run requires false")
    if getattr(config, "live_trading", False):
        violations.append("live_trading is true; a scheduled run requires false")
    return violations, checks


def check_denied_tools(settings: Any) -> Tuple[List[str], List[str]]:
    """Every order and account-mutating tool must be denied in settings."""
    checks = ["order_tools_denied", "mutating_tools_denied"]
    violations = []
    if not isinstance(settings, dict):
        return [
            ".claude/settings.json could not be read as an object; failing closed"
        ], checks

    deny = ((settings.get("permissions") or {}).get("deny")) or []
    if not isinstance(deny, list):
        return [".claude/settings.json permissions.deny is not a list"], checks
    denied_blob = "\n".join(str(entry) for entry in deny)

    for tool in FORBIDDEN_TOOLS:
        if tool not in denied_blob:
            violations.append(
                "%s is not denied in .claude/settings.json; a scheduled run must never "
                "have it available" % tool
            )
    return violations, checks


def check_approval_script_denied(settings: Any) -> Tuple[List[str], List[str]]:
    """Approval is a human act; the scheduler must not be able to invoke it."""
    checks = ["approve_decision_script_denied"]
    deny = ((settings or {}).get("permissions") or {}).get("deny") or []
    blob = "\n".join(str(entry) for entry in deny)
    if "approve_decision.py" not in blob:
        return (
            ["scripts/approve_decision.py is not denied in .claude/settings.json; "
             "a scheduled run must never be able to approve anything"],
            checks,
        )
    return [], checks


def check_no_pending_submission(repo_root: str = REPO_ROOT) -> Tuple[List[str], List[str]]:
    """An unresolved submission must be handled by a human, not a cron job."""
    checks = ["no_unresolved_submission"]
    path = os.path.join(repo_root, "state", "pending_submission.json")
    if os.path.exists(path):
        return (
            ["state/pending_submission.json exists: a submission outcome is unresolved. "
             "Run scripts/reconcile_submission.py by hand. A scheduled run must not "
             "proceed past an uncertain submission."],
            checks,
        )
    return [], checks


def preflight_safety(
    config: Any,
    settings: Optional[Dict[str, Any]] = None,
    repo_root: str = REPO_ROOT,
) -> SafetyCheck:
    """Everything that must be true before a scheduled report-only run starts."""
    if settings is None:
        settings = load_settings(os.path.join(repo_root, ".claude", "settings.json"))

    violations: List[str] = []
    checks: List[str] = []

    for fn in (
        lambda: check_switches(config),
        lambda: check_denied_tools(settings),
        lambda: check_approval_script_denied(settings),
        lambda: check_no_pending_submission(repo_root),
    ):
        found, ran = fn()
        violations.extend(found)
        checks.extend(ran)

    return SafetyCheck(ok=not violations, violations=violations, checks=checks)


def postflight_safety(
    before: Dict[str, Any],
    after: Dict[str, Any],
    repo_root: str = REPO_ROOT,
) -> SafetyCheck:
    """Everything that must still be true after a scheduled run finishes.

    ``before`` and ``after`` may each be a full :func:`run_digest` snapshot or a
    bare ``{rel: sha}`` surface map; the latter is the pre-digest shape and is
    still accepted so a run whose preflight predates this change is checked
    rather than skipped.
    """
    violations: List[str] = []
    checks: List[str] = ["safety_surface_unchanged", "no_approval_created"]

    before_surface, before_archive = split_digest(before)
    after_surface, after_archive = split_digest(after)

    changed = surface_changes(before_surface, after_surface)
    if changed:
        violations.append(
            "a report-only run modified files it must never touch: %s"
            % ", ".join(changed)
        )

    # An unattended run writes three kinds of file. Anything else it touched --
    # created, modified or deleted -- is out of scope, whatever the report says.
    # This backs up the PreToolUse write guard rather than trusting it: the hook
    # prevents, this detects, and a bypass of one is still caught by the other.
    before_tree, after_tree = digest_tree(before), digest_tree(after)
    if before_tree:
        checks.append("no_writes_outside_the_permitted_paths")
        strays = unauthorized_writes(before_tree, after_tree)
        if strays:
            violations.append(
                "a report-only run wrote outside its permitted paths (%s): %s"
                % (", ".join(WRITABLE_DURING_SCHEDULED_RUN), "; ".join(strays))
            )

    # The digest is short because the depth lives elsewhere. If a run destroys
    # what it lives in, the shortening has become information loss.
    checks.append("no_research_or_audit_record_lost")
    if before_archive:
        losses = archive_losses(before_archive, after_archive)
        if losses:
            violations.append(
                "a report-only run destroyed prior research or audit records: %s. "
                "reports/latest.md is a digest, so these are the only copy of the "
                "detail — a run may add to them or update a note, never delete or "
                "gut one." % "; ".join(losses)
            )

    approvals = os.path.join(repo_root, "state", "approvals.json")
    if os.path.exists(approvals):
        try:
            with open(approvals, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = None
        if data:
            violations.append(
                "state/approvals.json is non-empty after a report-only run. A scheduled "
                "run must never create an approval."
            )

    pending = os.path.join(repo_root, "state", "pending_submission.json")
    checks.append("no_submission_handoff_created")
    if os.path.exists(pending):
        violations.append(
            "state/pending_submission.json exists after a report-only run; a scheduled "
            "run must never mint a submission handoff."
        )

    return SafetyCheck(ok=not violations, violations=violations, checks=checks)


def load_settings(path: str = SETTINGS_PATH) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# Usage / cost metadata
# --------------------------------------------------------------------------


@dataclass
class UsageRecord:
    """Approximate usage for one scheduled run, when the CLI reports it."""

    started_at: str
    finished_at: str
    duration_seconds: Optional[float] = None
    total_cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    num_turns: Optional[int] = None
    model: Optional[str] = None
    session_id: Optional[str] = None
    report: Optional[str] = None
    decision: Optional[str] = None
    exit_code: Optional[int] = None
    available: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def parse_usage(payload: Any, **extra: Any) -> UsageRecord:
    """Pull usage metadata out of a ``claude -p --output-format json`` result.

    The CLI's exact shape has changed before and will change again, so every
    field is optional and a miss is recorded as ``available=False`` rather than
    guessed at. Never invent a cost figure.
    """
    record = UsageRecord(
        started_at=extra.pop("started_at", ""),
        finished_at=extra.pop("finished_at", ""),
    )
    for key, value in extra.items():
        if hasattr(record, key):
            setattr(record, key, value)

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return record
    if isinstance(payload, list) and payload:
        # stream-json: the terminal "result" event carries the totals.
        for event in reversed(payload):
            if isinstance(event, dict) and event.get("type") == "result":
                payload = event
                break
        else:
            payload = payload[-1]
    if not isinstance(payload, dict):
        return record

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

    def pick(*names):
        for name in names:
            if payload.get(name) is not None:
                return payload[name]
            if usage.get(name) is not None:
                return usage[name]
        return None

    record.total_cost_usd = pick("total_cost_usd", "cost_usd", "total_cost")
    record.input_tokens = pick("input_tokens")
    record.output_tokens = pick("output_tokens")
    record.cache_read_tokens = pick("cache_read_input_tokens")
    record.cache_creation_tokens = pick("cache_creation_input_tokens")
    record.num_turns = pick("num_turns")
    record.session_id = pick("session_id")
    record.model = pick("model")
    duration = pick("duration_ms")
    if duration is not None:
        try:
            record.duration_seconds = round(float(duration) / 1000.0, 3)
        except (TypeError, ValueError):
            record.duration_seconds = None
    if record.duration_seconds is None:
        record.duration_seconds = extra.get("duration_seconds")

    record.available = any(
        v is not None
        for v in (
            record.total_cost_usd, record.input_tokens,
            record.output_tokens, record.num_turns,
        )
    )
    return record


def append_usage(record: UsageRecord, path: str = USAGE_LOG) -> None:
    """Append one usage record. Never raises — usage tracking is not critical."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Report inspection
# --------------------------------------------------------------------------
#
# The structural contract for both report tiers lives in :mod:`src.reporting`:
# ``DETAIL_REPORT_SECTIONS`` for the timestamped audit record and
# ``CONCISE_REPORT_SECTIONS`` plus :func:`validate_concise_report` for the daily
# digest. These two names are the pre-digest spelling, kept so nothing that
# imported them has to change.
REQUIRED_REPORT_SECTIONS = DETAIL_REPORT_SECTIONS
missing_report_sections = missing_detail_sections


def utc_stamp(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
