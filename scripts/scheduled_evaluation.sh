#!/bin/bash
#
# Stage 7 — scheduled, REPORT-ONLY evaluation.
#
# Invoked by launchd (see scheduler/). Runs Claude Code noninteractively against
# prompts/scheduled_evaluation.md and writes one markdown report. It changes
# nothing else, and it proves that by digesting the safety surface before and
# after the run.
#
#   ./scripts/scheduled_evaluation.sh                  # normal run
#   ./scripts/scheduled_evaluation.sh --dry-run        # safety checks only, no Claude
#   ./scripts/scheduled_evaluation.sh --check-runtime  # can this environment run at all?
#   ./scripts/scheduled_evaluation.sh --slot pm        # label the run
#   ./scripts/scheduled_evaluation.sh --skip-gate      # bypass the capital gate (testing)
#
# Exit codes: 0 ok, 1 safety failure, 2 setup problem, 3 Claude failed.
#
# ENVIRONMENT INDEPENDENCE. Under launchd there is no .zshrc, no nvm.sh and no
# inherited Terminal environment, so nothing here may resolve an executable by
# name and hope. The installed plist carries CLAUDE_BIN, NODE_BIN and an
# absolute PATH, all resolved and probe-verified by
# scripts/resolve_launchd_runtime.py at install time. This script uses them when
# they are present and re-resolves them when it is run by hand.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

export TZ="America/Chicago"

SLOT="am"
DRY_RUN=0
CHECK_RUNTIME=0
SKIP_GATE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --slot) SLOT="${2:-am}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --check-runtime) CHECK_RUNTIME=1; shift ;;
    --skip-gate) SKIP_GATE=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

PYTHON="${PYTHON:-python3}"
CLAUDE_BIN="${CLAUDE_BIN:-}"
NODE_BIN="${NODE_BIN:-}"

# A Node-based Claude Code install needs `node` on PATH; a native binary does
# not. The plist supplies NODE_BIN only when the installer proved it necessary,
# and pinning its directory here is what keeps the run independent of nvm.
if [ -n "$NODE_BIN" ] && [ -x "$NODE_BIN" ]; then
  case ":$PATH:" in
    *":$(dirname "$NODE_BIN"):"*) : ;;
    *) PATH="$(dirname "$NODE_BIN"):$PATH"; export PATH ;;
  esac
fi

# Resolve claude to an absolute path, in decreasing order of trust:
#   1. CLAUDE_BIN from the installed plist (absolute, probe-verified at install)
#   2. whatever this shell has on PATH (an interactive run-now by a human)
#   3. the installer's resolver, which searches shell-independent locations
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
fi
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  CLAUDE_BIN="$("$PYTHON" scripts/resolve_launchd_runtime.py --print claude_bin --quiet 2>/dev/null || true)"
fi

REPORTS_DIR="$REPO_ROOT/reports"
RECOMMENDATIONS_DIR="$REPORTS_DIR/recommendations"
RESEARCH_DIR="$REPO_ROOT/research"
LOG_DIR="$REPO_ROOT/logs/scheduled"
mkdir -p "$REPORTS_DIR" "$RECOMMENDATIONS_DIR" "$RESEARCH_DIR" "$LOG_DIR" || exit 2

STAMP="$(date +%Y-%m-%d_%H%M)"
# Three tiers, three lifetimes:
#   REPORT_PATH  the detailed audit record for this run, kept forever
#   DIGEST_PATH  the ~1,000-word daily summary, replaced every run
#   RESEARCH_DIR standing per-candidate notes, carried across runs
REPORT_PATH="$REPORTS_DIR/${STAMP}.md"
DIGEST_PATH="$REPORTS_DIR/latest.md"
# Written only when the decision is a BUY. It is what makes a scheduled
# recommendation promotable without letting the scheduled run act on it.
RECOMMENDATION_PATH="$RECOMMENDATIONS_DIR/${STAMP}.json"
RUN_LOG="$LOG_DIR/${STAMP}_${SLOT}.log"
SAFETY_DIGEST="$(mktemp -t rh-agent-digest)"
RAW_OUTPUT="$(mktemp -t rh-agent-out)"
trap 'rm -f "$SAFETY_DIGEST" "$RAW_OUTPUT"' EXIT

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$RUN_LOG"; }

log "scheduled evaluation starting (slot=$SLOT, tz=$TZ, local=$(date '+%Y-%m-%d %H:%M %Z'))"
log "audit record: $REPORT_PATH"
log "daily digest: $DIGEST_PATH"
log "recommendation: $RECOMMENDATION_PATH (written only for a BUY)"
log "research dir: $RESEARCH_DIR"
log "runtime: claude=${CLAUDE_BIN:-UNRESOLVED} node=${NODE_BIN:-none} python=$PYTHON"
log "runtime: PATH=$PATH"

# --------------------------------------------------------------------------
# 0. Runtime: can this environment actually start Claude Code?
# --------------------------------------------------------------------------
# This is checked up front and by absolute path. The previous failure mode was
# a job that passed every safety check and then died on `claude: not found`,
# because the human who tested it had a shell that had already sourced nvm.
check_runtime() {
  if ! command -v "$PYTHON" >/dev/null 2>&1; then
    log "FATAL: '$PYTHON' is not executable in this environment."
    return 2
  fi

  if [ -z "$CLAUDE_BIN" ]; then
    log "FATAL: no Claude Code executable could be resolved."
    log "       launchd reads no .zshrc and sources no nvm.sh, so 'claude' working"
    log "       in your Terminal proves nothing about the scheduled environment."
    log "       Fix with: ./scheduler/install.sh verify"
    return 2
  fi
  case "$CLAUDE_BIN" in
    /*) : ;;
    *)
      log "FATAL: CLAUDE_BIN='$CLAUDE_BIN' is not an absolute path. A scheduled run"
      log "       must never depend on a PATH lookup for its interpreter."
      return 2
      ;;
  esac
  if [ ! -x "$CLAUDE_BIN" ]; then
    log "FATAL: CLAUDE_BIN='$CLAUDE_BIN' is not an executable file."
    return 2
  fi
  if [ -n "$NODE_BIN" ] && [ ! -x "$NODE_BIN" ]; then
    log "FATAL: NODE_BIN='$NODE_BIN' is not an executable file."
    return 2
  fi

  local version
  if ! version="$("$CLAUDE_BIN" --version 2>&1)"; then
    log "FATAL: '$CLAUDE_BIN --version' failed in this environment."
    printf '%s\n' "$version" | head -5 | tee -a "$RUN_LOG"
    log "       If this Claude Code install needs Node, re-run"
    log "       ./scheduler/install.sh verify so NODE_BIN is resolved and persisted."
    return 2
  fi
  log "runtime ok: $(printf '%s' "$version" | head -1)"
  return 0
}

# --dry-run deliberately skips this: it exercises the safety surface only and
# must stay usable on a machine with no Claude Code installed at all.
if [ "$DRY_RUN" -eq 0 ]; then
  check_runtime || exit 2
fi

# --------------------------------------------------------------------------
# 1. Preflight: may this run start at all?
# --------------------------------------------------------------------------
if ! "$PYTHON" scripts/check_scheduled_safety.py --preflight \
      --save-digest "$SAFETY_DIGEST" >>"$RUN_LOG" 2>&1; then
  log "FATAL: report-only preflight failed. Not running. See $RUN_LOG"
  exit 1
fi
log "preflight passed: all three switches closed, order tools denied, approval script denied"

# The status check must also succeed; corrupt state fails closed.
if ! "$PYTHON" scripts/check_status.py >>"$RUN_LOG" 2>&1; then
  log "FATAL: check_status.py failed (corrupt state fails closed). Not running."
  exit 1
fi

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
# 1a. Capital gate: is today's evaluation worth paying for?
# --------------------------------------------------------------------------
# The Claude invocation is the expensive part of this job and it is pointless on
# a day when nothing could be bought whatever it concluded. Two such days exist
# and both are decided by arithmetic, before any model runs:
#
#   exit 10  the month's authorization is spent          -> skip, silently
#   exit 11  authorization remains, settled cash cannot fund it -> skip, notify
#   exit 12  the broker could not be read                -> skip, notify
#
# None of these is a job failure: the schedule stays installed and tomorrow's
# run re-decides from scratch, so a new month or a deposit reactivates
# evaluation with no human action. --skip-gate exists for testing the rest of
# the pipeline and is never used by launchd.
# The snapshot is REGENERATED every run into state/, never read from a
# hand-written file. The previous version read a persisted broker.json that
# nothing refreshed, so a deposit could never flip FUNDING_REQUIRED to
# EVALUATE and an emptied account kept paying for evaluations.
GATE_SNAPSHOT="$REPO_ROOT/state/broker_snapshot.json"
GATE_NOTIFICATION="$(mktemp -t rh-agent-note)"
trap 'rm -f "$SAFETY_DIGEST" "$RAW_OUTPUT" "$GATE_NOTIFICATION"' EXIT

run_gate() {
  # Phase one needs no broker data at all, so an exhausted month costs nothing.
  "$PYTHON" scripts/check_capital_gate.py --authorization-only \
    --emit-notification "$GATE_NOTIFICATION" >>"$RUN_LOG" 2>&1
  local phase_one=$?
  if [ "$phase_one" -eq 10 ]; then
    # The month is spent. Normally that costs nothing more — but near the
    # month's final tradable day the funding reminder needs a real balance, and
    # a reminder that guesses is worse than none. Refresh once, re-run, return.
    if "$PYTHON" -c "import sys;sys.path.insert(0,'.');\
import importlib.util as u;s=u.spec_from_file_location('g','scripts/check_capital_gate.py');\
m=u.module_from_spec(s);s.loader.exec_module(m);sys.exit(0 if m.near_month_end() else 1)"; then
      log "near month end: refreshing the snapshot so the funding reminder is accurate"
      ./scripts/refresh_broker_snapshot.sh --out "$GATE_SNAPSHOT" >>"$RUN_LOG" 2>&1 || true
      "$PYTHON" scripts/check_capital_gate.py --authorization-only \
        --snapshot "$GATE_SNAPSHOT" \
        --emit-notification "$GATE_NOTIFICATION" >>"$RUN_LOG" 2>&1
    fi
    return 10
  fi
  if [ "$phase_one" -ne 0 ]; then
    return "$phase_one"
  fi

  # Authorization remains, so the one number the repository cannot compute —
  # settled cash — has to be fetched. A minimal Claude call with two read-only
  # account tools, which is what makes restored funding self-healing: every run
  # re-reads the account, so a deposit is picked up by the next morning's run
  # with no human action.
  log "refreshing the broker snapshot (2 read-only account tools)"
  if ! ./scripts/refresh_broker_snapshot.sh --out "$GATE_SNAPSHOT" >>"$RUN_LOG" 2>&1; then
    log "snapshot refresh failed; the gate will fail closed rather than guess"
    rm -f "$GATE_SNAPSHOT"
  fi

  "$PYTHON" scripts/check_capital_gate.py \
    --snapshot "$GATE_SNAPSHOT" \
    --emit-notification "$GATE_NOTIFICATION" >>"$RUN_LOG" 2>&1
}

if [ "$SKIP_GATE" -eq 0 ]; then
  run_gate
  GATE_EXIT=$?

  case "$GATE_EXIT" in
    0)  log "capital gate: EVALUATE — proceeding" ;;
    10) log "capital gate: AUTHORIZATION_EXHAUSTED — skipping the evaluation."
        log "              A fully-used month is a normal outcome. The schedule stays"
        log "              installed and next month's authorization resumes it."
        if [ -s "$GATE_NOTIFICATION" ]; then
          "$PYTHON" scripts/notify.py --from-json "$GATE_NOTIFICATION" >>"$RUN_LOG" 2>&1
        fi
        exit 0 ;;
    11) log "capital gate: FUNDING_REQUIRED — skipping the evaluation and notifying."
        "$PYTHON" scripts/notify.py --from-json "$GATE_NOTIFICATION" >>"$RUN_LOG" 2>&1
        exit 0 ;;
    12) log "capital gate: BROKER_UNREADABLE — skipping. A failed or stale read is"
        log "              never treated as a funded account."
        exit 0 ;;
    *)  log "capital gate returned an unexpected code $GATE_EXIT; failing closed."
        exit 1 ;;
  esac
fi

# --------------------------------------------------------------------------
# 2. Run Claude Code noninteractively, report-only.
# --------------------------------------------------------------------------
# Only read-only tools are allowed. The order tools and every account-mutating
# tool stay denied by .claude/settings.json, which the preflight just verified;
# --disallowedTools is belt and braces on top of that.
#
# WebSearch and WebFetch are read-only and are here on purpose. Robinhood stays
# authoritative for holdings, cost basis, pair eligibility, tradability, orders
# and execution — nothing external may contradict it on those. But Robinhood
# exposes no network-usage, issuance, protocol-development, security or
# regulatory data, and a crypto research package cannot be completed without
# them. Declaring RESEARCH_INCOMPLETE because Robinhood lacks a field that a
# primary source publishes openly is a research failure, not a safe default.
#
# `Write` is listed unscoped because a path specifier on a file tool matches
# nothing in this CLI version (see the write-scope note above). Its scope comes
# from the PreToolUse hook instead. `Edit` is deliberately absent: the run has no
# need to modify a file in place, so it does not get the capability.
ALLOWED_TOOLS="Read,Glob,Grep,Bash(python3 scripts/check_status.py),Bash(python3 scripts/validate_plan.py:*),Bash(python3 scripts/emit_buy_scaffold.py:*),Write,WebFetch,mcp__robinhood-trading__get_accounts,mcp__robinhood-trading__get_portfolio,mcp__robinhood-trading__get_equity_positions,mcp__robinhood-trading__get_crypto_positions,mcp__robinhood-trading__get_equity_orders,mcp__robinhood-trading__get_crypto_orders,mcp__robinhood-trading__get_equity_quotes,mcp__robinhood-trading__get_crypto_quotes,mcp__robinhood-trading__get_currency_pairs,mcp__robinhood-trading__get_equity_fundamentals,mcp__robinhood-trading__get_financials,mcp__robinhood-trading__get_earnings_results,mcp__robinhood-trading__get_earnings_calendar,mcp__robinhood-trading__get_equity_news,mcp__robinhood-trading__get_equity_historicals,mcp__robinhood-trading__get_sec_filing_index,mcp__robinhood-trading__get_sec_filing_facts,mcp__robinhood-trading__get_sec_filing,mcp__robinhood-trading__get_watchlists,mcp__robinhood-trading__get_watchlist_items,WebSearch"

DISALLOWED_TOOLS="mcp__robinhood-trading__place_equity_order,mcp__robinhood-trading__place_crypto_order,mcp__robinhood-trading__place_option_order,mcp__robinhood-trading__review_equity_order,mcp__robinhood-trading__review_option_order,mcp__robinhood-trading__preview_crypto_order,mcp__robinhood-trading__cancel_equity_order,mcp__robinhood-trading__cancel_crypto_order,mcp__robinhood-trading__cancel_option_order,mcp__robinhood-trading__exercise_option,mcp__robinhood-trading__cancel_option_exercise,mcp__robinhood-trading__create_watchlist,mcp__robinhood-trading__update_watchlist,mcp__robinhood-trading__add_to_watchlist,mcp__robinhood-trading__remove_from_watchlist,mcp__robinhood-trading__follow_watchlist,mcp__robinhood-trading__unfollow_watchlist,mcp__robinhood-trading__create_alert,mcp__robinhood-trading__update_alert,mcp__robinhood-trading__delete_alert,mcp__robinhood-trading__mark_alerts_read,mcp__robinhood-trading__create_scan,mcp__robinhood-trading__update_scan_config,mcp__robinhood-trading__update_scan_filters"

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_EPOCH="$(date +%s)"

# WRITE SCOPE. This marker turns on the PreToolUse write guard registered in
# .claude/settings.json (scripts/scheduled_write_guard.py), which denies any
# Write/Edit outside reports/, research/ and the scheduled log files. It is set
# only here, so interactive sessions in this repository are unaffected.
#
# The obvious alternative -- a path specifier in --allowedTools, e.g.
# "Write(reports/**)" -- does NOT work: tested against Claude Code 2.1.263, a
# scoped file-tool rule matches nothing and denied writes to the very directory
# it named, while bare "Write" allowed them everywhere. Hence the hook.
#
# The run cannot clear this: it lives in the environment of the CLI process that
# spawns the hook, and the run's Bash access is two read-only commands. And the
# postflight tree check reports any out-of-scope write even if the hook were
# bypassed -- prevention here, detection there.
export RH_AGENT_SCHEDULED_RUN=1
log "write scope: reports/, research/, logs/scheduled/, logs/scheduled_usage.jsonl (enforced by PreToolUse hook)"

PROMPT="$(cat prompts/scheduled_evaluation.md)

---

RUNTIME CONTEXT
Local time now: $(date '+%Y-%m-%d %H:%M %Z') (America/Chicago)
Scheduled slot: $SLOT

You may write to exactly these three places and nowhere else:

  1. DETAILED AUDIT RECORD (required) -> $REPORT_PATH
     The full record of this run. Sections per Step 10.

  2. DAILY DIGEST (required)          -> $DIGEST_PATH
     The concise human-facing summary. Target 800-1200 words. Sections and
     content requirements per Step 12; validated after the run, and a digest
     missing the decision, the authorization, the five-way comparison, the
     allocation, the confidence or the change triggers is a failure.

  3. RESEARCH NOTES (as needed)       -> $RESEARCH_DIR/<SYMBOL>.md
     Standing per-candidate research, per Step 11. Read these first; refresh
     only what moved. Do not delete or gut an existing note.

  4. RECOMMENDATION PAYLOAD (BUY only) -> $RECOMMENDATION_PATH
     Only when the decision is SINGLE_BUY or SPLIT_BUY_PLAN. One complete,
     ordinary BUY payload per leg, per Step 13, with NO decision_id and no
     approval field. Omit this file entirely for a WAIT.

Do not write research/INDEX.md — it is regenerated automatically after the run.
This is an unattended, REPORT-ONLY run. Change nothing else."

# Re-digest the safety surface NOW, at the moment the model gains control.
#
# The postflight asks one question: did the *model* change anything it must not
# have? So the baseline has to be the state the model starts from. Digesting
# before the pre-run steps made the snapshot refresh — a deterministic shell
# step that legitimately writes state/broker_snapshot.json, outside the model's
# write scope by design — look like an out-of-scope write by the run. The check
# was right and the ordering was wrong.
#
# The first preflight above still does its own job: it refuses to start at all
# if the switches are open or an order tool is exposed.
if ! "$PYTHON" scripts/check_scheduled_safety.py --preflight \
      --save-digest "$SAFETY_DIGEST" >>"$RUN_LOG" 2>&1; then
  log "FATAL: safety preflight failed after the pre-run steps. Not invoking Claude."
  exit 1
fi
log "safety surface re-digested after the pre-run steps (model-entry baseline)"

log "invoking $CLAUDE_BIN (report-only, restricted tools)"
REPORT_ENV_PATH="$REPORT_PATH" \
DIGEST_ENV_PATH="$DIGEST_PATH" \
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
# 3. Postflight: did the run change anything it must not have?
# --------------------------------------------------------------------------
SAFETY_EXIT=0
if ! "$PYTHON" scripts/check_scheduled_safety.py --postflight \
      --digest "$SAFETY_DIGEST" >>"$RUN_LOG" 2>&1; then
  SAFETY_EXIT=1
  log "FATAL: report-only POSTFLIGHT FAILED — the run modified protected state."
  log "       Inspect immediately: git status && git diff"
fi

# --------------------------------------------------------------------------
# 4. Record the report, usage metadata, and update latest.md
# --------------------------------------------------------------------------
"$PYTHON" scripts/record_scheduled_run.py \
  --report "$REPORT_PATH" \
  --digest "$DIGEST_PATH" \
  --research-dir "$RESEARCH_DIR" \
  --recommendation "$RECOMMENDATION_PATH" \
  --emit-notification "$GATE_NOTIFICATION" \
  --raw "$RAW_OUTPUT" \
  --started-at "$STARTED_AT" \
  --finished-at "$FINISHED_AT" \
  --duration "$DURATION" \
  --exit-code "$CLAUDE_EXIT" \
  --slot "$SLOT" >>"$RUN_LOG" 2>&1
RECORD_EXIT=$?

# --------------------------------------------------------------------------
# 4a. Post-run promotion: carry a valid recommendation into the pipeline
# --------------------------------------------------------------------------
# This runs only after the Claude process has exited, with a different marker,
# so it is the deterministic shell promoting a recommendation rather than a
# model advancing its own advice. Promotion re-checks every guardrail from
# scratch — freshness, banner agreement, authorization, broker orders, settled
# cash, quote drift, tradability — and mints at most a PROPOSED decision. It
# creates no approval and no execution authority, and a failure here is
# reported rather than retried.
if [ -s "$RECOMMENDATION_PATH" ]; then
  # The capital gate's snapshot cannot answer promotion's questions: it carries
  # settled cash and nothing else, on purpose, because it runs every morning
  # including the ~90% that end in WAIT. Promotion needs a refreshed quote per
  # asset, tradability, fractional eligibility and the broker's own order
  # history, so a BUY day — and only a BUY day — pays for a second, still
  # read-only gather. The two snapshots stay separate and disjoint.
  log "gathering the promotion snapshot (read-only; quotes, orders, tradability)"
  PROMOTION_SNAPSHOT="$REPO_ROOT/state/promotion_snapshot.json"
  if RH_AGENT_SCHEDULED_RUN= RH_AGENT_SCHEDULED_POST_RUN= \
      "$REPO_ROOT/scripts/refresh_promotion_snapshot.sh" \
        --recommendation "$RECOMMENDATION_PATH" \
        --out "$PROMOTION_SNAPSHOT" >>"$RUN_LOG" 2>&1; then
    log "promotion snapshot ready: $PROMOTION_SNAPSHOT"
  else
    log "promotion snapshot could not be gathered; promotion will fail closed"
    PROMOTION_SNAPSHOT=""
  fi

  log "promoting the recommendation (post-run, all guardrails re-checked)"
  if RH_AGENT_SCHEDULED_POST_RUN=1 RH_AGENT_SCHEDULED_RUN= \
      "$PYTHON" scripts/promote_latest_recommendation.py \
        --recommendation "$RECOMMENDATION_PATH" \
        ${PROMOTION_SNAPSHOT:+--snapshot "$PROMOTION_SNAPSHOT"} >>"$RUN_LOG" 2>&1; then
    log "promotion: a PROPOSED decision was minted (no approval, no ticket)"
  else
    log "promotion did not mint a decision; see $RUN_LOG"
    RH_AGENT_NOTE_PATH="$GATE_NOTIFICATION" "$PYTHON" - <<'PYEOF' >>"$RUN_LOG" 2>&1 || true
import json, os, sys
sys.path.insert(0, os.getcwd())
from src.notifications import NOTIFY_PROMOTION_FAILED, failure
note = failure(NOTIFY_PROMOTION_FAILED,
               "Recommendation could not be promoted",
               "A scheduled BUY was recommended but promotion refused it. "
               "The recommendation stands; nothing was approved or submitted.")
with open(os.environ["RH_AGENT_NOTE_PATH"], "w", encoding="utf-8") as h:
    json.dump(note.to_dict(), h, indent=2, ensure_ascii=False)
PYEOF
  fi
fi

# An actionable BUY is the one thing worth interrupting a human for. A WAIT
# writes no notification file, so this is silent on most days.
if [ -s "$GATE_NOTIFICATION" ]; then
  "$PYTHON" scripts/notify.py --from-json "$GATE_NOTIFICATION" >>"$RUN_LOG" 2>&1 \
    || log "notification delivery failed (the report is still written)"
fi

if [ "$SAFETY_EXIT" -ne 0 ]; then
  exit 1
fi
if [ "$CLAUDE_EXIT" -ne 0 ]; then
  log "claude failed; see $RUN_LOG"
  exit 3
fi
if [ "$RECORD_EXIT" -ne 0 ]; then
  log "report was produced but could not be recorded; see $RUN_LOG"
  exit 2
fi

log "scheduled evaluation complete"
log "  audit record: $REPORT_PATH"
log "  daily digest: $DIGEST_PATH"
exit 0
