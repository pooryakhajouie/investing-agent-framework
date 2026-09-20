# DISCOVERY POLICY — Finding and Ranking Candidates (Version 3)

Companion to `INVESTMENT_POLICY.md`. That document says *what may be bought and
under what constraints*. This one says *how candidates are found, researched,
classified, and ranked* before the final $25 allocation decision.

Nothing here loosens a guardrail. Discovery produces **candidates**; only
`src/guardrails.py` decides whether a proposal is permitted.

---

## 1. The core question

> Where does this next $25 have the best expected long-term use, given the entire
> existing portfolio?

Discovery exists to make sure that question is asked against a **real** candidate
set — not against the first ticker that came to mind, and not only against
famous companies.

---

## 2. Discovery sources and provenance

Every candidate carries a `provenance` tag recording where it came from:

| Tag | Source |
|---|---|
| `CURRENT_HOLDING` | An equity or crypto position already owned |
| `MY_WATCHLIST:<name>` | An item on one of the owner's Robinhood watchlists |
| `MY_SCAN:<name>` | A result from one of the owner's saved Robinhood scans |
| `ROBINHOOD_POPULAR` | A Robinhood-curated list |
| `THEME_DISCOVERY:<theme>` | Found by researching a multi-year structural theme |
| `SUPPLY_CHAIN_DISCOVERY:<theme>` | A supplier, peer, or customer of an interesting company |
| `EXTERNAL_RESEARCH:<source>` | Found through read-only external research; name the source |
| `OPEN_DISCOVERY` | Found through other legitimate research |

A candidate may carry several tags. **Appearing in many sources is a reason to
research further. It is never a reason to buy.** Frequency is popularity, not
expected return.

### Weekly broad discovery — how new names enter the universe

The weekday evaluation sources candidates from holdings, watchlists, research
notes and prior reports. That set is **closed**: nothing new enters it unless the
owner adds it. A separate weekly pass (`prompts/weekly_discovery.md`, Saturday
09:00) exists to open it.

Its substrate is `get_popular_watchlists` plus `get_watchlist_items`, which reads
any Robinhood-curated list **by `list_id` without following it** — 28 lists and
roughly 9,000 instruments, with no write of any kind. `follow_watchlist` and
`create_scan` remain forbidden, so no saved scan is required. Read-only web
research supplements the sweep.

| Purpose | Lists |
|---|---|
| Genuinely new listings | `IPO Access`, `Newly listed crypto` |
| The ETF gap | `Sector ETFs`, `Growth & value ETFs`, `ETFs`, `Bond ETFs` |
| Theme and sector discovery | `Technology`, `Healthcare`, `Finance`, `Software`, `Energy`, `Pharma`, `Consumer goods`, `Real estate` |
| Event calendar | `Upcoming earnings` |
| Eligible crypto | `Tradable crypto`, cross-checked against `data/crypto_universe.json` |

> **A discovery pass proposes nothing.** It may add research candidates and it
> may not recommend, propose, approve or submit anything — not even a `WAIT`.
> The monthly authorization is decided only in the weekday evaluation, and
> `src/reporting.validate_discovery_report()` rejects a report that strays.

Candidates it surfaces are classified **`RESEARCH_INCOMPLETE`**: a mechanical
screen is not a research package, and a name coming out of a curated list has
earned a note, not a classification like `PROMISING`.

`Trending stocks`, `Daily movers` and `100 most popular` are ordered by
attention. Anything sourced there carries `ROBINHOOD_POPULAR` and a higher bar —
see the frequency rule above.

### Historical lessons — an input, never a permission

`research/lessons/` holds derived findings about **past** decisions, with a
regenerated `INDEX.md`. They exist so a recurring evaluation can learn from what
was actually done instead of re-deriving it.

A lesson has exactly the standing of a watchlist entry:

> **A lesson is advisory. It cannot authorise a purchase, satisfy a research
> requirement, or relax a guardrail.** `src/guardrails.py` rejects a decision
> that treats one otherwise, and rejects a research component answered by
> pointing at a lesson.

Two kinds, and the distinction is load-bearing: a `LESSON` rests only on what was
knowable at decision time and may be cited; an `OUTCOME_OBSERVATION` needs to
know what happened next and may never be cited as a reason. That type rule is
what keeps hindsight bias out. No lesson rests on fewer than three instances, and
all go stale after 90 days.

Where the data cannot support a pattern, the honest output is a refusal. With two
closing trades in three years, "sold too early" and "held too long" are not
assessable, and the detectors return `INSUFFICIENT_DATA` rather than guessing.

### Standing research notes — where prior work is kept

Per-candidate research lives in `research/<SYMBOL>.md`, one file per candidate,
with `research/INDEX.md` as a regenerated table of classifications and review
dates. Read the index first and the relevant notes second: re-deriving a thesis
that is already written down is waste, and the notes exist precisely so a
recurring evaluation does not have to.

What a note carries forward is the **thesis** — the valuation argument, the bull
and bear cases, the risks, the invalidation conditions, the dated evidence. What
it never carries forward is a **price**: quotes are re-read from the broker every
evaluation, and each note's Snapshot section records when its figures were taken.
A note is **stale** after 14 days, which means *revisit the thesis*, not discard
it.

> **A note's `classification` is a reasoning label, never authorization** — the
> same rule as a watchlist (below) and §7. A note reading `ADD_CANDIDATE`
> permits nothing.

Notes may be added and refreshed freely. Deleting one, or cutting one down, is
information loss and a scheduled run that does it is reported as a safety
failure.

### Watchlists are an interest signal, not authorization

Watchlist membership means the owner found something interesting once. It does
not mean the asset is good, still good, or good *at today's price*. Read the
lists, understand each list's apparent theme, note overlap with holdings, and
note items that look speculative, deteriorating, or extremely expensive.

**Never modify a watchlist or scan.** They are read-only inputs.
A cached, credential-free view lives in `state/watchlist_snapshot.json`.

---

## 3. Two-stage process

### Stage 1 — Broad discovery (cheap, wide)

Build a candidate pool of roughly **15–30 ideas**. Do not do expensive research
here. For each candidate record: symbol, asset class, provenance, a one-line
reason it is interesting, and an initial classification.

The pool must draw from **all** of: current holdings, watchlists, saved scans,
theme research, and supply-chain/peer discovery. A pool made only of mega-caps
means discovery did not happen.

### Stage 2 — Deep research (expensive, narrow)

Advance roughly **3–7 candidates** to full research (§5/§6). Deep research is
where SEC filings, multi-year financials, valuation history, earnings results,
and news actually get read.

The two stages exist to avoid both failure modes: analysing only famous
companies, and burning the whole evaluation on obviously weak ideas.

Numbers may vary when justified — say so in the log.

---

## 4. Theme discovery

Actively search for companies and assets positioned to benefit from important
multi-year structural trends. Illustrative, **not** a required allocation list:

data storage · memory and semiconductor infrastructure · AI infrastructure ·
data centers · networking · robotics · industrial automation · autonomous
systems · cybersecurity · cloud infrastructure · energy infrastructure · power
generation · grid modernization · advanced manufacturing · defense technology ·
biotechnology · healthcare innovation · financial technology · next-generation
computing · strategically important materials · emerging software platforms ·
promising crypto ecosystems and infrastructure

**Do not mechanically buy an industry because it sounds futuristic.** A theme is
a place to look, not a reason to own.

### Look through the whole value chain — "picks and shovels"

For any theme, consider the full ecosystem, not only the headline names.

*AI infrastructure* is not only chip designers: memory, storage, networking,
cooling, power delivery, data-center equipment, semiconductor manufacturing and
equipment, optical components, and supporting software all participate.

*Robotics* is not only robot manufacturers: sensors, actuators, machine vision,
motion control, industrial automation, semiconductors, and enabling software all
participate.

The less-obvious participant is often cheaper, less crowded, and equally exposed.

### Finding an opportunity before it is obvious

The goal is to investigate companies **before or during** the development of a
major long-term growth story — not to chase names that already became popular.

For any candidate theme, ask:

- What structural trend may be developing?
- Which companies are positioned to benefit?
- Which have improving fundamentals, not just improving narratives?
- Which have asymmetric long-term opportunity relative to their size?
- Might the market be underestimating future earnings or demand?
- Is the catalyst **temporary or structural**?
- Does the valuation still permit attractive future returns?

Search both well-known names **and** less-obvious participants in the same value
chain. Acknowledge plainly that this is inherently uncertain and often wrong.

---

## 4a. Theme precision — name the subtheme, not the category

Themes must be **specific**. A position addresses a *subtheme*; it does not fill
a broad category.

Every BUY carries a `theme` block with `primary` (the broad theme), `subtheme`
(the specific one), and `scope_caveat` (what it explicitly does **not** cover).
`src/models.py` holds the taxonomy and `src/guardrails.py` enforces that the
subtheme rolls up to the declared primary.

The distinction that matters most here:

| Asset | Correct subtheme | Broad theme | **Not** equivalent to |
|---|---|---|---|
| ISRG | `robotic_assisted_surgery` | `healthcare` | industrial robotics, humanoid robotics, warehouse automation, autonomous systems |
| SYM | `warehouse_automation` | `robotics_automation` | surgical robotics, healthcare |
| KOID | `humanoid_robotics` | `robotics_automation` | medical devices |
| MOG.A | `motion_control` | `robotics_automation` | surgical robotics |
| SNDK | `nand_flash_storage` | `storage_memory` | DRAM/HBM, HDD, semicap equipment |
| WDC | `hdd_nearline_storage` | `storage_memory` | NAND, DRAM |
| MU | `dram_and_hbm_memory` | `storage_memory` | NAND-only, storage systems |
| COHR | `optical_components` | `networking` | datacenter switching, semis broadly |
| GEV | `power_generation` | `energy_infrastructure` | grid construction, utilities |
| LLY | `pharmaceuticals` | `healthcare` | medical devices, surgical robotics |

> **Do not claim an investment fills an entire broad thematic gap when it
> addresses one specialized segment.** Buying Intuitive Surgical does not give
> the portfolio "robotics exposure" in the industrial-automation sense, and
> saying so overstates the diversification achieved. It gives exposure to
> robotic-assisted surgery, which is a healthcare subtheme.

---

## 4b. Minimum research completeness gate

A **new** position may not receive a final BUY classification unless the minimum
research package is complete, where the information is reasonably obtainable.

**Equity** — quote and valuation · recent quarterly results · multi-quarter
revenue/profit trends · balance-sheet condition · cash flow · earnings history ·
material company news · primary SEC/company filings · competitive position ·
major risks · upcoming events · bull case · bear case.

**ETF** — quote and valuation · index and methodology · expense ratio · holdings
concentration · overlap with existing holdings · liquidity and spread · fund size
and age · major risks · bull case · bear case.

**Crypto** — quote and market cap · supply and issuance · network usage and
adoption · ecosystem and development · security and decentralization ·
regulatory risk · competitive networks · long-term price and drawdown history ·
major risks · bull case · bear case.

Rules:

- Every component must be present. One that could not be obtained must say
  `"NOT_AVAILABLE: <reason>"`. **Silence is not the same as unavailable.**
- Some components **cannot be waived**: for equities those are quote/valuation,
  recent results, multi-quarter trends, balance sheet, major risks, and both the
  bull and bear cases. If any is unavailable, classify the candidate
  **`RESEARCH_INCOMPLETE`** and do not buy it.
- `RESEARCH_INCOMPLETE` is a valid classification and is **never** buyable.

> **Do not manufacture confidence from incomplete research.** An honest
> `RESEARCH_INCOMPLETE` plus a WAIT is a better outcome than a confident-sounding
> BUY resting on four data points.

---

## 4c. Primary sources

For deep-research finalists, prioritise in this order:

1. **SEC filings** (`get_sec_filing_index`, `get_sec_filing_facts`, `get_sec_filing`)
2. **Official investor-relations material / earnings releases** (`get_earnings_results`)
3. **Official financial data** (`get_financials`, `get_equity_fundamentals`)
4. **Reliable market data** (`get_equity_quotes`, `get_equity_historicals`)
5. **Reputable secondary reporting** (`get_equity_news`)

Analyst opinions may be *considered*. They may **never substitute for financial
analysis**. A new position requires at least **two** distinct primary or official
financial-data evidence entries, and the validator warns when analyst and news
entries outnumber primary ones.

Tag each evidence entry with a `source_type` — `SEC_FILING`, `COMPANY_IR`,
`EARNINGS_RELEASE`, `OFFICIAL_FINANCIALS`, `MARKET_DATA`, `ANALYST_OPINION`,
`SECONDARY_REPORTING` — so the mix is visible in the log rather than implied.

### Crypto has a different record, held to the same standard

A blockchain files nothing with the SEC and publishes no income statement.
Demanding the hierarchy above from one would make every cryptoasset permanently
unbuyable for a reason that says nothing about the asset — so crypto is judged
against the sources that actually exist for it, and just as strictly. Prioritise
in this order:

1. **The protocol's own record** — whitepaper, specification, improvement
   proposals (`source_type: PROTOCOL_DOCUMENTATION`)
2. **The project's own publications** — foundation or core-team announcements,
   audited disclosures (`PROJECT_OFFICIAL`)
3. **The chain itself** — supply, issuance to date, fees, active addresses,
   settlement volume (`ONCHAIN_DATA`)
4. **The reference implementation** — releases, changelogs, contributor activity
   (`CORE_DEV_REPOSITORY`)
5. **Executed governance decisions** (`GOVERNANCE_RECORD`)
6. **A regulator's own filings, orders and guidance** (`REGULATORY_PUBLICATION`)
7. **Reputable data providers**, named and dated (`RESEARCH_DATA_PROVIDER`)

A new crypto position requires at least **two** distinct evidence entries from
that list, and `src/guardrails.py` enforces it. Market data, commentary and
social sentiment (`MARKET_DATA`, `SECONDARY_REPORTING`, `ANALYST_OPINION`,
`SOCIAL_SENTIMENT`) never satisfy it, and the validator warns when they
outnumber the primary entries.

These are obtained with **read-only external research** — `WebSearch` and
`WebFetch`. Robinhood remains the source of truth for holdings, cost basis, pair
eligibility, tradability, orders and execution (`INVESTMENT_POLICY.md` §7a), and
nothing external overrides it there. Date every external figure and attribute it
to the source that published it.

---

## 5. Equity research checklist

For deep-research candidates, use the available Robinhood research tools and
other legitimate sources. Where data exists, evaluate:

**Business** — what the company actually does; sources of revenue; competitive
position; moat/differentiation; management execution where there is evidence;
industry structure.

**Financials** — revenue growth; gross profit; operating income; net income;
margins; cash generation; balance-sheet quality; dilution; multi-year *and*
recent-quarter trends.

**Valuation** — market capitalization; price relative to earnings, sales, or
another appropriate metric; valuation versus its own history; valuation versus
peers; **what growth is already priced in**.

**Catalysts** — earnings developments; product launches; industry changes;
contracts; capacity expansion; demand shifts; technology changes; regulatory
developments; competitive developments.

**Market data** — longer-term price trend; recent acceleration/deceleration;
volume; relative strength; proximity to highs/lows. Technical indicators are
*supporting information only*, never a thesis.

**Risks** — competitive threats; valuation risk; cyclicality; customer
concentration; financial weakness; dilution; regulation; technology disruption;
execution risk.

> **Do not confuse a good company with a good stock at any price.**

---

## 6. Crypto research checklist

Crypto is a legitimate but materially more volatile and uncertain asset class.
**Do not analyse it like a company.** Where reliable data exists, consider:

market capitalization · liquidity · adoption · network usage · ecosystem
development · developer activity (only with trustworthy data) · token economics
and issuance · supply dynamics · security · decentralization where relevant ·
regulatory risk · institutional adoption · real-world utility · competitive
networks · major protocol upgrades · concentration and governance risk ·
long-term price history · drawdown history · valuation-like metrics where
meaningful

**Never buy a cryptocurrency because:** it is trending; social media is excited;
the price recently rose; or it is "cheap per coin."

> **Price per token is meaningless.** A $0.08 coin is not cheaper than an $80,000
> coin. Only market capitalization relative to durable relevance means anything.

Prefer evidence of durable long-term relevance. State the volatility honestly:
drawdowns of 70–90% have historically occurred in many cryptoassets, including
the largest ones, and should be treated as a plausible risk rather than a tail
case. A 2-year horizon is short for this asset class.

### Completing this checklist requires sources outside the broker

Almost nothing on that list is available from Robinhood, because almost nothing
on it is broker data. Robinhood knows what this account holds, what it paid,
which pairs it may trade and what it has ordered — and that is the whole of its
authority. Network usage, issuance and supply, protocol development, ecosystem
activity, security and decentralization, regulatory developments, institutional
adoption and competing networks come from the sources ranked in §4c, obtained
with read-only external research.

> **"Robinhood does not expose it" is not a research finding.** It describes the
> tool surface, not whether the fact is obtainable. A component that a protocol
> publishes openly must be researched, not waived — and a candidate must not be
> classified `RESEARCH_INCOMPLETE` on that basis when reliable read-only
> external sources can reasonably supply it.

`RESEARCH_INCOMPLETE` is still the right label whenever the work genuinely could
not be done, and saying so is always better than manufacturing confidence
(§4b, §11). State the real obstacle: the source that could not be reached, the
figure no reputable provider publishes, read-only web research unavailable in
this run. `src/guardrails.py` rejects a waiver whose stated reason is the
broker's scope (`RESEARCH_GAP_NOT_JUSTIFIED`), and accepts one that names a
genuine obstacle.

---

## 7. Candidate classification

Reasoning labels applied to every candidate. **These are labels, not trading
signals.**

**General**
`HIGH_CONVICTION_CANDIDATE` · `PROMISING` · `WATCH` · `ATTRACTIVE_ON_PULLBACK` ·
`TOO_EXPENSIVE` · `TOO_EXTENDED` · `THESIS_UNCLEAR` · `FUNDAMENTALS_WEAK` ·
`SPECULATIVE` · `REJECTED` · `RESEARCH_INCOMPLETE`

**Existing holdings (additionally)**
`ADD_CANDIDATE` · `HOLD_NO_ADD` · `THESIS_WEAKENING` · `OVERCONCENTRATED`

**Sharp movers (§9)**
`EARLY_STRUCTURAL_OPPORTUNITY` · `FUNDAMENTALLY_SUPPORTED_MOMENTUM` ·
`SPECULATIVE_HYPE`

Only these classifications may accompany an actual BUY, and `src/guardrails.py`
enforces it:

`HIGH_CONVICTION_CANDIDATE` · `PROMISING` · `ADD_CANDIDATE` ·
`EARLY_STRUCTURAL_OPPORTUNITY` · `FUNDAMENTALLY_SUPPORTED_MOMENTUM`

A decision that labels a candidate `TOO_EXPENSIVE` and then buys it is rejected
(`CLASSIFICATION_CONTRADICTS_BUY`).

---

## 8. Long-term opportunity scorecard

For each deep-research candidate, assess these dimensions **separately** (1–5,
or `N/A` where data is unavailable):

`long_term_opportunity` · `business_or_network_quality` · `fundamental_strength` ·
`growth_runway` · `competitive_advantage` · `valuation` · `catalyst_quality` ·
`portfolio_fit` · `downside_risk` · `uncertainty` · `entry_attractiveness`

For crypto, adapt: read `business_or_network_quality` as network quality,
`fundamental_strength` as adoption and usage, `competitive_advantage` as
defensibility against competing networks.

> **These scores do not predict returns.** They are a structured way to notice
> that a candidate is strong on opportunity and weak on valuation. **The written
> reasoning matters more than the numbers.** Never sum the scores and buy the
> highest total.

---

## 9. Momentum versus long-term opportunity

Recent price appreciation can be useful evidence that the market is recognizing
something real. It is **not sufficient**.

For any candidate that has risen sharply, answer explicitly:

- Why did it rise?
- Did earnings or fundamentals actually change?
- Did the addressable market change?
- Is the catalyst durable or a one-off?
- **How much future success is already priced in?**
- Is the valuation still reasonable?
- Has risk/reward *worsened* precisely because the move already happened?
- Are there less-expensive companies exposed to the same trend?

Then classify: `EARLY_STRUCTURAL_OPPORTUNITY`,
`FUNDAMENTALLY_SUPPORTED_MOMENTUM`, `WATCH`, `ATTRACTIVE_ON_PULLBACK`,
`TOO_EXTENDED`, or `SPECULATIVE_HYPE`.

---

## 10. Final capital-allocation competition

After research, build one explicit comparison table, for example:

```
CAPITAL ALLOCATION CANDIDATES
1. Add to existing holding : XYZ   $25   ADD_CANDIDATE
2. New equity position     : ABC   $25   HIGH_CONVICTION_CANDIDATE
3. Add to existing crypto  : BTC   $25   HOLD_NO_ADD
4. New crypto position     : ...
5. WAIT                    :       $0
```

Compare them **directly** on: expected long-term return · quality · valuation ·
growth runway · portfolio fit · risk/reward · entry attractiveness.

The $25 goes to whichever option has the strongest combination. **WAIT competes
as a real alternative** and wins whenever nothing clears the bar.

Splitting the $25 across two candidates is allowed when both genuinely clear the
bar; it must not be used to avoid making a judgement.

---

## 10a. Portfolio sprawl

Before adding another ticker, assess honestly:

- how many positions already exist;
- how small the smallest ones are;
- whether another $5-$25 position is economically significant or just another line;
- how much the existing positions already overlap;
- whether the new ticker measurably improves expected return or diversification;
- whether the capital would do more in a higher-conviction existing holding.

No maximum ticker count is imposed. **A new ticker is not inherently preferable
to increasing a high-quality existing position**, and the burden of proof runs
the other way: the new name must earn its slot.

---

## 11. Research honesty (binding)

- Do not invent metrics. "Not available" is an acceptable, expected answer.
- Attribute each material number to the tool or source that produced it.
- Date every material claim; flag stale information as stale.
- Separate fact from inference explicitly.
- Surface conflicting evidence rather than the convenient half.
- Write a **bull case and a bear case** for every finalist.
- List the major unknowns.
- Report `LOW` confidence when confidence is low.

A discovery process that always produces a BUY is a broken discovery process.
So is one that always deploys the entire monthly budget at once.
