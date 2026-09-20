#!/bin/bash
#
# Gather the read-only broker facts the deterministic promotion step re-checks.
#
#   ./scripts/refresh_promotion_snapshot.sh --symbols SNDK
#   ./scripts/refresh_promotion_snapshot.sh --symbols SNDK,BTC-USD --out state/x.json
#   ./scripts/refresh_promotion_snapshot.sh --recommendation reports/recommendations/x.json
#
# WHY THIS EXISTS, AND WHY IT IS NOT THE CAPITAL GATE'S SNAPSHOT.
#
# On 2026-09-17 a scheduled BUY could not be promoted. Part of that was a schema
# defect, but underneath it sat a second problem that the schema defect had been
# masking: the runner handed promotion `state/broker_snapshot.json`, and that
# file structurally cannot answer promotion's questions. It is gathered by two
# account tools and carries settled cash, and nothing else. Promotion re-checks a
# refreshed quote per asset, quote age, tradability, fractional eligibility,
# account-type tradability, crypto halt state and minimum order size, and the
# broker's own order history for the month. None of that is in there. So the
# post-run promotion had never been able to mint a decision, for any payload.
#
# The obvious fix — teach the morning gate to gather all of it — is the wrong
# one. The gate exists to decide whether an expensive evaluation is worth paying
# for at all, and it runs every weekday morning including the ~90% that end in
# WAIT and promote nothing. Making it eight tools instead of two would charge
# every day for what a BUY day needs. So there are two snapshots, gathered by two
# different jobs, and neither weakens the other:
#
#   state/broker_snapshot.json     the gate. Two tools. Every morning.
#   state/promotion_snapshot.json  this. Read-only, broader, and only on the
#                                  days a BUY was actually recommended.
#
# WHAT IT MAY DO. Read-only account and market tools plus Write, nothing else.
# Every order tool is explicitly denied, exactly as in the evaluation run. The
# write guard is armed in PROMOTION-SNAPSHOT mode, so this process may write
# exactly one path and not the gate's file, not reports, not research. It
# approves nothing, mints nothing, and has no order path.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

OUT="$REPO_ROOT/state/promotion_snapshot.json"
SYMBOLS=""
RECOMMENDATION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:-$OUT}"; shift 2 ;;
    --symbols) SYMBOLS="${2:-}"; shift 2 ;;
    --recommendation) RECOMMENDATION="${2:-}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

PYTHON="${PYTHON:-python3}"

# The symbols to quote come from the recommendation itself when one is given, so
# the runner never has to parse JSON in shell.
if [ -z "$SYMBOLS" ] && [ -n "$RECOMMENDATION" ]; then
  SYMBOLS="$("$PYTHON" - "$RECOMMENDATION" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        legs = (json.load(handle) or {}).get("legs") or []
except (OSError, ValueError):
    raise SystemExit(1)
seen = []
for leg in legs:
    if not isinstance(leg, dict):
        continue
    ticker = str(leg.get("ticker") or leg.get("symbol") or "").strip().upper()
    if ticker and ticker not in seen:
        seen.append(ticker)
print(",".join(seen))
PYEOF
)" || SYMBOLS=""
fi

if [ -z "$SYMBOLS" ]; then
  echo "no symbols to gather: pass --symbols or a --recommendation naming legs" >&2
  exit 2
fi

CLAUDE_BIN="${CLAUDE_BIN:-}"
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
fi
if [ -z "$CLAUDE_BIN" ] || [ ! -x "$CLAUDE_BIN" ]; then
  echo "no Claude Code executable could be resolved" >&2
  exit 2
fi

mkdir -p "$(dirname "$OUT")" || exit 2

# Recorded BEFORE the subprocess, so there is no window in which a leftover file
# could pass as a refresh.
STARTED_AT="$("$PYTHON" -c 'import time; print(time.time())')"
PRIOR_DIGEST="$(shasum "$OUT" 2>/dev/null | awk '{print $1}')"

# Read-only account, order, quote and tradability tools. Every order tool is
# denied below in addition to being absent here.
ALLOWED="mcp__robinhood-trading__get_accounts,mcp__robinhood-trading__get_portfolio,mcp__robinhood-trading__get_equity_orders,mcp__robinhood-trading__get_crypto_orders,mcp__robinhood-trading__get_equity_quotes,mcp__robinhood-trading__get_crypto_quotes,mcp__robinhood-trading__get_equity_tradability,mcp__robinhood-trading__get_currency_pairs,Write"
DISALLOWED="mcp__robinhood-trading__place_equity_order,mcp__robinhood-trading__place_crypto_order,mcp__robinhood-trading__place_option_order,mcp__robinhood-trading__review_equity_order,mcp__robinhood-trading__review_option_order,mcp__robinhood-trading__preview_crypto_order,mcp__robinhood-trading__cancel_equity_order,mcp__robinhood-trading__cancel_crypto_order,mcp__robinhood-trading__cancel_option_order,mcp__robinhood-trading__exercise_option,mcp__robinhood-trading__cancel_option_exercise,mcp__robinhood-trading__create_watchlist,mcp__robinhood-trading__update_watchlist,mcp__robinhood-trading__add_to_watchlist,mcp__robinhood-trading__remove_from_watchlist,mcp__robinhood-trading__follow_watchlist,mcp__robinhood-trading__unfollow_watchlist,mcp__robinhood-trading__create_alert,mcp__robinhood-trading__update_alert,mcp__robinhood-trading__delete_alert,mcp__robinhood-trading__mark_alerts_read,mcp__robinhood-trading__create_scan,mcp__robinhood-trading__update_scan_config,mcp__robinhood-trading__update_scan_filters"

PROMPT="Gather read-only broker facts for the promotion re-checks. Symbols: $SYMBOLS

Call get_accounts and get_portfolio and find the account with agentic_allowed
true. Call get_equity_orders and get_crypto_orders for that account. For each
symbol above: if it is a hyphenated pair such as BTC-USD call get_crypto_quotes
and get_currency_pairs; otherwise call get_equity_quotes and
get_equity_tradability.

Write EXACTLY this JSON to $OUT and nothing else — no prose, no other file:

{
  \"schema_version\": 1,
  \"as_of\": \"<current UTC time as YYYY-MM-DDTHH:MM:SSZ>\",
  \"account_is_agentic\": true,
  \"account_masked\": \"<last four digits, masked as ••••NNNN>\",
  \"account_type\": \"<that account's type>\",
  \"cash_usd\": \"<get_portfolio cash, 2dp>\",
  \"unsettled_funds_usd\": \"<that account's unsettled_funds, 2dp>\",
  \"buying_power_usd\": \"<get_portfolio buying_power.buying_power, 2dp>\",
  \"equity_orders\": [ <the equity orders as returned, or [] if there are none> ],
  \"crypto_orders\": [ <the crypto orders as returned, or [] if there are none> ],
  \"assets\": {
    \"<SYMBOL>\": {
      \"quote_price_usd\": \"<last traded price, full precision>\",
      \"quote_timestamp\": \"<ISO-8601 UTC of that quote>\",
      \"tradable\": <true|false>,
      \"fractional_tradable\": <true|false>,
      \"account_type_tradable\": <true|false>,
      \"crypto_pair_halted\": <true|false, crypto only>,
      \"crypto_min_order_size\": \"<minimum order size, crypto only>\"
    }
  },
  \"read_errors\": [ <one short string per tool call that failed, or []> ]
}

One entry under assets per symbol above, keyed by the symbol exactly as given.
Omit the two crypto-only fields for an equity.

quote_timestamp: use the as-of time the quote response itself carries. If the
response carries none, use the same current UTC time you wrote in as_of — you
have just fetched the quote, so that is when it was read. This is the one field
you may derive; it is a fact about when you called the tool, not about the
market. Never derive a price, a tradability flag or a balance.

If a tool call fails, record it in read_errors rather than guessing a value — a
missing field fails closed and an invented one does not. Never write a full
account number. Do not call any other tool. Do not place, review, preview or
cancel anything."

# Armed in PROMOTION-SNAPSHOT mode: this run may write exactly one path, and it
# is not the capital gate's. The three scopes are disjoint on purpose, so no
# process can forge another's output.
export RH_AGENT_PROMOTION_SNAPSHOT=1

"$CLAUDE_BIN" -p "$PROMPT" \
  --allowedTools "$ALLOWED" \
  --disallowedTools "$DISALLOWED" >/dev/null 2>&1
CLAUDE_EXIT=$?

if [ "$CLAUDE_EXIT" -ne 0 ]; then
  echo "promotion snapshot gather failed (claude exited $CLAUDE_EXIT)" >&2
  rm -f "$OUT"
  exit 1
fi

# Did THIS invocation produce a usable snapshot? Same reasoning as the capital
# gate's refresh: a subprocess that exits 0 having written nothing must not leave
# a stale file behind to be mistaken for a current reading.
"$PYTHON" - "$OUT" "$STARTED_AT" "$PRIOR_DIGEST" "$SYMBOLS" <<'PYEOF'
import hashlib, json, os, sys
sys.path.insert(0, os.getcwd())
from src.promotion import backfill_quote_timestamps, promotion_refresh_problems

path, started_at, prior_digest, symbols = (
    sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4])
wanted = [s for s in symbols.split(",") if s]

snapshot, mtime = None, None
if os.path.exists(path):
    mtime = os.path.getmtime(path)
    try:
        with open(path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, ValueError) as exc:
        print("the gathered snapshot is unreadable: %s" % exc, file=sys.stderr)

# A quote the gather read but did not date is dated here, from the moment this
# invocation began — see backfill_quote_timestamps. Conservative by construction:
# it can only make a quote look older. Written back so promotion reads the same
# file the operator can inspect.
if snapshot is not None:
    from datetime import datetime, timezone
    filled = backfill_quote_timestamps(
        snapshot, datetime.fromtimestamp(started_at, timezone.utc))
    if filled:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, ensure_ascii=False)
        print("dated %s from the gather start (the quote response carried none)"
              % ", ".join(filled))

problems = promotion_refresh_problems(snapshot, mtime, started_at, wanted)

if not problems and prior_digest and mtime is not None:
    with open(path, "rb") as handle:
        if hashlib.sha1(handle.read()).hexdigest() == prior_digest:
            problems.append(
                "the snapshot is byte-identical to the one that existed before "
                "this invocation, so nothing new was read from the broker")

if problems:
    for problem in problems:
        print("gather failed: %s" % problem, file=sys.stderr)
    if os.path.exists(path):
        try:
            os.remove(path)
            print("removed the unusable snapshot at %s so it cannot be mistaken "
                  "for a current reading" % path, file=sys.stderr)
        except OSError as exc:
            print("could not remove %s: %s" % (path, exc), file=sys.stderr)
    raise SystemExit(1)

print("promotion snapshot gathered: %s, %d asset(s), written by this invocation"
      % (path, len(snapshot.get("assets") or {})))
PYEOF
