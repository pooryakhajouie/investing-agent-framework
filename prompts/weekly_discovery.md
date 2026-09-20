# Weekly Broad Discovery — REPORT ONLY, PROPOSES NOTHING

You are running one **weekly, unattended broad-market discovery pass**. Its only
job is to widen the candidate universe so new companies, ETFs, themes and
eligible crypto can enter consideration **without the owner having to add them
by hand**.

## The two rules that govern everything else

> **1. This run is REPORT ONLY. It changes nothing but its own report and
> research notes.**
>
> **2. This run PROPOSES NOTHING.** No purchase, no allocation, no decision, no
> approval, no order. Not even a recommendation.

The monthly $25 authorization is decided **only** in the weekday evaluation
(`prompts/scheduled_evaluation.md`). A discovery pass that drifts into
recommending a buy has exceeded its authority, and the report validator rejects
it: a discovery report containing a `DECISION:` line, a proposed allocation, a
`decision_id`, or approval language is a failure regardless of how good the
research is.

Specifically, and without exception:

- **Never enable execution.** Do not edit `config.json`.
- **Never approve anything.** Do not run `scripts/approve_decision.py`.
- **Never call an order tool**, including `review_*` and `preview_*`.
- **Never submit anything.**
- **Never modify** a watchlist, scan, or alert. You are *reading* curated lists,
  not following them — `follow_watchlist` is a write tool and is forbidden.
- **Never edit** any policy, `config.json`, `src/`, `state/`, or
  `logs/decisions.jsonl`.
- **Never write** `state/portfolio_history.json`. It is ingested by a human-run
  script, and a PreToolUse hook will deny the attempt.

You may write to exactly three places: the discovery report, research notes
under `research/`, and nothing else. This is enforced by a hook, not requested.

---

## Step 1 — Load the rules

Read `CLAUDE.md` §7 (tool rules), `DISCOVERY_POLICY.md` (all of it — this run is
the discovery process), and `config.json` to confirm the three execution
switches are closed. If any is open, stop and say so in the report.

## Step 2 — Know what is already in the universe

Read, so that "new" means genuinely new:

1. `research/INDEX.md` — every candidate already carrying a note.
2. The current holdings across **all** accounts (`get_accounts`, then
   `get_equity_positions` / `get_crypto_positions`). A name already held is not
   a discovery.
3. The owner's four watchlists (`get_watchlists`, `get_watchlist_items`).
4. `data/crypto_universe.json` — the eligible crypto allow-list.

Anything in those four sets is **already in the universe**. Your job is what is
not.

## Step 3 — Sweep the curated lists (read-only)

`get_popular_watchlists` returns Robinhood's curated lists; `get_watchlist_items`
reads any of them **by `list_id`, without following it**. That is the whole
mechanism, and it needs no write of any kind.

Sweep by purpose, not exhaustively:

| Purpose | Lists |
|---|---|
| Genuinely new listings | `IPO Access`, `Newly listed crypto` |
| The ETF gap | `Sector ETFs`, `Growth & value ETFs`, `ETFs`, `Bond ETFs` |
| Theme and sector discovery | `Technology`, `Healthcare`, `Finance`, `Software`, `Energy`, `Pharma`, `Consumer goods`, `Real estate`, `Tech, media, & telecom` |
| Event calendar | `Upcoming earnings` |
| Eligible crypto | `Tradable crypto` (cross-check against `data/crypto_universe.json`) |

> **Attention lists are a momentum trap.** `Trending stocks`, `Daily movers` and
> `100 most popular` are ordered by attention, not by expected return. You may
> read them, but anything sourced from them carries provenance
> `ROBINHOOD_POPULAR` and a **higher** bar, per `DISCOVERY_POLICY.md` §2:
> appearing in many sources is a reason to research further and never a reason
> to buy. Do not let this pass become a momentum feed — the household's
> existing book is already concentrated in one AI-semiconductor bet.

Rotate coverage across weeks rather than reading everything every time. Say in
the report which lists you read **this** week and which you deferred.

## Step 4 — Screen cheaply, then narrow

The lists reach roughly nine thousand instruments. Do not research nine thousand.

1. **Mechanical screen first** — `get_equity_fundamentals` takes up to 10
   symbols per call. Screen on what is cheap and decisive: market cap, whether
   it is a penny stock, whether it is leveraged or inverse (both forbidden —
   `CLAUDE.md` §4), sector overlap with the existing book, and valuation on any
   appropriate metric.
2. **Reject fast and say why.** A name dropped at the screen with a one-line
   reason is a good outcome, not a gap.
3. **Shortlist ~5–10.** Then use read-only web research (`WebSearch`,
   `WebFetch`) and, where relevant, SEC filings to understand the business.

Aim to end with **2–4 candidates worth a research note**, and be willing to end
with zero. A week that says "nothing new clears the bar, here is what I looked
at" is a successful pass.

## Step 5 — Apply the standing prohibitions before writing anything down

Drop, without further work, anything that is: an option, future, or event
contract; a leveraged or inverse ETF; an OTC or penny stock; a crypto pair not
in `data/crypto_universe.json`, or one that is halted or display-only. These are
`CLAUDE.md` §4 prohibitions — a candidate that cannot be bought is not a
candidate, and researching it is wasted effort.

For crypto, use the **crypto framework** (`DISCOVERY_POLICY.md` §6) and its own
primary sources (§4c) — never equity valuation metrics.

## Step 6 — Seed research notes

For each surviving candidate, write `research/<SYMBOL>.md` in the shape given in
`prompts/scheduled_evaluation.md` Step 11, with:

- **`classification: RESEARCH_INCOMPLETE`** — a discovery pass has by definition
  not assembled a full package. Do not classify a candidate `PROMISING` or
  `HIGH_CONVICTION_CANDIDATE` off a screen.
- `provenance` recorded in the Snapshot section: which list it came from, dated.
- `last_reviewed` set to today.
- An honest **What would change the classification** section — the specific
  research that would move it forward.

> A classification in a note is a reasoning label, **never** authorization.

**Never delete or cut down an existing note.** If a candidate you surface
already has a note, refresh it rather than replacing it — the postflight
fingerprints every note and a run that destroys research fails.

## Step 7 — Write the discovery report

Write it to the path given in `$DISCOVERY_REPORT_PATH`, with exactly these
sections:

```markdown
# Weekly Discovery — <YYYY-MM-DD>

## Scope
## New candidates
## Screened out
## Research queued
## No purchase proposed
```

- **Scope** — which lists you read, **with the date**, how many instruments they
  covered, and which lists you deferred to a later week. An undated sweep cannot
  be audited or reused, and the validator requires a date here.
- **New candidates** — what entered the universe. Name the symbols, their
  source list, and the one-line reason each is worth a note. If none qualified,
  say so explicitly — an empty section is a validation failure but an explicit
  "none this week" is not.
- **Screened out** — what you looked at and dropped, grouped by reason. Keep it
  compact; this is the record that the sweep was real.
- **Research queued** — which `research/` notes you wrote or refreshed.
- **No purchase proposed** — state plainly that this pass proposes no purchase,
  that nothing here is a recommendation, and that any candidate must still win
  the five-way capital-use comparison in a weekday evaluation before a single
  dollar moves.

Target **600–1,400 words**. Do not print full list dumps — name what matters and
leave the bulk out.

Close by noting that nothing was approved, enabled, or submitted, and that
execution remains disabled.
