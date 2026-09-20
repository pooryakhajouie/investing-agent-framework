# Scheduled Evaluation — REPORT ONLY

You are running one **scheduled, unattended** evaluation for the long-term
investing experiment in this repository. Nobody is watching this run.

## The one rule that governs everything else

> **This run is REPORT ONLY. It changes nothing.**

Specifically, and without exception:

- **Never enable execution.** Do not edit `config.json`. Do not change
  `execution_mode`, `agent_enabled`, or `live_trading`.
- **Never approve anything.** Do not run `scripts/approve_decision.py`. Do not
  write `state/approvals.json`. Approval is a human act performed interactively.
- **Never call an order tool**, including `review_*` and `preview_*`.
- **Never submit anything.** Do not run `scripts/submit_approved.py`,
  `scripts/prepare_submission.py`, `scripts/record_submission.py`, or
  `scripts/execute_approved.py`.
- **Never modify** a watchlist, scan, or alert.
- **Never edit** `INVESTMENT_POLICY.md`, `DISCOVERY_POLICY.md`, `CLAUDE.md`,
  `config.json`, or anything in `src/`.
- **Never write** to `state/budget.json` or `logs/decisions.jsonl`.

An unattended run has strictly less authority than an interactive one, not more.
Your entire output is **markdown under `reports/` and `research/`** — a detailed
audit record, a concise daily digest, per-candidate research notes, and — only
when the decision is a BUY — a machine-readable recommendation payload (Steps
10-13). If you find yourself wanting to change any other file to make a
recommendation work, that is the signal to stop and say so in the report.

**This is enforced, not requested.** A `PreToolUse` hook denies any write
outside `reports/`, `research/` and the scheduled log files, and the postflight
fingerprints the whole repository tree and reports an out-of-scope write as a
safety failure. A denied write is not a bug to work around — it means the write
was outside your authority.

The runner verifies these invariants before and after you run, by digest. A run
that mutates the safety surface is reported as a failure regardless of what the
report says.

---

## Step 1 — Load the rules

Read `CLAUDE.md`, `INVESTMENT_POLICY.md`, `DISCOVERY_POLICY.md`, and
`config.json`. Confirm all three execution switches are closed. If any is open,
**stop** and write a report whose Decision section says exactly
`DECISION: WAIT`, explaining that the switches are not in the expected state.

## Step 2 — Load current state

```bash
python3 scripts/check_status.py
```

Note the calendar month, the authorized budget, the committed amount, and the
**remaining monthly authorization**. If it reports a state error, stop and say
so — corrupt state fails closed by design.

## Step 3 — Reuse prior research

Read, in this order:

1. **`research/INDEX.md`** — one row per candidate: classification, when it was
   last reviewed, and whether its thesis is stale. This is the cheapest way to
   see what work already exists.
2. **`research/<SYMBOL>.md`** — the standing note for every candidate still in
   play. These carry the accumulated thesis, both cases, the risks and the dated
   evidence. **This is where prior research lives**, not in the daily digest.
3. **`reports/latest.md`** — yesterday's digest. It is a ~1,000-word summary by
   design, so treat it as the headline, never as the evidence.
4. **The most recent `reports/YYYY-MM-DD_HHMM.md`** — the previous run's full
   audit record, when you need what it actually saw and why.
5. `state/last_evaluation.json` and this month's `logs/decisions.jsonl`.
6. **`research/lessons/INDEX.md`** — derived findings about past decisions.
   Read the index; read a lesson only when it bears on today's decision.

> **A lesson is advisory and nothing more.** It cannot authorise a purchase,
> satisfy a research requirement, or relax a guardrail — the same standing as a
> watchlist entry. A lesson marked `OUTCOME_OBSERVATION` may **never** be cited
> as a reason at all, and one marked stale has expired. "It worked last time" is
> not a thesis, and the guardrails reject a decision that argues that way
> (`LESSON_MISUSED_AS_AUTHORIZATION`). Lessons scoped `MANUAL_ACTION` describe
> the owner's own behaviour: they are information for the owner and may not
> justify an agent purchase.
>
> `state/portfolio_history.json` is the record those lessons come from. You may
> read it. You may **not** write it — it is ingested by a human-run script, and
> the write guard will deny the attempt.

A note's price figures are **never** reused — quotes are re-read from the broker
every run (Steps 4 and 7). What a note carries forward is the *thesis*: the
valuation argument, the competitive read, the bull and bear cases, the
invalidation conditions.

**Reuse research that is still valid.** This is a recurring job, and re-deriving
an unchanged thesis every weekday is a waste of both money and attention. Only
do expensive deep research when something **materially changed**:

- a candidate moved enough to alter its expected return;
- a prior WAIT named a condition that has now been met;
- earnings, a filing, or material news landed for a holding or a finalist;
- a genuinely new candidate surfaced;
- the previous package was `RESEARCH_INCOMPLETE` and the gap can now be closed.

- a note is marked **stale** in the index (its thesis is over two weeks old).

If nothing material changed, say so plainly and carry the prior conclusion
forward. A short report that says "nothing changed, still WAIT, here is why"
is a **successful** run, not a lazy one.

> **Shortening the report never means shortening the analysis.** Steps 4-9 are
> performed in full every run, whatever the digest ends up saying. The reason the
> digest can be short is that the depth is written down elsewhere — in the audit
> record and the research notes — not that it was skipped.

## Step 4 — Portfolio and authorization (READ-ONLY)

`get_accounts`, then `get_portfolio`, `get_equity_positions`,
`get_crypto_positions` for **every** account, not just the agentic one. Identify
the agentic account and mask the number (`••••0002`). Never write an account
number to any file.

> **"Existing position" means held anywhere in the portfolio, across all
> accounts.** The agentic account is the execution *destination*, not the
> definition of what is owned. A name held in the individual account is existing
> exposure even when the agentic account is empty.

Check this month's actual agentic orders with `get_equity_orders` and
`get_crypto_orders` so the remaining authorization reflects reality, not just
the local ledger.

## Step 5 — Watchlists and candidate state

`get_watchlists` and `get_watchlist_items` (read-only). Note what is new since
the last report, what has moved materially, and what has gone quiet.

## Step 6 — Crypto, on its own terms

**Crypto is not an afterthought.** It competes for the same $25 as equities, in
this same run, and a scheduled report that treats it as a footnote is incomplete.

Research it with the **crypto framework in `DISCOVERY_POLICY.md` §6**, never with
equity metrics — there is no P/E, no margin and no balance sheet for a
cryptocurrency, and reaching for one is a category error.

Examine, and record in the report:

- **existing crypto holdings across all accounts** and their cost basis where
  available (`get_crypto_positions` returns `cost_bases`);
- **concentration** — crypto's share of total portfolio value, and the largest
  single coin's share within that;
- **eligibility** — which pairs Robinhood supports and will trade in this
  account, from `data/crypto_universe.json`. Never bypass or hand-edit this
  allow-list;
- **current, dated crypto market conditions** (`get_crypto_quotes`, news).

New crypto ideas outside the current holdings are legitimate when the research
supports them.

**Robinhood is not your only permitted source, and for most of this framework it
is the wrong one.** It is a broker: it publishes no issuance schedule, no
active-address count, no protocol roadmap and no regulatory analysis. Treating
its tool surface as the limit of what is knowable makes every crypto candidate
permanently `RESEARCH_INCOMPLETE` for a reason that says nothing about the asset.

| Robinhood is authoritative for | Use read-only web research for |
|---|---|
| my holdings, across all accounts | network usage and adoption |
| cost basis | issuance, supply and tokenomics |
| pair eligibility and tradability | protocol development and roadmap |
| halted / display-only status, minimum order size | ecosystem and application activity |
| orders and order history | security and decentralization |
| buying power and account state | regulatory developments |
| execution | institutional adoption |
| | competing networks |

`WebSearch` and `WebFetch` are allowed in this run and are read-only: they place
nothing, modify nothing and touch no account. Use them when the broker tools
cannot complete the framework, and prioritise **primary and official sources** —
the protocol's own specification and improvement proposals, the foundation's or
core team's publications, the reference implementation, on-chain data, executed
governance decisions, a regulator's own filings, reputable named data providers.
The ranked hierarchy and the `source_type` values are in `DISCOVERY_POLICY.md`
§4c.

**Nothing external overrides Robinhood on the left-hand column.** Those facts
describe this account, not the world. Date every external figure and attribute
it to the source that published it; an unobtainable number is still written
"not available", never invented.

> **Do not classify a crypto candidate `RESEARCH_INCOMPLETE` merely because
> Robinhood does not expose those research fields**, when reliable read-only
> external sources can reasonably supply them.

`RESEARCH_INCOMPLETE` stays correct whenever the research genuinely could not be
done, and saying so is always better than manufacturing confidence. Name the
real obstacle — the source that could not be reached, the figure nobody
reputable publishes, web research unavailable in this run. The guardrails reject
a gap blamed on the broker's scope and accept one that names a genuine obstacle.

Two things that are **not** reasons to buy, and must not be presented as such:

- a large unrealized loss, by itself. To average down, the thesis must still be
  intact **and** the forward return attractive from today's price — a lower
  average cost is not a reason on its own;
- recent price appreciation, by itself. Price per token is meaningless; only
  market cap relative to durable relevance means anything.

Any crypto leg you propose must state its **volatility** and its effect on
**portfolio concentration** explicitly. Drawdowns of 70–90% have historically
occurred in many cryptoassets, including the largest ones, and should be treated
as a plausible risk rather than a tail case.

## Step 7 — Current market information

Price the holdings and the live candidates, equities and crypto alike. Pull news
and any earnings dates that fall inside the current month. Date everything; flag
stale information as stale. Never invent a number — write "not available".

## Step 7a — Check every event against the month's last tradable opportunity

The $25 does not roll over, so "later this month" ends before the 31st does.

| Asset class | Final opportunity in the month |
|---|---|
| Equity / ETF | the **close of the month's last trading day** |
| Crypto | the **last instant of the month** — no closing bell |

> **An event occurring after that edge is next month's information.** It must
> never be described as something this month's authorization can act on.

**Worked case, because this was previously reported wrongly.** MU reports after
the close on **2026-09-30**, September's last session. Its normal post-earnings
*equity* reaction is first tradable on **2026-10-01**, with **October's** $25.
September's $25 cannot reach it and does not carry. The same instant would still
be inside September for a crypto position.

For every dated event you weigh, state which side of the edge it falls on. A
recommendation to wait for information must say when that information becomes
*actionable*, not merely when it is published.

**And if you recommend waiting through such an event, say what that costs.**
Waiting past the month's final tradable opportunity may intentionally allow that
month's authorization to expire unused. That is a legitimate choice — WAIT is a
first-class outcome and an expired authorization is not a failure — but the
report must **state it explicitly** rather than describing the wait as though
the budget could still respond. An unnoticed expiry is the failure.

Where it matters at month end, give the number of **trading sessions** left, not
just calendar days: twelve calendar days can be eight sessions.

## Step 8 — Compare all five uses of the capital

Every scheduled evaluation must explicitly compare these, wherever each is
relevant, and say why the ones it rejected fell short:

1. **Adding to an existing equity or ETF position** (existing ETFs count)
2. **Opening a new equity or ETF position**
3. **Adding to an existing crypto position**
4. **Opening a new eligible crypto position**
5. **Preserving some or all of the budget as cash — WAIT**

"Existing" means **held anywhere across all accounts**, not just the agentic
account. A bucket is **not** `NOT_APPLICABLE` merely because the agentic account
is empty of that asset.

Write `NOT_APPLICABLE: <reason>` only when the bucket genuinely does not exist —
for instance if no cryptocurrency were held in any account. **Silence is not the
same as inapplicable**, and neither is an empty agentic account.

WAIT competes as a real alternative, not a fallback.

## Step 9 — Decide

Produce exactly one of:

- `WAIT`
- `SINGLE_BUY`
- `SPLIT_BUY_PLAN` (2–5 legs, combined total within the remaining authorization)

**Splitting is never required, and must never be forced.** Multiple purchases to
diversify, to look active, or to use up the budget are failures of judgement. One
high-conviction purchase, or WAIT, is frequently the better answer. Only propose
a split when each leg clears the bar on its own and the legs are exposed to
genuinely different return drivers. Think about portfolio sprawl explicitly
before recommending several small positions.

Legs may mix asset classes: `$10` equity + `$5` BTC-USD + `$10` a second equity
is valid **if each leg is justified**. Every leg must state its asset, asset
class, existing vs new position, dollar amount, thesis, and **why that money is
better spent on that leg than on the other legs**.

The combined **proposed + approved + pending + executed** amount for the calendar
month must stay within the single $25 authorization.

A recommendation is not permission — and a recommendation from an *unattended*
run is no closer to permission than any other. Every leg would still need its own
separate human approval, its own fingerprint, its own submission ticket and its
own reconciliation before anything could ever execute. Execution is disabled, and
this run may not enable it.

## Step 10 — Write the detailed audit record

**Three files, three jobs.** Do not conflate them:

| File | Audience | Length | Job |
|---|---|---|---|
| `$REPORT_PATH` | an auditor, later | as long as the work needs | everything this run saw and decided |
| `$DIGEST_PATH` (`reports/latest.md`) | a human, today | **800-1,200 words** | the decision and what drives it |
| `research/<SYMBOL>.md` | the **next run** | as long as the thesis needs | standing per-candidate research |

Write the audit record first, to the path given in `$REPORT_PATH`, with exactly
these sections:

```markdown
# Scheduled Evaluation — <YYYY-MM-DD HH:MM America/Chicago>

## Portfolio & budget status
## Changes since last evaluation
## Watchlist developments
## Strongest candidates
## Decision

DECISION: <WAIT|SINGLE_BUY|SPLIT_BUY_PLAN>

## Proposed allocation
## Confidence
## What would change this
```

The `DECISION:` line must appear on its own line, exactly in that form — the
runner parses it.

**Strongest candidates** must cover equities *and* crypto, and show the five-way
comparison from Step 8 — including the buckets you rejected and why.

Under **Proposed allocation**:

- for `WAIT`, write `None — WAIT` and the remaining authorization;
- otherwise give a table of legs — asset, asset class, `EXISTING_POSITION` or
  `NEW_POSITION`, amount, and the one-line allocation argument — then the
  combined total and the remaining authorization after the plan.

For any crypto leg, state its volatility and concentration effect explicitly.

Under **What would change this**, every dated event must carry the month it is
actionable in, not merely its date. If the recommendation is to wait through an
event that falls past this month's final tradable opportunity, say plainly that
doing so may allow this month's authorization to expire unused, and that the
event is actionable with next month's authorization instead.

Close the report by noting that every leg would require its own separate human
approval, and that nothing was approved, enabled, or submitted.

This file is the **audit record**. It carries the full holdings and watchlist
tables, the complete research packages, and the full rejection reasoning. Length
is not a fault here — this is the tier where depth belongs.

## Step 11 — Write or refresh the per-candidate research notes

Standing research lives in `research/<SYMBOL>.md`, one file per candidate, keyed
by symbol rather than by date. Notes are what let a run reuse yesterday's
thinking instead of re-deriving it, and what let the daily digest be short
without losing anything.

Write or refresh a note when this run **materially advanced or changed** the
standing view of a candidate: new primary-source evidence, a completed or newly
closed research component, a changed classification, a thesis that broke.

**Do not** rewrite every note every run. A note you did not revisit keeps its
existing `last_reviewed` date and is left byte-identical. **Never delete a note,
and never cut one down** — the postflight fingerprints them and a run that
destroys prior research is reported as a safety failure.

Each note has this shape:

```markdown
# <SYMBOL> — <name>

- **symbol:** <SYMBOL>
- **asset_class:** <EQUITY|ETF|CRYPTO>
- **position:** <EXISTING_POSITION|NEW_POSITION>
- **classification:** <a label from DISCOVERY_POLICY.md §7>
- **last_reviewed:** <YYYY-MM-DD>

## Snapshot
Current figures **with the date they were taken**. Prices go stale; say so.

## Thesis
The standing long-term case, and the valuation argument that governs it.

## Bull case
## Bear case
## Risks
## What would change the classification
The specific, checkable conditions — a filing, a multiple, a metric, a date.

## Evidence
Dated entries with `source_type`, per `DISCOVERY_POLICY.md` §4c. For crypto,
external primary sources belong here (§6).
```

A classification in a note is a **reasoning label, never authorization**
(`DISCOVERY_POLICY.md` §7). A note saying `ADD_CANDIDATE` permits nothing.

Do not write `research/INDEX.md` — it is regenerated automatically after the run.

## Step 12 — Write the concise daily digest

Write `$DIGEST_PATH` (`reports/latest.md`). This is the file a human actually
opens each morning, so it is **targeted at 800-1,200 words, roughly two pages**,
and it is validated after the run.

It opens with an **ACTION banner** and then exactly these sections:

```markdown
# Scheduled Evaluation — <YYYY-MM-DD HH:MM America/Chicago>

> **ACTION: BUY $10.00 SNDK**
>
> - **Remaining monthly authorization:** $25.00 → $15.00 if this is approved and filled
> - **Confidence:** MEDIUM
> - **Why:** <one sentence>
> - **Human approval required:** YES — nothing here is approved or submitted.
>   Promote it with `python3 scripts/promote_latest_recommendation.py`, then
>   approve each leg separately.

## Status
## What changed
## Five-way capital-use comparison
## Decision

DECISION: <WAIT|SINGLE_BUY|SPLIT_BUY_PLAN>

## Proposed allocation
## Confidence
## Watching
## What would change this
## Detail and evidence
```

**The ACTION banner comes first, above every heading.** The whole point of the
digest is that the owner can learn whether there is something to buy without
reading anything else, so the answer is the first thing on the page in a fixed
shape every day:

| Decision | ACTION line |
|---|---|
| `WAIT` | `ACTION: NONE — WAIT` |
| `SINGLE_BUY` | `ACTION: BUY $<amount> <SYMBOL>` |
| `SPLIT_BUY_PLAN` | `ACTION: BUY $<amt> <SYM> + $<amt> <SYM>` (2–5 legs, joined by ` + `) |

The banner and the `DECISION:` line must agree, and the leg count must match the
plan type — the validator checks both. Immediately under the ACTION line, and
nowhere else, put the four things a reader needs in order to decide whether to
act at all: the **remaining monthly authorization**, the **confidence**, a
**one-sentence** rationale, and **whether human approval is still required**.

For a `WAIT`, the approval line reads `Not applicable — no purchase is proposed,
so there is nothing to approve.` For a BUY it reads **YES**, and it must say so:
a recommendation is never permission, and the banner is the part most likely to
be read in isolation. The validator rejects a banner that says approval is not
required, claims pre-approval, or claims an order was submitted or executed.

What each section must carry:

- **Status** — the timestamp, the three execution switches by name
  (`execution_mode=DRY_RUN`, `agent_enabled=false`, `live_trading=false`), and
  the **remaining monthly authorization as a dollar figure**, with the day of
  the month and the sessions left.
- **What changed** — only what *materially* changed since the prior evaluation.
  If nothing did, say so in a sentence.
- **Five-way capital-use comparison** — a compact table, one row per use:
  existing equity/ETF · new equity/ETF · existing crypto · new crypto · WAIT.
  Each row: the strongest candidate, its classification, and a one-line reason.
  **All five rows appear every time**, including the rejected ones.
- **Decision** — the `DECISION:` line on its own, exactly in that form, plus two
  or three sentences of reasoning. The runner parses this line.
- **Proposed allocation** — for `WAIT`, `None — WAIT` and the remaining
  authorization. Otherwise the legs: asset, class, existing/new, amount, and a
  one-line argument each, then the combined total. For a crypto leg, state its
  volatility and concentration effect.
- **Confidence** — `LOW`, `MEDIUM` or `HIGH`, and the one thing that most
  undermines it.
- **A historical lesson** — include one **only when it materially affects
  today's decision**, in one sentence, naming its id. The digest is not a place
  to recite history; detailed lessons live in `research/lessons/` and the audit
  record. If no lesson changed the answer, mention none.
- **Watching** — the **3 to 5** most important things being watched, one line
  each. Not the whole research backlog; that lives in the audit record.
- **What would change this** — specific, checkable conditions: a price, a
  filing, a metric, a dated event. Give each dated event the month it is
  **actionable** in, per Step 7a. If waiting through an event past this month's
  final tradable opportunity, say plainly that this month's authorization may
  expire unused.
- **Detail and evidence** — links to `$REPORT_PATH`, to `research/`, and to
  `state/last_evaluation.json` / `logs/decisions.jsonl`.

**Keep out of the digest**, because they are what made the daily report
unreadable and they all live elsewhere now:

- full holdings tables and full watchlist tables → audit record;
- complete research packages → the research notes and the audit record;
- long rejection narratives → audit record;
- restatements of policy, the approval machinery, or why execution is disabled —
  one line in **Status** is the whole of it.

The digest is a summary, **never a substitute**. It must not be the only place
any fact appears, and it may never drop a required element to hit the word
count: a digest missing the decision, the authorization, any of the five
capital-use rows, the allocation, the confidence or the change triggers is
rejected outright. If the content will not fit, cut *detail*, not *elements* —
and remember the audit record has already taken the detail.

### The per-section budget — write to this, not to a total

"Aim for 800-1,200 words" failed in practice: the 2026-09-18 run came in at
**1,727**, because a total is only checkable once the whole thing is already
written, and by then every section feels load-bearing. Budget each section
instead. These sum to roughly 1,100 words including the title and banner, which
leaves headroom inside the band:

| Section | Words | What fits in that |
|---|---:|---|
| Title + **ACTION banner** | 110 | the banner's four fields, nothing else |
| `## Status` | 110 | the three switches, the budget table, one reconciliation line |
| `## What changed` | 190 | **at most 5 numbered items**, 2-4 lines each |
| `## Five-way capital-use comparison` | 140 | the 5-row table; one verdict clause per row |
| `## Decision` | 80 | the `DECISION:` line and the single load-bearing argument |
| `## Proposed allocation` | 110 | the allocation table, the combined total, why this size |
| `## Confidence` | 120 | the rating, the main weakness, the outcome-quadrant note |
| `## Watching` | 95 | **3-5 bullets**, one line each |
| `## What would change this` | 150 | the change conditions; dated events as prose, not a table |
| `## Detail and evidence` | 70 | the pointers only — audit record, payload, `research/` |

Two rules that do most of the work:

- **One evidence clause per claim.** The audit record carries the second and
  third supporting figure. A digest sentence citing three sources is an audit
  sentence in the wrong file.
- **A dated-event table belongs in the audit record.** Two sentences of prose
  carry which events are and are not reachable with this month's authorization,
  and the `authorization_expiry_acknowledged` statement stays either way.

### Check it before finishing, and trim if needed

Do not hand in an unmeasured digest. After writing it:

```bash
python3 -c "import sys;sys.path.insert(0,'.');from src.reporting import validate_concise_report as v;c=v(open('reports/latest.md').read());print(c.word_count, c.ok, c.violations, c.warnings)"
```

The **1,500-word ceiling is a hard failure** and is never to be raised. Above
1,200 the validator warns, and that warning is to be acted on, not accepted: go
back to the section budget above, find the section that is over, and move its
detail into the audit record. Repeat until the warning is gone. Cut *detail*,
never a required element.

## Step 13 — If, and only if, the decision is a BUY: write the recommendation payload

A `DECISION: WAIT` needs nothing here. Stop.

For `SINGLE_BUY` or `SPLIT_BUY_PLAN`, also write
**`reports/recommendations/<YYYY-MM-DD_HHMM>.json`** — the same stamp as the
audit record. This is what makes a scheduled recommendation actionable *without*
letting a scheduled run act:

**Generate the skeleton rather than writing it from memory:**

```bash
python3 scripts/emit_buy_scaffold.py                       # equity, SINGLE_BUY
python3 scripts/emit_buy_scaffold.py --kind crypto
python3 scripts/emit_buy_scaffold.py --plan-type SPLIT_BUY_PLAN
```

That command prints the exact contract below with this month's real calendar
numbers already filled in. Start from its output. The keys are not a style
preference: the guardrails read these spellings and no others, and a payload
that invents its own — `positions_held` for `position_count`,
`quote_and_valuation` for `current_quote_and_valuation` — is rejected in full at
promotion, after the evaluation has already been paid for.

<!-- BEGIN CANONICAL_BUY_PAYLOAD -->
<!-- Generated by scripts/emit_buy_scaffold.py from src/decision_schema.py.
     Do not edit by hand: run `python3 scripts/emit_buy_scaffold.py --sync-prompts`. -->

```json
{
  "schema_version": 1,
  "generated_at": "<ISO-8601 UTC>",
  "source_report": "reports/<YYYY-MM-DD_HHMM>.md",
  "plan_type": "SINGLE_BUY",
  "legs": [
    {
      "decision": "BUY",
      "action": "buy",
      "side": "buy",
      "asset_class": "EQUITY",
      "asset_type": "us_common_stock",
      "position_type": "NEW_POSITION",
      "classification": "<a classification from DISCOVERY_POLICY.md that supports a purchase>",
      "ticker": "<TICKER, or a hyphenated crypto pair such as BTC-USD>",
      "symbol": "<same as ticker>",
      "security_name": "<full name>",
      "exchange": "<NASDAQ / NYSE / NYSEARCA; omit for crypto>",
      "proposed_amount_usd": "<dollars, 2dp — a choice, never a default>",
      "monthly_budget_before_usd": "<authorization before this leg, 2dp>",
      "monthly_budget_after_usd": "<authorization after this leg, 2dp>",
      "month": "<YYYY-MM>",
      "investment_horizon_months": 36,
      "current_price_usd": "<refreshed quote, from get_equity_quotes or get_crypto_quotes>",
      "quote_timestamp": "<ISO-8601 UTC of that quote>",
      "fractional_eligible": true,
      "confidence": "<LOW | MEDIUM | HIGH>",
      "thesis": "<replace with this decision's own reasoning; keep the key>",
      "value_creation": "<replace with this decision's own reasoning; keep the key>",
      "valuation_reasoning": "<replace with this decision's own reasoning; keep the key>",
      "timing_reason": "<replace with this decision's own reasoning; keep the key>",
      "why_not_wait": "<replace with this decision's own reasoning; keep the key>",
      "margin_for_error": "<replace with this decision's own reasoning; keep the key>",
      "portfolio_exposure": "<replace with this decision's own reasoning; keep the key>",
      "risks": "<replace with this decision's own reasoning; keep the key>",
      "invalidation": "<replace with this decision's own reasoning; keep the key>",
      "alternatives_considered": [
        {
          "symbol": "<TICKER>",
          "provenance": "<MY_WATCHLIST:<list> | CURRENT_HOLDING | OPEN_DISCOVERY>",
          "classification": "<label from DISCOVERY_POLICY.md>",
          "why_not": "<replace with this decision's own reasoning; keep the key>"
        },
        {
          "symbol": "<TICKER>",
          "provenance": "<provenance>",
          "classification": "<label>",
          "why_not": "<replace with this decision's own reasoning; keep the key>"
        }
      ],
      "monthly_optionality": {
        "days_remaining_in_month": "<from `python3 scripts/emit_buy_scaffold.py` — never computed by hand>",
        "tradable_sessions_remaining": "<from `python3 scripts/emit_buy_scaffold.py` — never computed by hand>",
        "known_events_this_month": "<replace with this decision's own reasoning; keep the key>",
        "candidates_being_watched": "<replace with this decision's own reasoning; keep the key>",
        "probability_ranking_changes": "<replace with this decision's own reasoning; keep the key>",
        "dry_powder_benefit": "<replace with this decision's own reasoning; keep the key>",
        "partial_deployment_considered": "<replace with this decision's own reasoning; keep the key>"
      },
      "portfolio_sprawl_assessment": {
        "position_count": "<replace with this decision's own reasoning; keep the key>",
        "smallest_positions": "<replace with this decision's own reasoning; keep the key>",
        "economic_significance_of_this_add": "<replace with this decision's own reasoning; keep the key>",
        "overlap_analysis": "<replace with this decision's own reasoning; keep the key>",
        "concentration_alternative": "<replace with this decision's own reasoning; keep the key>"
      },
      "theme": {
        "primary": "<broad theme from the THEME_TAXONOMY in src/models.py>",
        "subtheme": "<subtheme that rolls up to it>",
        "scope_caveat": "<what this position does NOT cover>"
      },
      "events_considered": [
        {
          "label": "<replace with this decision's own reasoning; keep the key>",
          "date": "<YYYY-MM-DD>",
          "asset_class": "EQUITY",
          "session_timing": "<before the open | intraday | after the close>",
          "occurs_after_close": false,
          "status": "<RESOLVED | PENDING>",
          "actionable_with_this_month_authorization": true,
          "note": "<replace with this decision's own reasoning; keep the key>"
        }
      ],
      "evidence": [
        {
          "tool": "get_equity_quotes",
          "source_type": "MARKET_DATA",
          "component": "Quote and valuation",
          "detail": "<replace with this decision's own reasoning; keep the key>"
        },
        {
          "tool": "get_financials",
          "source_type": "OFFICIAL_FINANCIALS",
          "component": "Financial statements",
          "detail": "<replace with this decision's own reasoning; keep the key>"
        },
        {
          "tool": "get_sec_filing_facts",
          "source_type": "SEC_FILING",
          "component": "Primary filing",
          "detail": "<replace with this decision's own reasoning; keep the key>"
        }
      ],
      "new_position_justification": {
        "incremental_expected_return": "<replace with this decision's own reasoning; keep the key>",
        "diversification_benefit": "<replace with this decision's own reasoning; keep the key>",
        "overlap_with_existing": "<replace with this decision's own reasoning; keep the key>",
        "diversification_is_economic_not_cosmetic": "<replace with this decision's own reasoning; keep the key>",
        "best_existing_alternative": {
          "symbol": "<the specific existing holding this was weighed against>",
          "why_not": "<replace with this decision's own reasoning; keep the key>"
        },
        "why_new_position_beats_adding_to_existing": "<replace with this decision's own reasoning; keep the key>",
        "initial_size_significance": "<replace with this decision's own reasoning; keep the key>"
      },
      "research_package": {
        "current_quote_and_valuation": "<replace with this decision's own reasoning; keep the key> — required; findings, or 'NOT_AVAILABLE: <reason>'. Silence is not the same as unavailable.",
        "recent_quarterly_results": "<replace with this decision's own reasoning; keep the key> — required; findings, or 'NOT_AVAILABLE: <reason>'. Silence is not the same as unavailable.",
        "multi_quarter_trends": "<replace with this decision's own reasoning; keep the key> — required; findings, or 'NOT_AVAILABLE: <reason>'. Silence is not the same as unavailable.",
        "balance_sheet": "<replace with this decision's own reasoning; keep the key> — required; findings, or 'NOT_AVAILABLE: <reason>'. Silence is not the same as unavailable.",
        "cash_flow": "<replace with this decision's own reasoning; keep the key> — findings, or 'NOT_AVAILABLE: <reason>'.",
        "earnings_history": "<replace with this decision's own reasoning; keep the key> — findings, or 'NOT_AVAILABLE: <reason>'.",
        "material_news": "<replace with this decision's own reasoning; keep the key> — findings, or 'NOT_AVAILABLE: <reason>'.",
        "primary_filings": "<replace with this decision's own reasoning; keep the key> — findings, or 'NOT_AVAILABLE: <reason>'.",
        "competitive_position": "<replace with this decision's own reasoning; keep the key> — findings, or 'NOT_AVAILABLE: <reason>'.",
        "major_risks": "<replace with this decision's own reasoning; keep the key> — required; findings, or 'NOT_AVAILABLE: <reason>'. Silence is not the same as unavailable.",
        "upcoming_events": "<replace with this decision's own reasoning; keep the key> — findings, or 'NOT_AVAILABLE: <reason>'.",
        "bull_case": "<replace with this decision's own reasoning; keep the key> — required; findings, or 'NOT_AVAILABLE: <reason>'. Silence is not the same as unavailable.",
        "bear_case": "<replace with this decision's own reasoning; keep the key> — required; findings, or 'NOT_AVAILABLE: <reason>'. Silence is not the same as unavailable."
      },
      "authorization_expiry_acknowledged": "<required only when a weighed event falls past this month's final tradable opportunity (2026-09-30 16:00); say plainly that the authorization expires unused>"
    }
  ]
}
```

Every key above is required and spelled exactly as the guardrails read
it. Replace the placeholder values; keep the keys. `days_remaining_in_month`
and `tradable_sessions_remaining` are taken from
`python3 scripts/emit_buy_scaffold.py` and never computed by hand — they
are checked against the project's exchange calendar and a hand-computed
value earns OPTIONALITY_MISREPORTED.

For an EXISTING_POSITION, drop `new_position_justification` and
`research_package` and carry `cost_basis_analysis` instead. For crypto,
run the command with `--kind crypto`: the research components differ.
<!-- END CANONICAL_BUY_PAYLOAD -->

> **Omit `decision_id`.** A scheduled run may not mint one: an identifier with
> no ledger entry behind it is worse than none, and `logs/decisions.jsonl` is
> not yours to write. Promotion mints it.

Omit `approved`, `approval`, `approved_by`, `user_approved`, `execution_state`
and `fingerprint` as well. The guardrails reject them as
`SELF_APPROVAL_ATTEMPTED` and the promotion step rejects the payload outright.

The amounts and symbols here must match the ACTION banner exactly. Promotion
cross-checks them and refuses on any disagreement, because a banner that does
not describe the payload means the human read one thing and approved another.

Write only these four destinations. Modify no other file.
