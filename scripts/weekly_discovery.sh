#!/bin/bash
#
# Weekly broad-market discovery — REPORT ONLY, PROPOSES NOTHING.
#
# Invoked by launchd (see scheduler/). Runs Claude Code noninteractively against
# prompts/weekly_discovery.md to widen the candidate universe. It proposes no
# purchase, declares no decision, and approves nothing — the monthly $25
# authorization is decided only by the weekday evaluation.
#
#   ./scripts/weekly_discovery.sh                  # normal run
#   ./scripts/weekly_discovery.sh --dry-run        # safety checks only, no Claude
#   ./scripts/weekly_discovery.sh --check-runtime  # can this environment run at all?
#
# Exit codes: 0 ok, 1 safety failure, 2 setup problem, 3 Claude failed.
#
# This shares every control with scripts/scheduled_evaluation.sh — the same
# report-only preflight, the same absolute-path runtime resolution, the same
# PreToolUse write guard, the same postflight surface and archive digest — and
# adds one of its own: the report must contain no decision, allocation, or
# approval language (src/reporting.validate_discovery_report).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

export TZ="America/Chicago"

DRY_RUN=0
CHECK_RUNTIME=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --check-runtime) CHECK_RUNTIME=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

PYTHON="${PYTHON:-python3}"
CLAUDE_BIN="${CLAUDE_BIN:-}"
NODE_BIN="${NODE_BIN:-}"

if [ -n "$NODE_BIN" ] && [ -x "$NODE_BIN" ]; then
  case ":$PATH:" in
    *":$(dirname "$NODE_BIN"):"*) : ;;
    *) PATH="$(dirname "$NODE_BIN"):$PATH"; export PATH ;;
  esac
fi
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
fi
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  CLAUDE_BIN="$("$PYTHON" scripts/resolve_launchd_runtime.py --print claude_bin --quiet 2>/dev/null || true)"
fi

DISCOVERY_DIR="$REPO_ROOT/reports/discovery"
RESEARCH_DIR="$REPO_ROOT/research"
LOG_DIR="$REPO_ROOT/logs/discovery"
mkdir -p "$DISCOVERY_DIR" "$RESEARCH_DIR" "$LOG_DIR" || exit 2

STAMP="$(date +%Y-%m-%d)"
DISCOVERY_REPORT_PATH="$DISCOVERY_DIR/${STAMP}.md"
RUN_LOG="$LOG_DIR/${STAMP}.log"
SAFETY_DIGEST="$(mktemp -t rh-agent-disc-digest)"
RAW_OUTPUT="$(mktemp -t rh-agent-disc-out)"
trap 'rm -f "$SAFETY_DIGEST" "$RAW_OUTPUT"' EXIT

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$RUN_LOG"; }

log "weekly discovery starting (tz=$TZ, local=$(date '+%Y-%m-%d %H:%M %Z'))"
log "discovery report: $DISCOVERY_REPORT_PATH"
log "research dir:     $RESEARCH_DIR"
log "runtime: claude=${CLAUDE_BIN:-UNRESOLVED} node=${NODE_BIN:-none} python=$PYTHON"

# --------------------------------------------------------------------------
# 0. Runtime
# --------------------------------------------------------------------------
check_runtime() {
  if ! command -v "$PYTHON" >/dev/null 2>&1; then
    log "FATAL: '$PYTHON' is not executable in this environment."
    return 2
  fi
  if [ -z "$CLAUDE_BIN" ]; then
    log "FATAL: no Claude Code executable could be resolved."
    log "       Fix with: ./scheduler/install.sh verify --discovery"
    return 2
  fi
  case "$CLAUDE_BIN" in
    /*) : ;;
    *) log "FATAL: CLAUDE_BIN='$CLAUDE_BIN' is not an absolute path."; return 2 ;;
  esac
  if [ ! -x "$CLAUDE_BIN" ]; then
    log "FATAL: CLAUDE_BIN='$CLAUDE_BIN' is not an executable file."
    return 2
  fi
  local version
  if ! version="$("$CLAUDE_BIN" --version 2>&1)"; then
    log "FATAL: '$CLAUDE_BIN --version' failed in this environment."
    printf '%s\n' "$version" | head -5 | tee -a "$RUN_LOG"
    return 2
  fi
  log "runtime ok: $(printf '%s' "$version" | head -1)"
  return 0
}

if [ "$DRY_RUN" -eq 0 ]; then
  check_runtime || exit 2
fi

# --------------------------------------------------------------------------
# 1. Preflight — the same report-only gate as the weekday evaluation
# --------------------------------------------------------------------------
if ! "$PYTHON" scripts/check_scheduled_safety.py --preflight \
      --save-digest "$SAFETY_DIGEST" >>"$RUN_LOG" 2>&1; then
  log "FATAL: report-only preflight failed. Not running. See $RUN_LOG"
  exit 1
fi
log "preflight passed: all three switches closed, order tools denied, approval script denied"

if [ "$DRY_RUN" -eq 1 ]; then
  log "--dry-run: safety checks passed; skipping the Claude invocation"
  exit 0
fi
if [ "$CHECK_RUNTIME" -eq 1 ]; then
  log "--check-runtime: executables resolved and safety checks passed."
  log "                 No report was written and nothing was scheduled."
  exit 0
fi

# --------------------------------------------------------------------------
# 2. Run Claude Code, read-only and proposing nothing
# --------------------------------------------------------------------------
# Read-only tools only. No validate_plan, no check_status: a discovery pass has
# no decision to validate and no budget to consult, so it is not given the
# means to form one. get_popular_watchlists and get_watchlist_items are the
# discovery substrate; follow_watchlist stays denied.
ALLOWED_TOOLS="Read,Glob,Grep,Write,WebSearch,WebFetch,mcp__robinhood-trading__get_accounts,mcp__robinhood-trading__get_equity_positions,mcp__robinhood-trading__get_crypto_positions,mcp__robinhood-trading__get_watchlists,mcp__robinhood-trading__get_watchlist_items,mcp__robinhood-trading__get_popular_watchlists,mcp__robinhood-trading__search,mcp__robinhood-trading__get_equity_quotes,mcp__robinhood-trading__get_crypto_quotes,mcp__robinhood-trading__get_currency_pairs,mcp__robinhood-trading__get_equity_fundamentals,mcp__robinhood-trading__get_financials,mcp__robinhood-trading__get_equity_historicals,mcp__robinhood-trading__get_earnings_calendar,mcp__robinhood-trading__get_equity_news,mcp__robinhood-trading__get_equity_tradability,mcp__robinhood-trading__get_sec_filing_index,mcp__robinhood-trading__get_sec_filing_facts,mcp__robinhood-trading__get_scans,mcp__robinhood-trading__run_scan"

DISALLOWED_TOOLS="mcp__robinhood-trading__place_equity_order,mcp__robinhood-trading__place_crypto_order,mcp__robinhood-trading__place_option_order,mcp__robinhood-trading__review_equity_order,mcp__robinhood-trading__review_option_order,mcp__robinhood-trading__preview_crypto_order,mcp__robinhood-trading__cancel_equity_order,mcp__robinhood-trading__cancel_crypto_order,mcp__robinhood-trading__cancel_option_order,mcp__robinhood-trading__exercise_option,mcp__robinhood-trading__cancel_option_exercise,mcp__robinhood-trading__create_watchlist,mcp__robinhood-trading__update_watchlist,mcp__robinhood-trading__add_to_watchlist,mcp__robinhood-trading__remove_from_watchlist,mcp__robinhood-trading__follow_watchlist,mcp__robinhood-trading__unfollow_watchlist,mcp__robinhood-trading__create_alert,mcp__robinhood-trading__update_alert,mcp__robinhood-trading__delete_alert,mcp__robinhood-trading__mark_alerts_read,mcp__robinhood-trading__create_scan,mcp__robinhood-trading__update_scan_config,mcp__robinhood-trading__update_scan_filters"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_EPOCH="$(date +%s)"

# Arms the PreToolUse write guard for the duration of the run.
export RH_AGENT_SCHEDULED_RUN=1
log "write scope: reports/, research/, logs/discovery/ (enforced by PreToolUse hook)"

PROMPT="$(cat prompts/weekly_discovery.md)

---

RUNTIME CONTEXT
Local time now: $(date '+%Y-%m-%d %H:%M %Z') (America/Chicago)

You may write to exactly these two places and nowhere else:

  1. DISCOVERY REPORT (required) -> $DISCOVERY_REPORT_PATH
     Sections per Step 7. Validated after the run. A report containing a
     DECISION: line, a proposed allocation, a decision_id, or approval language
     is a FAILURE, however good the research is.

  2. RESEARCH NOTES (as needed) -> $RESEARCH_DIR/<SYMBOL>.md
     Per Step 6, classification RESEARCH_INCOMPLETE. Never delete or cut down
     an existing note.

This run PROPOSES NOTHING. Change nothing else."

log "invoking $CLAUDE_BIN (discovery, read-only, proposes nothing)"
DISCOVERY_ENV_PATH="$DISCOVERY_REPORT_PATH" \
RESEARCH_ENV_DIR="$RESEARCH_DIR" \
"$CLAUDE_BIN" -p "$PROMPT" \
  --output-format json \
  --allowedTools "$ALLOWED_TOOLS" \
  --disallowedTools "$DISALLOWED_TOOLS" \
  >"$RAW_OUTPUT" 2>>"$RUN_LOG"
CLAUDE_EXIT=$?

FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DURATION=$(( $(date +%s) - START_EPOCH ))
log "claude exited $CLAUDE_EXIT after ${DURATION}s"

# --------------------------------------------------------------------------
# 3. Postflight — surface unchanged, archive intact, nothing out of scope
# --------------------------------------------------------------------------
SAFETY_EXIT=0
if ! "$PYTHON" scripts/check_scheduled_safety.py --postflight \
      --digest "$SAFETY_DIGEST" >>"$RUN_LOG" 2>&1; then
  SAFETY_EXIT=1
  log "FATAL: report-only POSTFLIGHT FAILED — the run modified protected state."
  log "       Inspect immediately: git status && git diff"
fi

# --------------------------------------------------------------------------
# 4. Record the discovery report and validate that it proposes nothing
# --------------------------------------------------------------------------
"$PYTHON" scripts/record_discovery_run.py \
  --report "$DISCOVERY_REPORT_PATH" \
  --research-dir "$RESEARCH_DIR" \
  --raw "$RAW_OUTPUT" \
  --started-at "$STARTED_AT" \
  --finished-at "$FINISHED_AT" \
  --duration "$DURATION" \
  --exit-code "$CLAUDE_EXIT" >>"$RUN_LOG" 2>&1
RECORD_EXIT=$?

if [ "$SAFETY_EXIT" -ne 0 ]; then
  exit 1
fi
if [ "$CLAUDE_EXIT" -ne 0 ]; then
  log "claude failed; see $RUN_LOG"
  exit 3
fi
if [ "$RECORD_EXIT" -ne 0 ]; then
  log "the discovery report was produced but failed its contract; see $RUN_LOG"
  exit 2
fi

log "weekly discovery complete: $DISCOVERY_REPORT_PATH"
exit 0
