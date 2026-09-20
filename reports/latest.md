# Scheduled Evaluation — 2026-01-16 09:30 America/Chicago

> **ACTION: BUY $10.00 IDXF**
>
> - **Remaining monthly authorization:** $25.00 → $15.00 if this is approved and filled
> - **Confidence:** MEDIUM
> - **Why:** IDXF is the only candidate whose valuation and cost structure both
>   clear the bar at today's price, and adding to it beats opening a new line.
> - **Human approval required:** YES — nothing here is approved or submitted.

*Every figure in this file is synthetic. It is a worked example of the digest
contract, not a record of any real portfolio, account or transaction.*

## Status

**REPORT ONLY.** Verified before any analysis: `execution_mode=DRY_RUN`,
`agent_enabled=false`, `live_trading=false`. Nothing was approved, enabled,
promoted or submitted; the example agentic account has never placed an order.

| | |
|---|---|
| Month | 2026-01 — **day 16 of 31**, **11 equity sessions left** including today |
| Authorized / executed / pending / approved | $25.00 / $0.00 / $0.00 / $0.00 |
| **Remaining monthly authorization** | **$25.00** → **$15.00** if this leg fills |

Broker-reconciled: both order tools return `[]` for the month. No prior
recommendation was promoted or approved, so this run derives its answer rather
than carrying one forward.

## What changed

1. **IDXF's expense ratio fell** at the fund's annual reset, improving the
   long-run compounding arithmetic without changing what the fund holds. This is
   a structural improvement rather than a price move.
2. **EXMP reported in line** with its prior guidance. Nothing in the release
   changes the thesis in `research/EXMP.md`, and the position stays
   `HOLD_NO_ADD` — an unchanged thesis is not a reason to add.
3. **NEWC remains unheld and under-researched.** Two components of its research
   package are still unassembled, so it cannot clear the bar this month whatever
   its price does.
4. **ZZC-USD rallied on sentiment** with no protocol, adoption or issuance
   development behind it. Price appreciation on its own is not a thesis.

## Five-way capital-use comparison

| Use of the $25 | Candidate | Label | Verdict |
|---|---|---|---|
| **Existing equity/ETF** | **IDXF** | `ADD_CANDIDATE` | **SELECTED — $10.00.** Lowest-cost broad exposure already held; the expense reduction improves it further. |
| **New equity/ETF** | NEWC | `WATCH` | Rejected — research package incomplete, and it must beat adding to IDXF to justify a 21st line. It does not. |
| **Existing crypto** | ZZC-USD | `HOLD_NO_ADD` | Rejected — the move is sentiment, the sleeve is already at its concentration limit, and 70–90% drawdowns are the norm for the asset class. |
| **New crypto** | — | `RESEARCH_INCOMPLETE` | Rejected — no eligible pair clears the tradability allow-list with a complete research package. |
| **WAIT — preserve as cash** | — | — | **Partially chosen — $15.00 retained.** |

## Decision

DECISION: SINGLE_BUY

The question is not whether IDXF is exciting, but whether $10 has a better
long-term use elsewhere this month. It does not. EXMP's thesis is intact but
priced for it; NEWC cannot be researched to completion in time; ZZC-USD would
add concentration to the most volatile sleeve on the strength of a sentiment
move. Adding to the lowest-cost diversified holding already owned is the
unglamorous answer and the correct one.

## Proposed allocation

| Asset | Class | Position | Amount |
|---|---|---|---:|
| **IDXF** | `ETF` | `EXISTING_POSITION` | **$10.00** |

**Combined $10.00. Remaining authorization after this plan: $15.00.**
Cash-funded from settled cash; no margin component.

**Why $10 and not $25:** nothing about this month's information argues for
concentrating the whole authorization on a single day. Retaining $15 preserves
the ability to act if NEWC's research completes or if a better entry appears
before month end. **$5 was considered and rejected** as too small to matter
against the horizon.

## Confidence

**MEDIUM.** The thesis is simple and the instrument is diversified, which limits
how wrong a single judgement can be. What holds confidence down is that the
expense-ratio improvement is small in absolute terms, and that the decision is
partly a statement about the *absence* of better options rather than positive
conviction about this one.

**Recorded before the outcome, symmetrically.** If IDXF is materially lower in
three months, this is `ACCEPTED_RISK` — sound process, bad outcome. Materially
higher is not vindication either: a broad index moving with its market says
nothing about the quality of this decision.

## Watching

- **Whether NEWC's two missing research components become obtainable.** Until
  they do, it cannot be bought at any price.
- **Any change to IDXF's holdings methodology**, as distinct from its price.
- **The crypto sleeve's share of total portfolio value**, which is at the
  concentration limit and constrains any future ZZC-USD add.
- **EXMP's cash-conversion trend**, the one number that would move it off
  `HOLD_NO_ADD`.

## What would change this

- **Drop this recommendation** if IDXF's cost advantage disappears, or if a
  disclosed methodology change alters what the fund actually holds.
- **Re-derive rather than promote** if IDXF moves beyond the 2% equity slippage
  tolerance from the priced entry.
- **Reconsider NEWC** only once its research package is complete — a completed
  package is a precondition, never itself a reason to buy.

**Dated events:** January's equity edge is the close of the month's last trading
session. No dated event in this evaluation falls past that edge, so no part of
the authorization is being allowed to expire by design.

## Detail and evidence

- **Audit record:** `reports/2026-01-16_0930.md` — holdings and watchlist
  tables, the full five-way comparison, dated events, and every rejection.
- **Standing research:** `research/` — `IDXF.md`, `EXMP.md`, `NEWC.md`,
  `ZZC-USD.md`.
- **Machine state:** `state/last_evaluation.json`, `logs/decisions.jsonl`

*This recommendation is not permission. The leg requires its own separate human
approval and its own submission ticket. Execution remains disabled at all three
switches and this run did not touch them.*
