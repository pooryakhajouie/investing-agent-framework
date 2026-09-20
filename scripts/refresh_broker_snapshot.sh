#!/bin/bash
#
# Refresh state/broker_snapshot.json with current read-only account facts.
#
#   ./scripts/refresh_broker_snapshot.sh [--out PATH]
#
# WHY THIS EXISTS. The capital gate needs one number the repository cannot
# compute for itself: settled cash in the Agentic account. The first version of
# the gate read a persisted `broker.json` that nothing regenerated — a file
# written by hand during a live-purchase attempt. That is not a snapshot, it is
# a memory, and it fails in both directions silently: expensive evaluations
# against an account that has been empty for a week, or evaluation suppressed
# forever after a deposit the file never learned about.
#
# No Python in src/ may reach the network (an AST test enforces it), and MCP
# tools exist only inside a Claude Code process. So a refresh is necessarily a
# Claude invocation — but a *minimal* one: two read-only account tools, no
# research, no web access, no market data, a three-line prompt. It costs a
# fraction of an evaluation, which is the thing the gate exists to avoid.
#
# It is granted NO order tools, NO write access to anything but the snapshot
# path, and no ability to arm execution. Its entire output is one JSON object.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

OUT="$REPO_ROOT/state/broker_snapshot.json"
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:-$OUT}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

PYTHON="${PYTHON:-python3}"
CLAUDE_BIN="${CLAUDE_BIN:-}"
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
fi
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  echo "no Claude Code executable could be resolved" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUT")" || exit 2

# The invocation start. Everything below is judged against it: a snapshot that
# predates this moment is a leftover, not a refresh. Recorded BEFORE the
# subprocess so there is no window in which a stale file could qualify.
STARTED_AT="$(python3 -c 'import time; print(time.time())')"
PRIOR_DIGEST="$(shasum "$OUT" 2>/dev/null | awk '{print $1}')"

# Only the two tools that answer "how much settled cash is in the Agentic
# account". Everything else, including every order tool, stays unavailable.
ALLOWED="mcp__robinhood-trading__get_accounts,mcp__robinhood-trading__get_portfolio,Write"
DISALLOWED="mcp__robinhood-trading__place_equity_order,mcp__robinhood-trading__place_crypto_order,mcp__robinhood-trading__place_option_order,mcp__robinhood-trading__review_equity_order,mcp__robinhood-trading__review_option_order,mcp__robinhood-trading__preview_crypto_order,mcp__robinhood-trading__cancel_equity_order,mcp__robinhood-trading__cancel_crypto_order,mcp__robinhood-trading__cancel_option_order,mcp__robinhood-trading__exercise_option,mcp__robinhood-trading__cancel_option_exercise"

PROMPT="Call get_accounts and get_portfolio. Find the account with agentic_allowed
true. Write EXACTLY this JSON to $OUT and nothing else — no prose, no other file:

{
  \"as_of\": \"<current UTC time as YYYY-MM-DDTHH:MM:SSZ>\",
  \"account_is_agentic\": true,
  \"account_masked\": \"<last four digits, masked as ••••NNNN>\",
  \"account_type\": \"<that account's type>\",
  \"cash_usd\": \"<get_portfolio cash, 2dp>\",
  \"unsettled_funds_usd\": \"<that account's unsettled_funds, 2dp>\",
  \"buying_power_usd\": \"<get_portfolio buying_power.buying_power, 2dp>\"
}

Never write a full account number. Do not call any other tool. Do not place,
review, preview or cancel anything."

# The write guard is armed in SNAPSHOT-REFRESH mode: this run may write exactly
# state/broker_snapshot.json and nothing else. Note this is NOT the scheduled-run
# scope — a refresh may not write reports or research, and a scheduled
# evaluation may not write the snapshot. The two scopes are disjoint on purpose,
# so neither process can forge the other's output.
export RH_AGENT_SNAPSHOT_REFRESH=1

"$CLAUDE_BIN" -p "$PROMPT" \
  --allowedTools "$ALLOWED" \
  --disallowedTools "$DISALLOWED" >/dev/null 2>&1
CLAUDE_EXIT=$?

if [ "$CLAUDE_EXIT" -ne 0 ]; then
  echo "snapshot refresh failed (claude exited $CLAUDE_EXIT)" >&2
  # Clear any pre-existing snapshot: a failed refresh must not leave a stale
  # reading behind for the gate to find and believe.
  rm -f "$OUT"
  exit 1
fi

# Did THIS invocation produce a usable snapshot?
#
# "Does a parseable snapshot exist" is the wrong question, and asking it caused a
# real failure: a subprocess exited 0 having written nothing, the previous file
# was still on disk, and this script announced "snapshot refreshed" against a
# file three days old. Only the capital gate's own freshness check stopped it
# being used.
#
# So the checks are: the file must exist, must post-date this invocation, must be
# fresh on its own as_of, and must carry the fields the gate depends on. Any
# failure deletes the file — a stale snapshot left on disk is exactly what the
# next caller would mistake for a good one — and returns non-zero.
"$PYTHON" - "$OUT" "$STARTED_AT" "$PRIOR_DIGEST" <<'PYEOF'
import hashlib, json, os, sys
sys.path.insert(0, os.getcwd())
from src.capital_gate import MAX_SNAPSHOT_AGE_SECONDS, refresh_problems

path, started_at, prior_digest = sys.argv[1], float(sys.argv[2]), sys.argv[3]

snapshot, mtime = None, None
if os.path.exists(path):
    mtime = os.path.getmtime(path)
    try:
        with open(path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, ValueError) as exc:
        print("refreshed snapshot is unreadable: %s" % exc, file=sys.stderr)

problems = refresh_problems(snapshot, mtime, started_at)

# Content-identity check, in case a filesystem writes an identical file with a
# new mtime: unchanged bytes mean the subprocess produced nothing new.
if not problems and prior_digest and mtime is not None:
    with open(path, "rb") as handle:
        if hashlib.sha1(handle.read()).hexdigest() == prior_digest:
            problems.append(
                "the snapshot is byte-identical to the one that existed before "
                "this invocation, so nothing new was read from the broker")

if problems:
    for problem in problems:
        print("refresh failed: %s" % problem, file=sys.stderr)
    if os.path.exists(path):
        try:
            os.remove(path)
            print("removed the unusable snapshot at %s so it cannot be "
                  "mistaken for a current reading" % path, file=sys.stderr)
        except OSError as exc:
            print("could not remove %s: %s" % (path, exc), file=sys.stderr)
    raise SystemExit(1)

age = (__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
       - __import__("datetime").datetime.fromisoformat(
           snapshot["as_of"].replace("Z", "+00:00"))).total_seconds()
print("snapshot refreshed: %s, %.0fs old, written by this invocation" % (path, age))
PYEOF
