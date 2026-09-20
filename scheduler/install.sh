#!/bin/bash
#
# Enable / disable the REPORT-ONLY scheduled evaluation on macOS (launchd).
#
#   ./scheduler/install.sh enable                 # weekday 10:30 AM America/Chicago
#   ./scheduler/install.sh enable --pm            # also add the 2:00 PM scan
#   ./scheduler/install.sh enable --discovery     # weekly Saturday 9:00 discovery
#   ./scheduler/install.sh disable [--pm|--discovery]
#   ./scheduler/install.sh status                 # what is loaded right now
#   ./scheduler/install.sh verify [--pm|--discovery]   # test it, install nothing
#   ./scheduler/install.sh run-now [--discovery]  # run once immediately, by hand
#
# THREE JOBS, DIFFERENT AUTHORITY. The weekday evaluation (am/pm) may recommend
# WAIT / SINGLE_BUY / SPLIT_BUY_PLAN. The weekly discovery job may not even do
# that: it widens the candidate universe and proposes nothing at all. None of
# the three can approve or submit anything.
#
# The scheduled job is report-only: it never enables execution, never approves
# anything, never exposes an order tool and never submits anything. Enabling the
# schedule does NOT enable trading — the three execution switches stay closed
# and there is still no order-submission path in this repository.
#
# EXECUTABLE RESOLUTION. launchd starts a job with a minimal PATH, no .zshrc, no
# nvm.sh and nothing inherited from a Terminal. `run-now` succeeding from an
# interactive shell therefore proves nothing about 10:30 tomorrow. So install
# resolves the absolute `claude` executable (and a Node runtime, if this install
# needs one), verifies each by *running* it under that minimal environment, and
# writes the results into the plist as CLAUDE_BIN, NODE_BIN and an absolute
# PATH. If they cannot be resolved and verified, installation stops rather than
# scheduling a job that will fail unattended.
#
# `verify` does the whole of that — render, lint, and execute the rendered
# plist's own program under `env -i` with only the plist's own environment —
# without loading anything into launchd.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"

LABEL_AM="com.robinhood-agent.evaluation"
LABEL_PM="com.robinhood-agent.evaluation-pm"
LABEL_DISCOVERY="com.robinhood-agent.discovery"

ACTION="${1:-status}"
shift 2>/dev/null || true

SLOT="am"
for arg in "$@"; do
  case "$arg" in
    --pm) SLOT="pm" ;;
    --am) SLOT="am" ;;
    --discovery) SLOT="discovery" ;;
  esac
done

case "$SLOT" in
  pm)        LABEL="$LABEL_PM";        SLOT_FLAG=" --pm" ;;
  discovery) LABEL="$LABEL_DISCOVERY"; SLOT_FLAG=" --discovery" ;;
  *)         LABEL="$LABEL_AM";        SLOT_FLAG="" ;;
esac
TEMPLATE="$REPO_ROOT/scheduler/${LABEL}.plist"
TARGET="$AGENTS_DIR/${LABEL}.plist"
UID_NUM="$(id -u)"

RESOLVER="$REPO_ROOT/scripts/resolve_launchd_runtime.py"
# The discovery job has its own runner, with strictly less authority.
if [ "$SLOT" = "discovery" ]; then
  RUNNER="$REPO_ROOT/scripts/weekly_discovery.sh"
else
  RUNNER="$REPO_ROOT/scripts/scheduled_evaluation.sh"
fi

# Resolve claude/node to absolute, probe-verified paths and write them into a
# plist. Any failure here is fatal by design: a job whose interpreter cannot be
# resolved without shell initialisation must not be scheduled.
render_to() {
  local template="$1" output="$2"
  mkdir -p "$(dirname "$output")" || exit 2
  if ! python3 "$RESOLVER" --render "$template" --output "$output" --repo-root "$REPO_ROOT"; then
    echo >&2
    echo "refusing to continue: could not resolve the Claude Code executable (and any" >&2
    echo "Node runtime it needs) to absolute, verified paths." >&2
    echo >&2
    echo "launchd will not read your .zshrc or load nvm, so the schedule cannot rely on" >&2
    echo "an interactive shell's PATH. Fix one of these and re-run:" >&2
    echo "  * install Claude Code so it lives at a stable absolute path, or" >&2
    echo "  * export CLAUDE_BIN=/absolute/path/to/claude   (and NODE_BIN if needed)" >&2
    echo >&2
    echo "Diagnose with: python3 scripts/resolve_launchd_runtime.py --json" >&2
    exit 2
  fi
  plutil -lint "$output" >/dev/null || { echo "rendered plist is invalid" >&2; exit 2; }
  if grep -q '__[A-Z0-9_]*__' "$output"; then
    echo "rendered plist still contains placeholders:" >&2
    grep -o '__[A-Z0-9_]*__' "$output" | sort -u >&2
    exit 2
  fi
}

render() {
  render_to "$TEMPLATE" "$TARGET"
}

# Execute the rendered plist's OWN program, with `env -i` and only the plist's
# OWN environment. That is as close to launchd's starting conditions as can be
# reached without loading the job: no PATH from this shell, no nvm, no rc files.
# --check-runtime makes the runner prove it can start Claude Code and pass the
# report-only preflight, then stop. It writes no report and calls no model.
check_tcc_path() {
  local protected
  protected="$(python3 - "$REPO_ROOT" <<'PYEOF'
import os, sys
sys.path.insert(0, sys.argv[1])
from src.launchd import tcc_protected_ancestor
print(tcc_protected_ancestor(sys.argv[1], os.path.expanduser("~")) or "")
PYEOF
)"
  if [ -z "$protected" ]; then
    return 0
  fi
  echo "refusing to enable: this repository is inside a macOS privacy-protected folder." >&2
  echo >&2
  echo "  repository : $REPO_ROOT" >&2
  echo "  protected  : $protected" >&2
  echo >&2
  echo "launchd does not inherit the Desktop/Documents/Downloads access your" >&2
  echo "Terminal has. A scheduled job here fails before its first line with" >&2
  echo "'Operation not permitted' and exit code 126, even though running the" >&2
  echo "script by hand works perfectly. That gap is why this is checked." >&2
  echo >&2
  echo "Fix by moving the repository out of the protected folder:" >&2
  echo "  mv \"$REPO_ROOT\" ~/projects/$(basename "$REPO_ROOT")" >&2
  echo "  cd ~/projects/$(basename "$REPO_ROOT") && ./scheduler/install.sh enable" >&2
  echo >&2
  echo "Granting Full Disk Access to /bin/bash would also work and is NOT" >&2
  echo "recommended: it is a blanket grant to a shell interpreter, applying to" >&2
  echo "everything that shell ever runs, for the sake of one job." >&2
  return 1
}

verify_plist() {
  local plist="$1"
  local -a env_args=() prog_args=()

  while IFS= read -r line; do
    [ -n "$line" ] && env_args+=("$line")
  done < <(python3 "$RESOLVER" --env-args "$plist")

  while IFS= read -r line; do
    [ -n "$line" ] && prog_args+=("$line")
  done < <(python3 "$RESOLVER" --program-args "$plist")

  if [ "${#prog_args[@]}" -eq 0 ] || [ "${#env_args[@]}" -eq 0 ]; then
    echo "could not read ProgramArguments/EnvironmentVariables from $plist" >&2
    return 2
  fi

  echo "  program: ${prog_args[*]}"
  echo "  env:"
  printf '    %s\n' "${env_args[@]}"
  echo
  echo "  running under 'env -i' with only the plist's environment"
  echo "  (this approximates launchd: no .zshrc, no nvm, no inherited PATH)"
  echo

  # The plist supplies PATH; anything the job needs beyond it is a real bug.
  env -i "${env_args[@]}" "${prog_args[@]}" --check-runtime
}

case "$ACTION" in
  enable)
    # First, before anything else: is this location schedulable at all? launchd
    # does not inherit the Terminal's grant for Desktop/Documents/Downloads, so
    # a job here dies with "Operation not permitted" and exit 126 before running
    # a single line -- while everything still works by hand. Nothing further is
    # worth checking if the answer is no.
    if ! check_tcc_path; then
      exit 1
    fi
    [ -f "$TEMPLATE" ] || { echo "no template at $TEMPLATE" >&2; exit 2; }
    chmod +x "$REPO_ROOT/scripts/scheduled_evaluation.sh"
    # Refuse to schedule anything if the report-only invariants do not hold.
    if ! python3 "$REPO_ROOT/scripts/check_scheduled_safety.py" --preflight >/dev/null; then
      echo "refusing to enable: report-only safety preflight failed." >&2
      echo "run: python3 scripts/check_scheduled_safety.py --preflight" >&2
      exit 1
    fi
    # Render into a scratch file and prove it works under launchd-like
    # conditions BEFORE anything reaches ~/Library/LaunchAgents.
    STAGED="$(mktemp -t rh-agent-plist)"
    trap 'rm -f "$STAGED"' EXIT
    render_to "$TEMPLATE" "$STAGED"
    echo "verifying the rendered plist in a launchd-like environment..."
    if ! verify_plist "$STAGED"; then
      echo >&2
      echo "refusing to enable: the rendered job could not start in an environment" >&2
      echo "approximating launchd's. Nothing was installed. See the output above and" >&2
      echo "logs/scheduled/ for the runner's own log." >&2
      exit 2
    fi
    echo "verification passed."
    echo

    render
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null
    launchctl bootstrap "gui/$UID_NUM" "$TARGET" || {
      echo "bootstrap failed; you may need to grant Full Disk Access to launchd" >&2
      exit 2
    }
    launchctl enable "gui/$UID_NUM/$LABEL"
    echo "enabled $LABEL"
    case "$SLOT" in
      pm)        echo "  schedule: weekdays 14:00 America/Chicago" ;;
      discovery) echo "  schedule: Saturdays 09:00 America/Chicago"
                 echo "  scope:    broad discovery only — proposes NOTHING" ;;
      *)         echo "  schedule: weekdays 10:30 America/Chicago" ;;
    esac
    echo "  plist:    $TARGET"
    echo "  reports:  $REPO_ROOT/reports/"
    echo "  REPORT ONLY — execution remains disabled."
    ;;

  disable)
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null
    rm -f "$TARGET"
    echo "disabled $LABEL (unloaded and plist removed)"
    ;;

  status)
    for label in "$LABEL_AM" "$LABEL_PM" "$LABEL_DISCOVERY"; do
      if launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1; then
        echo "LOADED   $label"
        launchctl print "gui/$UID_NUM/$label" 2>/dev/null \
          | grep -E 'state|last exit code|runs' | sed 's/^/           /'
      else
        echo "not loaded  $label"
      fi
    done
    echo
    echo "config switches:"
    python3 - "$REPO_ROOT" <<'PY'
import json, sys
c = json.load(open(sys.argv[1] + "/config.json"))
for k in ("execution_mode", "agent_enabled", "live_trading"):
    print("           %-16s %s" % (k, c.get(k)))
PY
    echo
    echo "installed plist runtime (what launchd would actually use):"
    for label in "$LABEL_AM" "$LABEL_PM" "$LABEL_DISCOVERY"; do
      plist="$AGENTS_DIR/${label}.plist"
      if [ -f "$plist" ]; then
        echo "           $label"
        python3 "$RESOLVER" --env-args "$plist" 2>/dev/null | sed 's/^/             /'
      fi
    done
    ;;

  verify)
    # Everything `enable` would do, except loading anything into launchd.
    [ -f "$TEMPLATE" ] || { echo "no template at $TEMPLATE" >&2; exit 2; }
    chmod +x "$RUNNER"

    echo "1. report-only safety preflight"
    if ! python3 "$REPO_ROOT/scripts/check_scheduled_safety.py" --preflight >/dev/null; then
      echo "   FAILED — run: python3 scripts/check_scheduled_safety.py --preflight" >&2
      exit 1
    fi
    echo "   ok"
    echo
    echo "2. resolving executables (absolute paths, verified by running them)"
    python3 "$RESOLVER" --json >/dev/null || { echo "   FAILED" >&2; exit 2; }
    python3 "$RESOLVER" --json | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("   claude_bin    %s" % r["claude_bin"])
print("   node_bin      %s" % (r["node_bin"] or "(none)"))
print("   node_required %s" % ("yes" if r["node_required"] else "no"))
print("   PATH          %s" % r["path"])
'
    echo
    echo "3. rendering $LABEL to a scratch file (installing nothing)"
    STAGED="$(mktemp -t rh-agent-plist)"
    trap 'rm -f "$STAGED"' EXIT
    render_to "$TEMPLATE" "$STAGED"
    echo "   rendered and lint-clean, no placeholders left: $STAGED"
    echo
    echo "4. executing the rendered job in a launchd-like environment"
    if ! verify_plist "$STAGED"; then
      echo >&2
      echo "VERIFY FAILED. Do not enable the schedule yet." >&2
      exit 2
    fi
    echo
    echo "VERIFY PASSED for $LABEL."
    echo "  Nothing was installed, nothing was loaded, no report was written."
    echo "  The schedule is still NOT enabled: ./scheduler/install.sh enable${SLOT_FLAG}"
    ;;

  run-now)
    if [ "$SLOT" = "discovery" ]; then
      exec "$RUNNER"
    fi
    exec "$RUNNER" --slot "$SLOT"
    ;;

  *)
    echo "usage: $0 {enable|disable|status|verify|run-now} [--pm|--discovery]" >&2
    exit 2
    ;;
esac
