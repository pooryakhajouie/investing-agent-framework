# Long-Term Capital Allocation Evaluation — DRY RUN

You are running one periodic evaluation for the long-term investing experiment
in this repository. You will decide where the next increment of the **$25
monthly authorization** has the best expected long-term use — or that it has
none, and the answer is WAIT.

**This is a DRY RUN. You will not place an order. There is no code path that
could. Do not call any Robinhood order tool, including `review_*` and
`preview_*`, for any reason.**

Horizon: **at least 2 years.** You are an investor, not a trader.

---

## Step 1 — Load the rules

Read, in order: `CLAUDE.md`, `INVESTMENT_POLICY.md`, `DISCOVERY_POLICY.md`,
`config.json`.

Confirm `live_trading` is `false`. If it is `true`, **stop** and tell the user.

## Step 2 — Load current state

```bash
python3 scripts/check_status.py
```

Note the calendar month, the authorized budget, the amount already committed,
and — the number that governs this evaluation — the **remaining monthly
authorization**. Also note whether the crypto universe snapshot is fresh.

If this reports a state error, **stop**. Corrupt state fails closed by design.
Report the problem; do not repair it silently.

## Step 3 — Review this month's prior decisions and prior research

Read `state/last_evaluation.json` and this month's entries in
`logs/decisions.jsonl`. You need to know what you already decided and why,
whether a prior WAIT was conditioned on something that has now happened, and
what has already been committed.

Then read **`research/INDEX.md`** and the `research/<SYMBOL>.md` note for every
candidate you expect to consider. That is where standing research lives — the
thesis, both cases, the risks, the invalidation conditions and the dated
evidence — and re-deriving a thesis that is already written down is waste.

A note's **thesis** carries forward. A note's **prices never do**: re-read every
quote from the broker in Steps 4 and 7. A note is stale after 14 days, which
means revisit the thesis, not discard it. And a note's `classification` is a
reasoning label, **never** authorization (`DISCOVERY_POLICY.md` §7).

The most recent `reports/YYYY-MM-DD_HHMM.md` is the last scheduled run's full
audit record; `reports/latest.md` is only its ~1,000-word digest, so use the
digest for the headline and the audit record for the evidence.

## Step 4 — Full portfolio census (READ-ONLY)

Robinhood MCP has read access to **all** of the owner's accounts. Do not restrict
the analysis to the agentic account.

> **"Existing position" means held anywhere across ALL accounts.** The agentic
> account is the execution *destination*, not the definition of what is owned. A
> name held only in the individual account is still an existing position for
> `position_type`, for the new-position hurdle, and for the five-way comparison
> in Step 9 — even when the agentic account is empty.

1. `get_accounts` — enumerate every account. Keep account numbers in memory
   only; **never write them to a file**, and mask them (`••••1234`) when shown.
2. `get_portfolio` for each account — value, cash, buying power.
3. `get_equity_positions` for each account — symbol, quantity, average cost.
4. `get_crypto_positions` for each account with a crypto account (pass
   `rhs_account_number`) — asset, quantity, cost bases.
5. `get_equity_quotes` / `get_crypto_quotes` — price every holding so you know
   current value, gain/loss, and true concentration.
6. Optionally `get_equity_orders`, `get_crypto_orders`, `get_equity_tax_lots`,
   `get_realized_pnl` where they inform the decision.

Compare against `PORTFOLIO_BASELINE.md`, which holds the last full census, and
note what has changed.

**Remember:** buying power is a feasibility ceiling. The spending limit is the
remaining monthly authorization from Step 2, and it always binds first.

If a call fails, say so. **Do not guess at holdings.**

## Step 5 — Read the lists (READ-ONLY, never modify)

`get_watchlists`, then `get_watchlist_items` for each, plus `get_scans` (and
`run_scan` where a saved scan is useful). `state/watchlist_snapshot.json` holds
the cached view; refresh your understanding against the live lists.

For each list: its name, size, what kinds of assets it holds, its apparent
theme, overlap with holdings, plausible long-term candidates, and items that
look speculative, deteriorating, or extremely expensive.

**Watchlist membership is an INTEREST SIGNAL, not BUY AUTHORIZATION.**

## Step 6 — Refresh the crypto universe if stale

If Step 2 flagged the snapshot as stale (older than 30 days), refresh it:

```
get_currency_pairs(limit=700)        # read-only MCP call; save the raw JSON
python3 scripts/update_crypto_universe.py /path/to/raw_pairs.json
```

A crypto proposal cannot be validated without a loadable snapshot.

## Step 7 — Stage 1: broad discovery (~15–30 candidates)

Build a candidate pool, cheaply and widely, drawing from **all** of:

1. current equity holdings
2. current crypto holdings
3. the owner's watchlists
4. the owner's saved scans
5. Robinhood popular lists
6. theme research (§4 of `DISCOVERY_POLICY.md`)
7. peers, suppliers, and customers of interesting companies
8. open discovery

Record `provenance` for each (`CURRENT_HOLDING`, `MY_WATCHLIST:<name>`,
`MY_SCAN:<name>`, `THEME_DISCOVERY:<theme>`, `SUPPLY_CHAIN_DISCOVERY:<theme>`,
`ROBINHOOD_POPULAR`, `OPEN_DISCOVERY`), a one-line reason, and an initial
classification.

Do **not** do expensive research here. A pool made only of mega-caps means
discovery did not happen. Look through the value chain — picks and shovels, not
only headline names.

## Step 8 — Stage 2: deep research (~3–7 finalists)

Advance the most promising candidates and research them properly:

- `get_equity_fundamentals` — valuation ratios, market cap, 52-week range,
  dividend schedule, profile.
- `get_financials` — revenue, gross profit, net income, margins over time.
- `get_sec_filing_index` + `get_sec_filing_facts` — primary source data.
- `get_earnings_results` / `get_earnings_calendar` — event risk and surprise history.
- `get_equity_news` — material developments, with dates.
- `get_equity_historicals` — multi-year price context (months/years, not minutes).
- `get_equity_technical_indicators` — supporting information only, never a thesis.
- `get_equity_tradability` — fractional eligibility, which matters at $25.
- `get_crypto_quotes`, `get_currency_pairs` — for crypto finalists.
- `get_indexes` + `get_index_quotes` — broad market context.

Follow the checklists in `DISCOVERY_POLICY.md` §5 (equities) and §6 (crypto).

For each finalist produce: a **bull case**, a **bear case**, the major unknowns,
a classification, and the scorecard (§8) — remembering that the written
reasoning matters more than the numbers.

For anything that has risen sharply, answer §9 explicitly: why did it rise, is
the catalyst structural, how much is priced in, and is there a cheaper way to own
the same trend?

## Step 8a — Score the research-completeness gate for every finalist

For each finalist, fill the research package for its asset type (see
`DISCOVERY_POLICY.md` §4b). Every component gets either findings or
`"NOT_AVAILABLE: <reason>"`. **Leaving a component out is a violation; saying it
was unavailable is not.**

If a component that cannot be waived is unavailable, classify that finalist
**`RESEARCH_INCOMPLETE`** and remove it from BUY contention. It may still be the
best *idea* — it just cannot be bought on this evidence.

Prioritise primary sources: SEC filings, then earnings releases, then official
financial data, then market data, then reputable reporting. Tag each evidence
entry with `source_type`. A new position needs at least **two** primary or
official-financial entries. **Analyst price targets are not analysis.**

**Crypto has a different record, held to the same standard.** A blockchain files
nothing with the SEC, so its primary sources are the protocol's own
specification and improvement proposals (`PROTOCOL_DOCUMENTATION`), the
project's own publications (`PROJECT_OFFICIAL`), the chain itself
(`ONCHAIN_DATA`), the reference implementation (`CORE_DEV_REPOSITORY`),
executed governance decisions (`GOVERNANCE_RECORD`), a regulator's own filings
(`REGULATORY_PUBLICATION`) and reputable named data providers
(`RESEARCH_DATA_PROVIDER`). Two of those are required for a new crypto
position; market data and commentary never count. Obtain them with **read-only
web research** (`WebSearch`, `WebFetch`) — Robinhood publishes none of it, and
stays authoritative only for holdings, cost basis, pair eligibility,
tradability, orders and execution.

> **Do not waive a crypto research component, or classify a crypto candidate
> `RESEARCH_INCOMPLETE`, merely because Robinhood does not expose the field.**
> Name the real obstacle instead, or do the research. The validator rejects a
> gap blamed on the broker's scope (`RESEARCH_GAP_NOT_JUSTIFIED`).

## Step 8b — Assess theme precision

For each finalist name the **subtheme**, not the category, and state what it does
NOT cover. Robotic-assisted surgery is `healthcare`, not `robotics_automation`.
Do not claim a specialized position fills a whole broad thematic gap.

## Step 8c — Monthly optionality

Answer, in writing:

- How many days remain in this calendar month?
- What known earnings/events/catalysts land before month end?
- Which candidates are still under research and could overtake the leader?
- How likely is it that new information reorders the ranking?
- What is preserved dry powder worth this month specifically?
- **Would $5, $10, or $15 be better than $25?**

Remember the amount is a *choice*: BUY $5 / $10 / $15 / $20 / $25 / WAIT. If a
purchase would push cumulative spending past **50% of the authorization before
the 15th**, write a substantive `monthly_optionality_analysis` justifying the
loss of flexibility, or reduce the size.

## Step 8d — Place every event on the right side of the month boundary

"Later this month" ends before the 31st does. For an equity or ETF the month's
**final tradable opportunity** is the close of its last trading day; for crypto
it is the last instant of the month, because there is no closing bell.

> **An event after that edge is next month's information.** This month's
> authorization cannot be spent on it.

MU reporting after the close on **2026-09-30** — September's last session — is
the worked case: the post-earnings *equity* reaction is first tradable on
**2026-10-01**, with October's $25.

List the events you weighed in `events_considered`, each with a `label`, a
`date`, an `asset_class`, whether it `occurs_after_close`, and
`actionable_with_this_month_authorization`. The validator recomputes that last
field against the calendar and rejects a wrong answer
(`EVENT_ACTIONABILITY_MISREPORTED`).

If you are waiting through an event past the edge, that may **intentionally
allow this month's authorization to expire unused**. State it in
`authorization_expiry_acknowledged`; without it the decision is rejected
(`UNACKNOWLEDGED_AUTHORIZATION_EXPIRY`). An expired authorization is a fine
outcome. An unnoticed one is not.

Optionally report `monthly_optionality.tradable_sessions_remaining` — checked
against the calendar when supplied, and the honest denominator at month end.

## Step 9 — The capital-allocation competition

Build an explicit comparison of at least these five options:

```
1. Add to an existing equity/ETF holding : ...
2. New equity or ETF position            : ...
3. Add to existing crypto                : ...
4. New crypto position                   : ...
5. WAIT                                  : $0
```

Options 1 and 3 draw on holdings in **any** account, not just the agentic one,
and option 1 covers existing ETFs as well as individual stocks. A bucket is not
inapplicable merely because the agentic account is empty of that asset.

Compare directly on expected long-term return, quality, valuation, growth
runway, portfolio fit, risk/reward, and entry attractiveness.

Ask honestly:

- Would I want to own this for years, not weeks?
- Does it improve the portfolio, or just add activity?
- Does it duplicate exposure I already have?
- **Does the entry price offer enough expected return, with margin for error?**
- **Would putting this capital into an existing high-conviction holding do more?**
- Am I reaching for a reason because budget is available?
- **How much should be deployed — and how much should be kept for later this month?**

Two failure modes to avoid explicitly:

1. **Buying because nothing says wait.** "There is no pending catalyst, so there
   is nothing to wait for" is not a reason to buy. A strong company at a poor
   price is still a poor investment. The case must rest on expected return from
   today's price.
2. **Buying to fill a gap.** Missing exposure to a sector is a signal to
   research, not a reason to own something. Diversification that does not raise
   expected risk-adjusted return is cosmetic.

If nothing clears the threshold, the answer is **WAIT** — and it needs no
pending event to justify it. Valuation alone is enough.

## Step 10 — Build the decision object, or the plan

```bash
python3 scripts/validate_decision.py --new-id
```

Write the decision to a temporary JSON file (use the session scratch directory,
not this repo). Money is always a **decimal string** (`"25.00"`), never a JSON
number.

**Which shape to build:**

| Outcome | Build | Validate with |
|---|---|---|
| `WAIT` | one WAIT decision object | `validate_decision.py` |
| `SINGLE_BUY` | one BUY decision object | `validate_decision.py` |
| `SPLIT_BUY_PLAN` | a plan wrapping 2–5 BUY legs | `validate_plan.py` |

A `SPLIT_BUY_PLAN` wraps the same BUY objects shown below — one per leg, each
with **its own** `decision_id` (run `--new-id` once per leg) and its own
`allocation_rationale`:

```json
{
  "plan_id": "plan_...",
  "plan_type": "SPLIT_BUY_PLAN",
  "split_rationale": "Why splitting beats concentrating these dollars into the single best candidate.",
  "plan_sprawl_assessment": {
    "positions_after_this_plan": "...",
    "why_not_concentrate_into_one": "...",
    "smallest_leg_significance": "..."
  },
  "legs": [
    { "...one full BUY object...",
      "allocation_rationale": {
        "why_better_than_other_legs": "...",
        "why_this_amount": "...",
        "legs_compared_against": "Weighed directly against <SIBLING TICKER> in this plan."
      }
    }
  ]
}
```

Each leg's `monthly_budget_before_usd` / `monthly_budget_after_usd` must reflect
the authorization **after the legs ahead of it**, not the full remaining amount.
See `docs/STAGE7_MULTI_BUY.md`.

### BUY — equity or ETF

The BUY payload is generated from one contract, `src/decision_schema.py`, which
is the same module the scheduled runner's scaffold prints and the same set of
key names `src/guardrails.py` validates. Print it with
`python3 scripts/emit_buy_scaffold.py` (add `--kind crypto` for a pair), and add
your own `decision_id` — an interactive decision mints one, a scheduled
recommendation does not.

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

`proposed_amount_usd` is a **choice**, not a default. `new_position_justification`
and `research_package` are required only for `NEW_POSITION`; `monthly_optionality`,
`portfolio_sprawl_assessment` and `theme` are required for every BUY.

`asset_class` must agree with `asset_type`: `us_common_stock`→`EQUITY`,
`us_etf`→`ETF`, `crypto`→`CRYPTO`.

### BUY — crypto

Same shape, with `"asset_class": "CRYPTO"`, `"asset_type": "crypto"`, a
hyphenated pair in `ticker` (e.g. `"BTC-USD"`), and **no** `exchange` field.

### BUY — adding to an existing position

Add `"position_type": "EXISTING_POSITION"` and a `cost_basis_analysis`:

```json
"cost_basis_analysis": {
  "average_cost_usd": "83.92",
  "current_price_usd": "230.35",
  "raises_or_lowers_average": "RAISES",
  "what_changed_since_purchase": "...",
  "thesis_still_holds": "...",
  "valuation_then_vs_now": "...",
  "position_concentration": "13.6% of the total portfolio",
  "economic_meaning": "Why the direction of the average is or is not economically meaningful."
}
```

`raises_or_lowers_average` is arithmetic and is checked against
`current_price_usd`. Buying above your average **RAISES** it. Never claim
otherwise, and never treat a lower average as a reason in itself.

### WAIT

```json
{
  "decision_id": "dec_...",
  "decision": "WAIT",
  "confidence": "MEDIUM",
  "wait_basis": ["VALUATION", "PRESERVING_MONTHLY_OPTIONALITY"],
  "monthly_budget_remaining_usd": "25.00",
  "thesis": "Why no purchase is justified right now.",
  "timing_reason": "What would have to change for the next evaluation to be a BUY.",
  "alternatives_considered": [{"symbol": "...", "why_not": "..."}],
  "best_existing_equity_candidate": {"symbol": "...", "classification": "...", "why_not": "..."},
  "best_new_equity_candidate": {"symbol": "...", "classification": "...", "why_not": "..."},
  "best_existing_crypto_candidate": {"symbol": "...", "classification": "...", "why_not": "..."},
  "best_new_crypto_candidate": {"symbol": "...", "classification": "...", "why_not": "..."},
  "candidate_pool": [{"symbol": "...", "provenance": "...", "classification": "..."}],
  "evidence": [{"tool": "get_portfolio", "detail": "..."}],
  "events_considered": [
    {
      "label": "MU FQ4 earnings",
      "date": "2026-09-30",
      "asset_class": "EQUITY",
      "occurs_after_close": true,
      "actionable_with_this_month_authorization": false
    }
  ],
  "authorization_expiry_acknowledged": "MU reports after the close on the final September session, so the equity reaction is first tradable on 2026-10-01 with October's authorization. Waiting for it means allowing September's $25.00 to expire unused, which is accepted deliberately because nothing on offer clears the bar."
}
```

`events_considered` is **required** whenever `wait_basis` includes
`AWAITING_INFORMATION`, and `authorization_expiry_acknowledged` is required
whenever any listed event falls past the month's final tradable opportunity
(Step 8d).

## Step 11 — Validate (the authoritative gate)

```bash
python3 scripts/validate_decision.py /path/to/decision.json   # WAIT or SINGLE_BUY
python3 scripts/validate_plan.py     /path/to/plan.json       # SPLIT_BUY_PLAN
```

`validate_plan.py` runs every leg through the same single-decision validator,
against a budget state that already reflects the legs ahead of it, then applies
the cross-leg checks.

- **Passes** → go to Step 12.
- **Fails** → read the violations. Either fix a genuine mistake in your decision
  object (a typo, wrong arithmetic, a mis-stated field) and re-validate, or
  accept that the proposal is not permitted and revise the *decision*.

**Never** edit `config.json`, the policy documents, `src/guardrails.py`,
`state/budget.json`, or `data/crypto_universe.json` to make a rejected proposal
pass. If you believe a guardrail is genuinely wrong, stop and tell the user.

## Step 12 — Log it

```bash
python3 scripts/validate_decision.py /path/to/decision.json --log   # WAIT or SINGLE_BUY
python3 scripts/validate_plan.py     /path/to/plan.json     --log   # SPLIT_BUY_PLAN
```

Appends to `logs/decisions.jsonl` and updates `state/last_evaluation.json`. WAIT,
every BUY leg, and rejected proposals are all logged. A plan logs **one entry per
leg**, each keeping its own `decision_id`.

`state/budget.json` is **not** modified, because nothing is executed. In dry-run
mode the monthly authorization is never actually consumed.

## Step 13 — Do not send an order

There is no order to send. Confirm to the user that none was placed.

## Step 14 — Report, and write down what you learned

Explain your reasoning: the portfolio as it actually is, the candidate pool and
where it came from, the finalists with their bull and bear cases, the allocation
competition, and what you are watching for next time. Be explicit about what data
you could not get.

**Then persist the research.** For any candidate whose standing view this
evaluation materially advanced or changed — new primary-source evidence, a
research component closed, a changed classification, a thesis that broke — write
or refresh `research/<SYMBOL>.md` in the shape described in
`prompts/scheduled_evaluation.md` Step 11, and set `last_reviewed` to today.
Leave untouched notes byte-identical, and **never delete or cut down a note**:
that is research a future evaluation would otherwise have reused.

End your output with exactly one of these blocks, and nothing after it:

```
DECISION: WAIT
Remaining monthly authorization: $<REMAINING>
Reason: <one or two sentences>
(DRY RUN - no order was placed.)
```

```
DECISION: SINGLE_BUY
Asset: <TICKER or PAIR> (<EQUITY|ETF|CRYPTO>, <EXISTING_POSITION|NEW_POSITION>)
Amount: $<AMOUNT> of the $<REMAINING> available (full / partial deployment)
Decision ID: dec_<...>
Reason: <one or two sentences>
Remaining monthly authorization: $<REMAINING AFTER>
(DRY RUN - no order was placed. This leg requires its own separate approval.)
```

```
DECISION: SPLIT_BUY_PLAN
Plan ID: plan_<...>
Legs: <2-5>

  [0] <TICKER or PAIR> (<EQUITY|ETF|CRYPTO>, <EXISTING_POSITION|NEW_POSITION>)
      Amount: $<AMOUNT>
      Decision ID: dec_<...>
      Thesis: <one or two sentences>
      Better than the other legs because: <named comparison against a sibling>

  [1] ... (same fields)

Combined amount: $<TOTAL> of the $<REMAINING> available
Remaining monthly authorization: $<REMAINING AFTER>
(DRY RUN - no order was placed. EVERY leg requires its own separate approval.)
```
