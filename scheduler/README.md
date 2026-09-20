# Scheduled report-only jobs (macOS / launchd)

**Status: built and tested, NOT installed.** Nothing is scheduled right now.
`./scheduler/install.sh status` confirms it.

## Three jobs, deliberately different authority

| Job | When | May recommend | May propose |
|---|---|---|---|
| `com.robinhood-agent.evaluation` | weekdays 10:30 | `WAIT` / `SINGLE_BUY` / `SPLIT_BUY_PLAN` | no — a recommendation is not permission |
| `com.robinhood-agent.evaluation-pm` | weekdays 14:00 | same | no |
| `com.robinhood-agent.discovery` | **Saturdays 09:00** | **nothing at all** | no |

The discovery job widens the candidate universe so new companies, ETFs, themes
and eligible crypto can enter consideration without being added by hand. It has
**less** authority than the weekday evaluation: it may add research candidates
and it may not recommend a purchase, declare a decision, or use approval
language. `src/reporting.validate_discovery_report()` rejects a report that
does, and `scripts/record_discovery_run.py` fails the run.

Its substrate is `get_popular_watchlists` + `get_watchlist_items`, which reads
any curated list **by `list_id` without following it** — 28 lists, ~9,000
instruments, zero writes. No saved scan is needed; `create_scan` and
`follow_watchlist` stay forbidden.

```bash
./scheduler/install.sh verify --discovery     # test it, install nothing
./scheduler/install.sh enable --discovery     # only when you want it live
```

A scheduled run is deliberately *weaker* than an interactive one. It may look at
everything and recommend anything; it may change nothing.

| It may | It may never |
|---|---|
| read holdings, orders, watchlists, quotes, filings, news | enable execution |
| research with read-only external sources (`WebSearch`, `WebFetch`) | contradict Robinhood on holdings, basis or eligibility |
| reuse and extend prior research | approve anything |
| recommend `WAIT` / `SINGLE_BUY` / `SPLIT_BUY_PLAN` | expose an order tool |
| write a digest, an audit record and research notes | submit anything |
| add to or refresh a research note | delete or gut a note or audit record |
| write reports/, research/, logs/scheduled/ | write anywhere else in the repository |
| append usage metadata | touch the three execution switches |

Enabling the schedule **does not enable trading.** The three switches stay
closed and there is still no order-submission path in this repository.

---

## Verify first (installs nothing)

```bash
./scheduler/install.sh verify          # the 10:30 AM job
./scheduler/install.sh verify --pm     # the 2:00 PM job
```

`verify` does everything `enable` would do except load anything into launchd:

1. runs the report-only safety preflight;
2. resolves the absolute `claude` executable, and a Node runtime if this install
   needs one, **verifying each by actually running it**;
3. renders the plist to a scratch file, lints it, and refuses if any placeholder
   survived;
4. executes **the rendered plist's own program**, under `env -i` with **only the
   plist's own environment**, in `--check-runtime` mode.

Step 4 is the one that matters. `env -i` discards this shell entirely — no
`.zshrc`, no nvm, no inherited `PATH` — which is the condition under which the
scheduled job previously failed with `claude: not found`. The runner starts
Claude Code, confirms its version, passes the preflight, writes no report and
calls no model.

Nothing is installed, nothing is loaded, and the schedule stays disabled.

---

## Why the plist carries absolute paths

`launchd` reads no `.zshrc`, sources no `nvm.sh`, and inherits nothing from an
open Terminal. It starts a job with roughly `/usr/bin:/bin:/usr/sbin:/sbin` and
that is all.

That is why `run-now` succeeding from an interactive Terminal proved nothing:
that shell had already put Claude Code and nvm's Node on `PATH`. The scheduled
job had neither, and died on `claude: not found`.

So installation resolves and **persists** the runtime into the plist:

| Plist key | What it holds |
|---|---|
| `CLAUDE_BIN` | the absolute path to `claude`, verified by running it |
| `NODE_BIN` | the absolute path to `node`, **only when this install needs one** |
| `PATH` | absolute directories only, resolved bins first |

- `src/launchd.py` holds the resolution logic. It imports no `subprocess` and no
  network module — the same AST-enforced rule as the rest of `src/` — so it
  decides *what* to run and takes an injected probe to do the running.
- `scripts/resolve_launchd_runtime.py` is the probe, and the renderer. It runs
  `claude --version` and nothing else.

A native Claude Code binary needs no Node, and then `NODE_BIN` is left empty
rather than pinning a Node version onto the job's `PATH` for no reason. A
Node-based install (including one installed under nvm) gets that Node's `bin`
directory pinned into `PATH`, read from the nvm directory layout rather than
from any shell function.

**If the resolution fails, installation stops.** A job whose interpreter cannot
be found without shell initialisation is not scheduled at all:

```bash
python3 scripts/resolve_launchd_runtime.py --json   # diagnose
CLAUDE_BIN=/absolute/path/to/claude ./scheduler/install.sh verify   # override the search
```

An override is a hint, not a bypass: the probe still has to succeed before the
value is written into a plist.

---

## Enable

```bash
# weekday morning scan, 10:30 AM America/Chicago
./scheduler/install.sh enable
```

```bash
# optional second weekday scan, 2:00 PM America/Chicago
./scheduler/install.sh enable --pm
```

`enable` re-runs the whole of `verify` — including the `env -i` execution —
against a scratch plist before anything reaches `~/Library/LaunchAgents`. It
refuses to install if the report-only preflight fails, so a schedule can never
be created while a switch is open or an order tool is un-denied; and it refuses
if the executables cannot be resolved, so a schedule can never be created that
would fail unattended.

## Disable

```bash
./scheduler/install.sh disable          # remove the 10:30 AM job
./scheduler/install.sh disable --pm     # remove the 2:00 PM job
```

## Inspect

```bash
./scheduler/install.sh status           # what is loaded, the switches, the resolved runtime
./scheduler/install.sh verify           # render and test the job, install nothing
./scheduler/install.sh run-now          # run once immediately, by hand
./scripts/scheduled_evaluation.sh --dry-run       # safety checks only, no Claude call
./scripts/scheduled_evaluation.sh --check-runtime # can this environment start Claude Code?
```

`--dry-run` and `--check-runtime` differ on purpose: `--dry-run` exercises the
safety surface and stays usable on a machine with no Claude Code installed at
all, while `--check-runtime` is specifically about whether the executables
resolve here.

### The underlying launchctl commands

`install.sh` wraps these; they are here so nothing is hidden.

```bash
# enable  (after rendering the plist into ~/Library/LaunchAgents/)
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.robinhood-agent.evaluation.plist
launchctl enable    "gui/$(id -u)/com.robinhood-agent.evaluation"

# disable
launchctl bootout   "gui/$(id -u)/com.robinhood-agent.evaluation"
rm ~/Library/LaunchAgents/com.robinhood-agent.evaluation.plist

# inspect
launchctl print "gui/$(id -u)/com.robinhood-agent.evaluation"

# kick a run immediately, without waiting for the schedule
launchctl kickstart -p "gui/$(id -u)/com.robinhood-agent.evaluation"
```

Use `com.robinhood-agent.evaluation-pm` for the afternoon job.

---

## How a run works

0. **Write scope armed.** The runner exports `RH_AGENT_SCHEDULED_RUN=1`, which
   turns on the PreToolUse write guard for the duration of the run.
0. **Runtime check.** `CLAUDE_BIN` from the plist is confirmed to be an
   absolute, executable path and is asked for its version. A run that cannot
   start Claude Code fails here, with the fix named, instead of part-way through.
1. **Preflight** (`scripts/check_scheduled_safety.py --preflight`). Verifies the
   three switches are closed, every order and account-mutating tool is denied in
   `.claude/settings.json`, `approve_decision.py` is denied, and no submission is
   unresolved. It also digests the safety surface. **A failure aborts the run
   before Claude is invoked.**
2. **`check_status.py`** must succeed. Corrupt state fails closed.
3. **Claude Code runs noninteractively** against `prompts/scheduled_evaluation.md`
   with `--allowedTools` limited to read-only tools and `--disallowedTools`
   naming every order and mutating tool explicitly.
4. **Postflight** re-digests the safety surface. If any protected file moved, or
   an approval or submission handoff appeared, the run is reported as a **safety
   failure** regardless of what the report says.
5. **`record_scheduled_run.py`** validates the audit record's shape, validates
   the digest against its own contract, regenerates `research/INDEX.md`, and
   appends usage metadata.

### Files a run may not change

Digested before and after every run:

```
config.json                .claude/settings.json      CLAUDE.md
INVESTMENT_POLICY.md       DISCOVERY_POLICY.md        src/guardrails.py
src/models.py              src/execution.py           src/approval.py
src/submission.py          src/allocation.py          src/market_calendar.py
src/scheduling.py          src/launchd.py             scheduler/install.sh
prompts/scheduled_evaluation.md                       scripts/scheduled_evaluation.sh
scheduler/com.robinhood-agent.evaluation.plist
scheduler/com.robinhood-agent.evaluation-pm.plist
```

The list is `IMMUTABLE_DURING_SCHEDULED_RUN` in `src/scheduling.py`, and it
covers the run's own rules as well as the investment rules: a run may not
rewrite the instructions it was given, the runner that restricts its tools, the
module that checks these invariants, or the plists and installer that decide
when and with what environment it runs at all.

### Paths a run may write

An unattended run — weekday or discovery — may create or modify only these:

```
reports/                       the digest and the audit records
reports/discovery/             weekly discovery reports
research/                      per-candidate notes, and research/lessons/
logs/scheduled/                weekday runner logs
logs/scheduled_usage.jsonl     weekday usage metadata
logs/discovery/                discovery runner logs
logs/discovery_usage.jsonl     discovery usage metadata
```

Notably **not** writable, each for its own reason:

```
state/portfolio_history.json   personal financial history — ingested only by
                               scripts/ingest_history.py, which a human runs
state/budget.json              the authorization ledger
logs/decisions.jsonl           the decision log, appended only by the logger
```

Enforced twice over. A **`PreToolUse` hook**
(`scripts/scheduled_write_guard.py`, registered in `.claude/settings.json`)
denies a `Write`/`Edit` outside that set before it happens; it is armed only
while the runner exports `RH_AGENT_SCHEDULED_RUN=1`, so interactive work in this
repository is unaffected. And **postflight** fingerprints the whole repository
tree, reporting any out-of-scope create, modify or delete as a safety failure
even if the hook were bypassed.

`--allowedTools "Write(reports/**)"` would be the obvious way to do this and
does not work: tested on Claude Code 2.1.263, a path-scoped file-tool rule
matches nothing and denied writes to the directory it named, while bare `Write`
allowed them everywhere. Hence the hook.

### Research a run may not destroy

Separately fingerprinted, by `archive_inventory()`:

```
reports/*.md      every detailed audit record (latest.md excluded — it is the digest)
research/*.md      every standing research note (INDEX.md excluded — it is regenerated)
```

A run **may** add a report, add a note, or refresh a note. Deleting one, or
cutting one down to less than half its words, is reported as a **safety
failure** — the same treatment as a mutated guardrail. This is what makes the
short digest safe: it is a summary of material that provably still exists.

### Output — three tiers, three lifetimes

```
reports/YYYY-MM-DD_HHMM.md     detailed audit record, one per run, kept forever
reports/latest.md              the ~1,000-word daily digest, replaced every run
reports/recommendations/*.json machine-readable BUY payload — written only for a BUY
research/<SYMBOL>.md           standing per-candidate research, carried across runs
research/INDEX.md              regenerated after each run; never hand-edited
logs/scheduled/                per-run runner logs
logs/scheduled_usage.jsonl     approximate cost/token metadata, when the CLI reports it
```

`latest.md` used to be a byte copy of the audit record, which is how the daily
report reached 4,800 words and stopped being read. It is now a separate document
with an enforced contract (`src/reporting.py`): 800–1,200 words, hard-capped at
1,500, and required to carry the timestamp and switches, the remaining
authorization, **all five** capital-use rows, the decision, the allocation, the
confidence, 3–5 things being watched, specific change conditions, and pointers
to the audit record and `research/`.

Every one of those is checked independently of the word budget. **Brevity can
never be the reason a required element is missing** — over the ceiling, the fix
is to move detail into the audit record, not to drop an element. A run that
writes no digest gets a mechanically derived one, so `latest.md` is never
silently yesterday's file.

The research notes are what make a short digest safe. They hold the standing
thesis per candidate, so a run reuses yesterday's thinking instead of
re-deriving it, and shortening the digest cannot lose it. A note's
`classification` is a reasoning label, **never** authorization; a note's prices
are never reused, since quotes are re-read from the broker every run.

Usage is **best-effort and never invented**. When the CLI does not report cost or
tokens, the record says `"available": false` rather than guessing.

### The ACTION banner — read one line, not one report

Every digest opens, above every heading, with the answer:

```
> **ACTION: BUY $10.00 SNDK**
>
> - **Remaining monthly authorization:** $25.00 → $15.00 if this is approved and filled
> - **Confidence:** MEDIUM
> - **Why:** <one sentence>
> - **Human approval required:** YES — nothing here is approved or submitted.
```

or, on most days:

```
> **ACTION: NONE — WAIT**
```

The four fields under it are required, and the banner must agree with the
`DECISION:` line further down — a digest that announces a purchase over a WAIT
decision, or the reverse, fails the run. It may not claim approval,
pre-approval, submission or execution; its honest negations are fine.

### Promoting a recommendation into a pending decision

A scheduled BUY also writes `reports/recommendations/<stamp>.json` — one
complete, ordinary BUY payload per leg, **with no `decision_id` and no approval
field**. It is data the decision pipeline can accept and nothing the pipeline
has accepted. A `WAIT` writes no such file, and a run that declares a BUY
without one is reported as a failure.

Turning it into a real pending decision is a **separate, human-invoked step**:

```bash
# gather a read-only broker snapshot first (accounts, orders, cash, quote,
# tradability), then:
python3 scripts/promote_latest_recommendation.py --snapshot broker.json

# or check everything and write nothing:
python3 scripts/promote_latest_recommendation.py --dry-run --snapshot broker.json
```

Before minting anything it re-establishes every fact the recommendation rested
on — that it is the newest valid report and not stale, that the banner and the
payload agree, the monthly authorization reconciled against the broker, broker
orders and pending activity, settled cash (cash-only), a refreshed quote inside
the existing 2% equity / 5% crypto tolerance, tradability, and every normal
guardrail. Any failure prints the reason and mints nothing:

```
  quote: 1779.79 -> 1848.00  (+3.83%, BEYOND the 2.0% tolerance, quote age limit 300s)
  preflight: BLOCKED
    x [PRICE_MOVED_BEYOND_TOLERANCE] price moved +3.83% since the decision was priced
  -> NOT PROMOTED
```

On success it prints the `decision_id`, the fingerprint, and the exact challenge
phrase the approval step will demand. **It approves nothing.** The existing
approval workflow is unchanged, and every leg is still approved separately:

```bash
python3 scripts/approve_decision.py dec_...
```

The scheduled run itself can never do any of this. Promotion refuses outright
when `RH_AGENT_SCHEDULED_RUN=1`, it is listed in
`src.scheduling.FORBIDDEN_SCRIPTS`, and a scheduled run holds at most two exact
`Bash` grants — neither of them this script.

---

## Timing caveat, stated plainly

`StartCalendarInterval` fires on the **Mac's local time**. This machine is
already `America/Chicago`, so 10:30 in the plist is 10:30 America/Chicago, and
the job follows CST/CDT automatically. `TZ=America/Chicago` is also set in the
plist so the *report* is stamped in Chicago time regardless.

If the Mac's timezone is ever changed, the firing time follows the new zone.
launchd has no per-job timezone setting; that is a launchd limitation, not an
oversight here.

Other behaviours worth knowing:

- `RunAtLoad` is `false`, so enabling the job does not immediately run one.
- If the Mac is asleep or off at 10:30, launchd runs the job once **shortly
  after wake**, rather than skipping it silently. A late-afternoon report stamped
  10:30 is that behaviour, not a bug. `date` in the report header shows the real
  execution time.
- 10:30 AM Chicago is 11:30 AM Eastern — an hour into the regular session, so
  quotes are live rather than stale pre-market prints.
