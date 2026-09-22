# INVESTMENT POLICY — Long-Term Capital Allocation (Version 7)

**Status:** DRY RUN / DECISION ONLY. **No live order execution.**
**Canonical budget source:** `config.json` → `monthly_budget_usd`
**Canonical horizon floor:** `config.json` → `min_investment_horizon_months`
**Authoritative enforcement:** `src/guardrails.py` (deterministic code), not this document.
**Companion:** `DISCOVERY_POLICY.md` covers *how candidates are found and ranked*.

This document states *intent and reasoning standards*. Where this document and
`src/guardrails.py` disagree about whether an action is permitted, **the code wins**.

---

## 1. Primary objective

> **Maximize long-term total return / capital appreciation over a multi-year
> investment horizon while taking intelligent, deliberate risk.**

The intended holding period is **at least 2 years**, and potentially much longer.
`config.json` sets a hard floor of 24 months; a proposal that states a shorter
horizon is rejected by code.

This is explicitly **not** an objective to:

- make money every day;
- day trade, swing trade, or scalp;
- chase daily price fluctuations;
- maximize the number of trades;
- force the monthly budget to be invested.

Short-term market information may be used to **improve entry timing**. Short-term
trading is not the strategy.

Every decision ultimately answers one question:

> *Given everything I already own, everything I am watching, current valuations,
> business/asset quality, future opportunity, risks, and current market
> conditions — where does this next dollar have the best expected long-term use?*

---

## 2. Monthly capital

1. The maximum **new purchase** total per calendar month is **$25.00 USD**.
2. That $25 is **one shared authorization across all eligible asset classes**.
   Equities, ETFs, and crypto compete for the same dollars, in the same
   evaluation, on the same terms. Examples of valid months: $25 stock; $25 ETF;
   $25 crypto; $15 stock + $10 crypto; $10 added to an existing holding + $15
   into a new one; $10 equity + $5 BTC-USD + $10 a second equity; or
   **$0.00 — WAIT**.
3. Multiple purchases within a month are allowed; their combined value must never
   exceed $25.00. Since Version 7 they may be proposed together as a
   `SPLIT_BUY_PLAN` (§2c) rather than only as separate evaluations.
3a. **The ceiling counts four quantities for the calendar month:**

    ```
    executed + pending + approved-but-unsubmitted + proposed  <=  $25.00
    ```

    The broker reports the first two. The third — a leg a human has already
    approved but that has not yet been submitted — is invisible to both the
    broker and the local ledger, and must be reserved explicitly.
    `src/allocation.combined_exposure()` computes it and
    `assert_plan_within_authorization()` enforces it.
4. The agent may spend less than $25.00, and may spend nothing at all.
5. The agent **must never manufacture a reason to invest** because budget exists.
6. **Unspent budget does not roll over.** Every calendar month restarts at exactly
   $25.00. Only an explicit change to `config.json` by the account owner changes
   this. There is no catch-up spending after a quiet month.
7. **Account cash is not authorization.** Buying power answers "is this
   feasible?"; the monthly budget answers "is this permitted?" — and it is
   always the binding constraint. A $10,000 balance still authorizes $25.00.
8. **Do not force a purchase at month end** simply to use the budget. Letting
   $25.00 expire is strictly better than buying something the agent does not
   believe in.

## 2a. Monthly capital optionality — spending today has an opportunity cost

The agent's job is **not** to choose the best investment available today. It is
to allocate a limited $25 across an entire calendar month.

**Capital spent on the 5th is unavailable on the 30th.** That forfeited
flexibility is a real cost and must be priced into every BUY.

Before every BUY, explicitly evaluate:

- days remaining in the calendar month;
- known earnings, events, or catalysts still to come this month;
- important candidates currently under research;
- the probability that new information materially reorders the ranking;
- the benefit of preserving dry powder;
- **whether partial deployment beats using the whole budget.**

Valid outcomes are **BUY $5 · $10 · $15 · $20 · $25 · WAIT**, and any other
whole-cent amount at or above the broker minimum ($1.00 for a dollar-based
fractional equity order; per-pair minimum order size for crypto).

> **Never assume a valid BUY should consume the entire remaining budget.**
> "This is a good investment" does not imply "$25 of it, today."

**The mid-month gate.** If a purchase would bring cumulative spending above
**50% of the month's authorization before the 15th**, the decision must carry a
substantive `monthly_optionality_analysis` explaining why surrendering the rest
of the month's flexibility is justified. `src/guardrails.py` enforces this.

This is a **reasoning requirement, not a prohibition.** A well-argued full
deployment on the 3rd is permitted. An unargued one is not.

**The month's last tradable opportunity.** "Later this month" ends before the
31st does. Equities and ETFs trade only in exchange sessions, so the edge is the
**close of the month's last trading day**; crypto trades continuously, so its
edge is the **last instant of the month**. Report
`monthly_optionality.tradable_sessions_remaining` where it matters — at month
end, twelve calendar days can be eight sessions, and only the last of them can
spend this month's authorization. `src/market_calendar.py` computes both edges,
NYSE holidays and half sessions included.

### An event past that edge is next month's information

> **An event occurring after the month's final tradable opportunity must never
> be described as information that can be acted on with that month's
> authorization.**

The authorization does not carry (§2). If there is no session left in the month
after the event, the month's $25 cannot respond to it, whatever the calendar
says about the date.

**Worked example.** MU reports after the close on **2026-09-30**, September's
last session. The normal post-earnings *equity* reaction is first tradable on
**2026-10-01**, funded by **October's** authorization. September's $25 cannot
reach it and does not roll over. The same instant would still be inside
September for a **crypto** position, because crypto has no closing bell — which
is exactly why the edge is computed per asset class rather than assumed.

Any decision that weighs dated events carries `events_considered`, a list in
which each entry states:

| Field | Meaning |
|---|---|
| `label` | what the event is |
| `date` | `YYYY-MM-DD` |
| `asset_class` | `EQUITY`, `ETF` or `CRYPTO` — which market's clock applies |
| `occurs_after_close` / `session_timing` | whether the information lands after that session ends |
| `actionable_with_this_month_authorization` | true or false — **checked against the calendar, not trusted** |

A WAIT resting on `AWAITING_INFORMATION` (§2b) **must** supply that list, since
that basis is entirely a claim about when information arrives.

**Waiting through such an event is permitted and often right.** It has one
mandatory consequence:

> Waiting through an event past the month's final tradable opportunity may
> **intentionally allow that month's authorization to expire unused**. The
> decision and the report must **state that explicitly**, in
> `authorization_expiry_acknowledged`.

An expired authorization is a legitimate outcome (§2, §2b). Letting it expire
while describing the wait as though the budget could still act on the event is
not — and `src/guardrails.py` rejects it
(`EVENT_ACTIONABILITY_MISREPORTED`, `UNACKNOWLEDGED_AUTHORIZATION_EXPIRY`).

---

## 2b. WAIT does not require a future event

A WAIT is **not** conditional on expecting new information. Any of these is a
complete and sufficient reason on its own:

- valuation is unattractive;
- the entry price does not offer enough expected return;
- risk/reward is mediocre;
- uncertainty is unusually high;
- the price looks extended;
- stronger candidates may plausibly emerge;
- nothing clears the investment threshold;
- preserving monthly optionality is worth more than any candidate on offer;
- research is incomplete.

Every WAIT must declare a `wait_basis` from that list. `AWAITING_INFORMATION` is
one option among ten, not the default.

Correspondingly:

> **"No new information is expected soon" is NOT sufficient reasoning for BUY.**

A strong company can still be a bad investment at the current price. A BUY must
rest on the expected return available at **today's entry price**, which is why
every BUY carries `why_not_wait` and `margin_for_error`. The validator warns
whenever a timing argument leans on the absence of upcoming information.

---

## 2c. The three permitted outcomes

An evaluation returns exactly one **plan**:

| `plan_type` | Legs | Meaning |
|---|---|---|
| `WAIT` | 0 | Nothing clears the bar. The budget stays as cash. |
| `SINGLE_BUY` | 1 | One purchase. |
| `SPLIT_BUY_PLAN` | 2–5 | Several independently justified purchases. |

Legs may mix asset classes freely: `$10` equity + `$5` BTC-USD + `$10` a second
equity is a valid split when each leg is justified on its own.

> **Splitting is never required, and must never be forced.** Multiple purchases
> to "diversify", to look active, or to use up the budget are all failures of
> judgement. One high-conviction purchase, or WAIT, is frequently the better
> answer. Splitting must not be used to avoid making a judgement.

A `SPLIT_BUY_PLAN` therefore has to argue for its own shape. Each leg carries an
`allocation_rationale` answering three questions — why this money is better here
than on the sibling legs (`why_better_than_other_legs`), why this size
(`why_this_amount`), and which siblings it was weighed against, **named by
ticker** (`legs_compared_against`). The plan as a whole carries a
`split_rationale` explaining why splitting beats concentrating the same dollars
into the single best candidate, plus a `plan_sprawl_assessment`, because a split
creates several positions at once — exactly when sprawl is most likely and least
noticed.

**Every leg remains a fully independent decision.** Each keeps its own
`decision_id`, fingerprint, approval, submission ticket, reconciliation and
replay protection. There is deliberately no plan-level approval: a two-leg plan
requires two separate human approvals. See `docs/STAGE7_MULTI_BUY.md`.

---

## 3. Eligible investments

### Equities
- U.S.-listed common stocks.
- U.S.-listed, **non-leveraged** ETFs.
- Fractional-share-eligible securities where a whole share costs more than the
  amount invested (at $25/month, nearly always).
- Must be listed on a major U.S. exchange (NYSE, NASDAQ, NYSE Arca, Cboe BZX,
  NYSE American, IEX).

### Crypto
- **Direct cryptocurrencies supported and tradable through Robinhood Crypto**,
  in this account and jurisdiction.
- Eligibility is not a matter of opinion. A crypto proposal must name a
  hyphenated USD pair (`BTC-USD`) that appears in `data/crypto_universe.json` as
  `tradable`, individually tradable, not halted, not display-only, and above its
  minimum order size at the proposed dollar amount. That snapshot comes from
  Robinhood's own `get_currency_pairs` and is refreshed by the agent.

Equities and crypto may be compared directly against each other for the same $25.

---

## 4. Forbidden — absolutely, in all cases

- **Selling** any existing investment, for any reason. (See §5.)
- Options of any kind.
- Margin, borrowing, or leverage of any kind.
- Short selling or any short exposure.
- Leveraged ETFs/ETNs (2x, 3x, "Ultra", "UltraPro", geared) — including
  leveraged single-crypto ETPs.
- Inverse ETFs/ETNs (-1x, "Short", "Bear", "UltraShort").
- Futures, crypto futures, forex, prediction-market/event contracts.
- OTC / pink-sheet / non-major-exchange securities.
- Penny-stock speculation (share price below $1.00 is a hard rejection).
- Any transfer, deposit, withdrawal, or movement of money.
- Any change to Robinhood account settings, margin level, or option level.
- Any modification of watchlists, scans, or alerts.

The agentic Robinhood account is of type `limited_margin`. That is a broker
attribute, **not** permission. Margin remains forbidden.

---

## 5. Selling is disabled

Version 2 has **no sell path**. The agent may analyse existing holdings and
label them:

- `ADD_CANDIDATE` — attractive to add to
- `HOLD_NO_ADD` — keep, but no new capital
- `THESIS_WEAKENING` — deterioration worth flagging
- `OVERCONCENTRATED` — too large a share of the portfolio

It must **not** sell, and must not recommend selling as an action. A separate
selling policy may be designed later. Flagging `THESIS_WEAKENING` is information
for the account owner, not an instruction.

---

## 6. Portfolio-aware capital allocation

**Every evaluation begins with the existing portfolio**, read live from
Robinhood across **all** accounts.

**Five** uses of new capital compete on equal terms, and every evaluation must
explicitly compare all of them wherever each is relevant:

| | Option | WAIT must name |
|---|---|---|
| **A** | Add to an existing **equity or ETF** position | `best_existing_equity_candidate` |
| **B** | Open a new **equity or ETF** position | `best_new_equity_candidate` |
| **C** | Add to an existing **crypto** position | `best_existing_crypto_candidate` |
| **D** | Open a new eligible **crypto** position | `best_new_crypto_candidate` |
| **E** | Preserve some or all of the budget as cash — **WAIT** | the decision itself |

A `WAIT` must name the strongest candidate in A–D and say why each fell short.
`src/guardrails.py` enforces this (`MISSING_CAPITAL_USE_COMPARISON`), and the
pre-Version-7 keys `best_existing_position_candidate` and `best_crypto_candidate`
are still accepted as aliases so previously logged decisions stay valid.

Bucket A's field is named `best_existing_equity_candidate` for compatibility
with those aliases, but it covers **both individual stocks and eligible ETFs**
already held. An existing VOO, SPY or QQQ position is an add-candidate exactly
as an existing NVDA position is; the field name is not a restriction to single
stocks.

### What "existing position" means

> **An existing position is anything held anywhere in the owner's Robinhood
> portfolio, across ALL accounts — not only the agentic account.**

The agentic account is the **execution destination**, not the definition of what
is owned. This governs `position_type` (`EXISTING_POSITION` vs `NEW_POSITION`),
the new-position hurdle in §6a, portfolio fit in §8, sprawl in §8a, and the
five-way comparison above.

> **A bucket is not `NOT_APPLICABLE` merely because the agentic account does not
> currently hold that asset.** If BTC is held in the individual account, option C
> applies and must be answered on its merits, even when the agentic account is
> empty.

`NOT_APPLICABLE: <reason>` is reserved for a bucket that genuinely does not
exist — for example, if the owner held no cryptocurrency in *any* account.
**Silence is not the same as inapplicable**, and neither is an empty agentic
account.

Where a holding sits still matters for execution: cost basis comes from the
account that holds it, and any purchase settles in the agentic account. Name the
account when it bears on the reasoning, and mask it (`••••0002`).

- Do **not** favour novelty because a company is new.
- Do **not** favour an existing holding because it is already owned.
- Do **not** treat crypto as a residual category considered only after the
  equity work is done. It competes for the same dollars on the same terms.
- `WAIT` is a real competitor, not a fallback.

## 6a. The new-position hurdle

The portfolio already contains many small positions. **Do not create new
positions casually.**

Before opening a `NEW_POSITION`, compare it directly against putting the same
capital into the strongest existing holdings, and record that comparison in
`new_position_justification`:

- incremental expected return versus the best existing alternative;
- diversification benefit;
- overlap with existing holdings;
- **whether the diversification is economically useful or merely cosmetic**;
- expected conviction;
- whether an existing holding already provides comparable or superior exposure;
- whether the position would initially be too small to matter;
- the specific existing holding it was compared against, named.

> **Do not diversify simply to fill an empty sector or theme.**
> Missing exposure to healthcare, robotics, financials, or power is a **research
> signal, not a reason to buy.** Expected long-term risk-adjusted return remains
> the primary objective, and a new ticker is never inherently preferable to
> increasing a high-conviction existing position.

### Adding to existing holdings

Reasons that can justify adding: the thesis has strengthened; fundamentals are
improving; the long-term opportunity remains large; valuation has become more
attractive; a decline appears disconnected from long-term fundamentals; exposure
to an attractive theme is too small; the current price is an attractive long-term
entry; a high-quality asset trades below a reasonable estimate of value.

Reasons not to add: thesis deterioration; structural competitive problems;
declining financial quality; excessive concentration; valuation still
unreasonable; better opportunities elsewhere; a falling price that reflects
genuine deterioration.

---

## 7. Cost basis and averaging down

Lowering the dollar-cost average may be useful. It is **never a goal by itself.**

**Forbidden reasoning:**
> "The price is below my cost basis, therefore buy more."

**Required reasoning:**
> "The price is below my cost basis **and** the long-term thesis remains intact
> or has improved **and** expected forward return appears attractive relative to
> other uses of this capital."

Every proposal to add to an existing position must carry a `cost_basis_analysis`
covering: approximate average cost; current price; price relative to cost basis;
what fundamentally changed since purchase; whether the original thesis still
holds; valuation now versus at purchase where knowable; position concentration;
expected long-term upside; major downside risks; and alternative uses of the same
capital.

It must explicitly distinguish **good averaging down** from **catching a falling
knife**.

**Never average down to make an unrealized percentage loss look smaller.** The
average cost is an accounting artifact of past decisions; it has no bearing on
the forward return from today's price.

The direction the average moves is arithmetic, not opinion, so
`src/guardrails.py` checks it: a decision claiming a purchase "LOWERS" the
average when the current price is above the stated average cost is rejected
(`COST_BASIS_DIRECTION_WRONG`).

---

## 7a. Crypto is a full competitor, researched on its own terms

Crypto is an eligible asset class with equal standing. It must **never be
treated as a secondary afterthought** — a footnote appended once the equity
analysis is done. It competes for the same $25, in the same evaluation, on the
same terms.

**Research it with the crypto framework in `DISCOVERY_POLICY.md` §6**, never
with equity metrics. There is no P/E, no operating margin and no balance sheet
for a cryptocurrency; reaching for one is a category error, and the mandatory
research components in `DISCOVERY_POLICY.md` §4b reflect what actually matters:
market cap, supply and issuance, network usage and adoption, ecosystem and
development, security and decentralization, regulatory risk, competing networks,
and long-term price and drawdown history.

Every evaluation must examine:

- **existing crypto holdings across all accounts** — not merely the agentic
  account — and their cost basis where the broker reports it
  (`get_crypto_positions` returns `cost_bases`; note when direct quantity is
  materially less than total quantity, since the average then covers only part);
- **concentration** — crypto's share of total portfolio value, and the largest
  single coin's share within it;
- **eligibility** — which pairs Robinhood supports and will trade in *this*
  account, from `data/crypto_universe.json` (§3). This allow-list is never
  bypassed, loosened, or hand-edited;
- **current, dated crypto market conditions.**

New crypto ideas **outside** the existing holdings are legitimate when the
available research supports them. Discovery applies to crypto as it does to
equities.

### Where crypto research comes from — and what Robinhood is for

Robinhood is authoritative for facts about **this account** and for nothing
else:

| Robinhood is the source of truth for | External read-only sources supply |
|---|---|
| holdings, across all accounts | network usage and adoption |
| cost basis | issuance, supply and tokenomics |
| pair eligibility and tradability | protocol development and roadmap |
| halted / display-only status, minimum order size | ecosystem and application activity |
| orders, order history, execution | security and decentralization |
| buying power and account state | regulatory developments |
| | institutional adoption |
| | competing networks |

No external source may override the left-hand column. But Robinhood is a broker,
not a research database: it publishes none of the right-hand column and never
claimed to. **Use read-only web research when it is needed to complete the
framework**, prioritising primary and official sources — the protocol's own
specification and improvement proposals, the foundation's or core team's
publications, the reference implementation, on-chain data, executed governance
decisions, a regulator's own filings, and reputable named data providers. See
`DISCOVERY_POLICY.md` §4c for the ranked hierarchy and the `source_type` values.

> **A crypto candidate must not be marked `RESEARCH_INCOMPLETE` merely because
> Robinhood does not expose a research field**, when reliable read-only external
> sources can reasonably supply it.

That is not a safe default; it is a research failure dressed as one, and it
would make every crypto candidate unbuyable forever for a reason that describes
the tool surface rather than the asset. `src/guardrails.py` rejects a waived
crypto research component, or a dismissed crypto WAIT bucket, whose stated
reason is the broker's or the tool set's scope (`RESEARCH_GAP_NOT_JUSTIFIED`).

`RESEARCH_INCOMPLETE` remains correct whenever the research genuinely could not
be done — and it often will be. Name the actual obstacle: the source that could
not be reached, the figure no reputable provider publishes, read-only web
research unavailable in this run. A real obstacle is a valid waiver. And a
complete package is never itself a reason to buy: concentration, volatility and
price still have to be argued past.

### The two temptations, named

1. **A large unrealized loss is not, by itself, a reason to average down.**
   §7 applies unchanged: the average cost is an accounting artifact of past
   decisions and has no bearing on forward return from today's price. "It is
   down 60%, so it is cheap" is not a thesis.
2. **Recent price appreciation is not, by itself, a reason to buy.** Nor is a
   recent fall. Price per token is meaningless — a $0.08 coin is not cheaper
   than an $80,000 coin. Only market capitalization relative to durable
   relevance means anything.

### Volatility and concentration must be explicit

Any crypto BUY leg must state, in words rather than by implication:

- the **volatility** being accepted — drawdowns of 70–90% have historically
  occurred in many cryptoassets, including the largest ones, and should be
  treated as a **plausible risk** rather than a tail case, which also makes the
  24-month floor a *short* horizon for this asset class;
- the effect on **portfolio concentration** — what crypto's share becomes after
  the purchase, and what the largest single coin's share becomes.

A crypto leg that does not address both has not made its case.

## 8. Portfolio fit

A candidate cannot be evaluated in isolation. Before any BUY:

- What is already owned?
- Does this duplicate existing exposure?
- Does it materially increase concentration?
- Does it diversify into an attractive new source of return?
- Is an existing holding a better home for this $25?
- Does it materially change portfolio risk, and does the upside justify that?

Do not diversify merely to own many tickers. Concentration can be rational when
supported by evidence — but it must be **explicitly recognised**, never
accidental.

---

## 8a. Portfolio sprawl

Every BUY must carry a `portfolio_sprawl_assessment` covering:

- the number of positions held;
- the size of the smallest positions;
- the economic significance of adding another $5-$25 position;
- overlap among existing positions;
- whether another ticker meaningfully improves expected return or
  diversification, or whether the capital would be better concentrated into a
  higher-conviction holding.

There is deliberately **no maximum ticker count**. The requirement is that the
question be asked honestly each time, not that a number be respected.

---

## 9. Entry timing

The horizon is 2+ years, but entry price still matters.

Short-term data may legitimately inform *when during the month* to buy: current
valuation, an upcoming earnings date, an unusual short-term run-up, a meaningful
pullback, a market-wide selloff, company-specific news, technical
overextension, or improving price confirmation.

However:

- Do **not** attempt to time the exact bottom.
- A strong long-term opportunity should not be rejected merely because a
  slightly cheaper entry might appear later.
- A long horizon does **not** justify buying at any valuation.

---

## 10. Research quality

- **Do not invent data.** A number that was not retrieved from a tool must not
  appear as a fact. Write "not available".
- Prefer primary/company/SEC/official sources where accessible.
- **Distinguish factual evidence from inference**, explicitly.
- Identify the **date** of every material piece of information; do not rely on
  stale news without saying so.
- Identify **conflicting evidence** rather than suppressing it.
- Construct both a **BULL and a BEAR case** for every deep-research finalist.
- State major **unknowns** explicitly.
- **Never hide uncertainty.** `LOW` confidence is a valid, often correct answer.
- **Do not present short-term price forecasts as knowledge.**
- A compelling story is not sufficient. **Do not confuse a good company with a
  good stock at any price.**

---

## 11. The decision

Every evaluation produces **exactly one** plan: `WAIT`, `SINGLE_BUY`, or
`SPLIT_BUY_PLAN` (§2c). A `SINGLE_BUY` carries one BUY leg; a `SPLIT_BUY_PLAN`
carries 2–5. Each leg is a complete BUY decision in the form below, validated by
the unchanged single-decision validator and keeping its own `decision_id`.

### A BUY must contain

| Field | Meaning |
|---|---|
| `ticker` / `symbol` | Ticker, or hyphenated crypto pair (`BTC-USD`) |
| `security_name` | Full name |
| `asset_class` | `EQUITY`, `ETF`, or `CRYPTO` |
| `asset_type` | `us_common_stock`, `us_etf`, or `crypto` |
| `position_type` | `EXISTING_POSITION` or `NEW_POSITION` |
| `classification` | A label from `DISCOVERY_POLICY.md` that supports a purchase |
| `proposed_amount_usd` | Dollar amount |
| `monthly_budget_before_usd` / `monthly_budget_after_usd` | Authorization before and after |
| `investment_horizon_months` | ≥ 24 |
| `current_price_usd` + `quote_timestamp` | Price and as-of time |
| `thesis` | The long-term investment thesis |
| `value_creation` | Expected source of future value creation |
| `valuation_reasoning` | Valuation and entry reasoning |
| `timing_reason` | Why **now** rather than later |
| `alternatives_considered` | Why this candidate beat the alternatives |
| `portfolio_exposure` | Relevant existing portfolio exposure |
| `risks` | Key risks, honestly |
| `invalidation` | What would invalidate the thesis |
| `confidence` | `LOW` / `MEDIUM` / `HIGH` |
| `evidence` | Data used, with source tool names |
| `cost_basis_analysis` | **Required** when `position_type` is `EXISTING_POSITION` |
| `events_considered` | Dated events weighed, each classified against the month's final tradable opportunity (§2a) |
| `authorization_expiry_acknowledged` | **Required** when any such event falls past that edge |

### A SPLIT_BUY_PLAN must additionally contain

| Field | Meaning |
|---|---|
| `plan_id` | Identifies the plan; each leg still has its own `decision_id` |
| `plan_type` | `SPLIT_BUY_PLAN` |
| `legs` | 2–5 complete BUY decisions |
| `split_rationale` | Why splitting beats concentrating into the single best candidate |
| `plan_sprawl_assessment` | `positions_after_this_plan`, `why_not_concentrate_into_one`, `smallest_leg_significance` |

And on **each** leg:

| Field | Meaning |
|---|---|
| `allocation_rationale.why_better_than_other_legs` | Why this money is better here than on the siblings |
| `allocation_rationale.why_this_amount` | Why this size rather than more or less |
| `allocation_rationale.legs_compared_against` | Which sibling legs, **named by ticker** |

### A WAIT must contain

| Field | Meaning |
|---|---|
| `monthly_budget_remaining_usd` | Remaining authorization |
| `wait_basis` | One or more valid bases (§2b of this document, §9 of `CLAUDE.md`) |
| `best_existing_equity_candidate` | Strongest add-to-existing-equity candidate, and why not |
| `best_new_equity_candidate` | Strongest new equity/ETF candidate, and why not |
| `best_existing_crypto_candidate` | Strongest add-to-existing-crypto candidate, and why not |
| `best_new_crypto_candidate` | Strongest new eligible crypto candidate, and why not |
| `thesis` | Why no purchase is justified now |
| `timing_reason` | What would change the decision |
| `alternatives_considered` | Candidates genuinely evaluated |
| `confidence` | Confidence in the WAIT call |
| `evidence` | Data used |
| `events_considered` | **Required** when `wait_basis` includes `AWAITING_INFORMATION`; each event classified against the month's final tradable opportunity (§2a) |
| `authorization_expiry_acknowledged` | **Required** when waiting through an event past that edge, stating that the month's authorization may expire unused |

WAIT requires naming a best candidate in **each of the four** buckets, or an
explicit `"NOT_APPLICABLE: <reason>"` where one genuinely does not apply.
"Nothing looked good" is not an acceptable WAIT; the agent must show the
competition it ran, and that competition must include crypto on both sides —
adding to what is held, and opening something new.

The pre-Version-7 keys `best_existing_position_candidate` and
`best_crypto_candidate` remain accepted as aliases, so decisions logged before
Version 7 do not retroactively become invalid.

---

## 11a. Future live architecture — Robinhood is the budget source of truth

**Not enabled. Documented and tested now, before it is ever needed.**

When live execution eventually exists, `state/budget.json` **will not be trusted
on its own.** Before any live purchase the executor must reconcile the local
ledger against what the broker actually shows for the agentic account in the
current calendar month:

- completed purchases and fills;
- open and pending orders;
- partially filled orders;
- the locally recorded committed amount;
- duplicate decision IDs.

The safe spendable authorization is the **most conservative** reconciled result:

```
reconciled_committed = max(local_committed, broker_filled + broker_pending)
safe_remaining       = authorized - reconciled_committed
```

Worked example:

| | |
|---|---:|
| configured monthly authorization | $25.00 |
| actual completed purchases | $10.00 |
| pending authorized order | $5.00 |
| **maximum additional order** | **$10.00** |

Rules the implementation enforces (`src/reconciliation.py`, 27 tests):

- An **open** order reserves its **full** requested notional even if only partly
  filled, because the remainder can still execute.
- A **cancelled or rejected** order still counts whatever actually executed
  before it died.
- A **manual purchase** made in the app, which the local ledger never saw, still
  consumes authorization.
- A **stale local file** from a previous month contributes $0.00 of local
  spending, and only broker data is trusted for the current month.
- Local state claiming an authorization **above** `config.json` is ignored in
  favour of the configured value.
- Reconciled spending already **exceeding** the authorization blocks all further
  purchases.
- Any **unreadable** order — unknown state, missing id, malformed timestamp,
  unparseable amount — raises `ReconciliationError`. A failed broker read must
  never be read as "nothing was bought."
- A **sell** appearing in the agentic account blocks everything and its proceeds
  are never treated as new authorization.

> A crash, a manual purchase, a partial fill, or a stale local file must never
> make additional authorization appear available.

---

## 11b. Execution modes and the principle of approval

`config.json` carries three independent switches. **All three must be open before
execution is even considered**, and today all three are closed:

| Switch | Value | Meaning |
|---|---|---|
| `execution_mode` | **`DRY_RUN`** | `DRY_RUN` / `APPROVAL_REQUIRED` / `AUTONOMOUS`. Anything else fails closed at load. |
| `agent_enabled` | **`false`** | Kill switch. False means no execution, no submission, no live-order path. |
| `live_trading` | **`false`** | Retained interlock from earlier versions. |

`AUTONOMOUS` parses but is **refused unconditionally** with
`AUTONOMOUS_NOT_IMPLEMENTED`. Unattended execution has not been designed.

### The principle of approval

> **A BUY recommendation is not permission to execute.**

Three states are kept strictly separate:

```
MODEL DECISION  ->  USER APPROVAL  ->  EXECUTION
   PROPOSED          APPROVED         (disabled)
```

An evaluation can only ever produce `PROPOSED`. Only `scripts/approve_decision.py`
creates `APPROVED`. **Natural-language reasoning from Claude is never approval**,
and a decision payload carrying `approved`, `approval`, `approved_by`,
`user_approved`, or `execution_state` is rejected outright as
`SELF_APPROVAL_ATTEMPTED` — by the guardrails *and* again by the executor.

An approval identifies exactly one decision: decision id, asset, asset class,
action, **maximum** dollar amount, calendar month, timestamp, and a SHA-256
fingerprint of the canonical decision payload. It authorizes nothing else — not
another ticker, not another coin, not a larger amount, not a sell, not another
decision id, not another month, and not a materially modified decision.

### Immutability

The fingerprint covers `decision_id`, `action`, `side`, `asset`, `asset_class`,
`asset_type`, `position_type`, `proposed_amount_usd`, and `month`. Change any of
them and the approval is void (`DECISION_MODIFIED`). **An approved $10.00
purchase can never quietly become $15.00** — that trips both the fingerprint and
the separate `AMOUNT_EXCEEDS_APPROVAL` cap. Rewording a thesis does not void an
approval; those fields do not bind.

### Expiration

Approvals expire after **24 hours**, or sooner if the decision requests a shorter
window (never longer). They also expire when:

- the calendar month changes;
- the decision payload changes;
- the policy surface changes — `config.json`, `INVESTMENT_POLICY.md`,
  `src/guardrails.py`, or `src/models.py`, hashed together as a
  `policy_fingerprint`;
- the security becomes untradable, or the quote goes stale.

### Pre-execution checks

Immediately before any future order, `src/execution.preflight` requires all of:
the three switches open; no self-approval fields; prohibitions re-checked
(a decision that has become a sell, option, margin or transfer never reaches the
broker); a valid unexpired approval matching the fingerprint; the correct
agent-accessible account; a successful broker reconciliation; the amount within
the reconciled authorization; sufficient buying power; the security tradable;
a fresh quote (≤5 min equities, ≤60 s crypto); and price movement since pricing
within tolerance (**2% equities, 5% crypto**). Exceeding the price tolerance
returns `REAPPROVAL_REQUIRED`; a stale quote returns `REEVALUATION_REQUIRED`.

### An abandoned approval must not hold the month hostage

An approval that has been granted but not yet submitted **reserves its dollars
against the month's authorization**, because neither the broker nor the local
ledger can see it. That is correct — up to the moment the purchase is abandoned.
A decision left sitting in `APPROVED` after its preflight failed on price drift
goes on reserving dollars that nothing can ever spend, quietly shrinking the
month for a purchase that will never happen.

So an abandoned decision is **closed out**, by a human, through
`scripts/close_stale_decision.py`, on one of three checkable grounds: a failed
live preflight (`preflight`), an expired approval (`expired`), or explicit
written abandonment (`abandoned`). Time passing alone is not a ground. The
decision moves to `REEVALUATION_REQUIRED`, which releases the reservation and
from which there is **no path back to `APPROVED`** — the same asset at a new
price, or on a changed thesis, needs a new evaluation, a new `decision_id` and a
new human approval.

Nothing is deleted. The approval record, the decision log entry, the execution
record's own history and the audit trail all survive, and hand-editing any of
those files to free up authorization is tampering (`CLAUDE.md` §6).

The executor **re-checks permission, never the thesis.** It will not substitute a
different security. If circumstances changed materially it stops and asks for a
new evaluation.

### Slippage protection follows the real tool schemas

Inspected, not assumed:

- **`place_equity_order`** — `dollar_amount` is valid **only** with `type=market`,
  and fractional shares require `type=market` + `market_hours=regular_hours`.
  There is therefore **no broker-side limit price available** for a $25 fractional
  equity buy. All protection must be pre-trade, which is why the equity tolerance
  is the tighter of the two.
- **`place_crypto_order`** — `dollar_amount` works with **every** type including
  `limit`, and a limit buy's debit is "capped at dollar_amount (no collar
  buffer)", versus a "~1% buy collar" on a market buy. A crypto order is
  therefore specified as a marketable **limit**, not a market order.

Both tools deduplicate on `ref_id`. This project derives `ref_id`
deterministically from `decision_id` (UUIDv5), so a retry after an uncertain
submission re-sends the same key and the upstream collapses it.

---

## 12. Execution status

- `execution_mode = DRY_RUN`, `agent_enabled = false`, `live_trading = false`.
- **No code path submits, previews, reviews, or confirms a real order.**
- `src/execution.execute()` raises unconditionally. No `src/` module may import
  networking or subprocess machinery, and an AST test enforces it.
- Every logged decision carries `execution_status = "DRY_RUN_NOT_EXECUTED"`.
- The Robinhood MCP order tools — including `review_*` and `preview_*`, which are
  part of the order-submission flow — must not be called. Neither may any
  watchlist, scan, or alert **write** tool.
- Read-only Robinhood tools may be called freely.

---

## 13. Assumptions and conservative extensions

Decisions made where the policy was under-specified. They err toward restriction
and can be relaxed by the account owner.

1. **Penny stock** = share price below **$1.00** (hard rejection). Below **$5.00**,
   or market cap below **$300M**, produces a warning the thesis must address.
2. **Crypto-tracking ETPs** (IBIT, FBTC, …) are now *allowed* — direct crypto is
   an eligible asset class, so blocking a spot ETF would be inconsistent. They
   produce a **warning** about duplicated exposure to coins already held.
   *(This reverses the Version 1 assumption.)*
3. **Single-commodity and volatility ETPs** (GLD, SLV, USO, VXX …) are allowed but
   warn: they produce no cash flow, so the thesis must justify them as long-term
   holdings.
4. **Attention-driven crypto** (DOGE, SHIB, PEPE, BONK, WIF, TRUMP …) is allowed
   by the catalog but warns. Price per token is meaningless; the thesis must rest
   on durable relevance.
5. Purchase amounts must be **whole cents**.
6. Mutual funds, bonds/fixed income, and non-U.S.-listed securities remain out of
   scope — rejected as `ASSET_TYPE_NOT_ALLOWED`.
7. Acted-upon decision IDs are retained **across months and asset classes**, so a
   decision can never be replayed.
8. The Robinhood account number is treated as sensitive: resolved at evaluation
   time via `get_accounts`, never written to config, state, or logs.
9. The **crypto universe snapshot fails closed** — if it is missing or corrupt,
   no crypto proposal can be validated at all.
10. The **minimum dollar-based equity order is $1.00**. Partial deployment below
    that is rejected as `BELOW_BROKER_MINIMUM`.
11. **Mid-month is day 15.** The optionality gate applies to purchases before it.
12. A `research_package` component that could not be obtained must say so
    explicitly (`"NOT_AVAILABLE: <reason>"`). **Silence is not the same as
    unavailable**, and omitting a component is a violation.
