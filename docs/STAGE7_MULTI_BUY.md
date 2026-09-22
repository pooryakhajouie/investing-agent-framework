# Stage 7 — Multi-buy allocation plans

**Execution remains disabled.** Stage 7 changes what an evaluation may
*recommend*. It changes nothing about what may be *executed*, and it adds no
order path.

---

## The three outcomes

An evaluation now returns a **plan**:

| `plan_type` | Legs | Meaning |
|---|---|---|
| `WAIT` | 0 | No purchase. Still carries a full WAIT decision, so the existing WAIT rules apply unchanged. |
| `SINGLE_BUY` | 1 | One purchase. |
| `SPLIT_BUY_PLAN` | 2–5 | Several purchases, combined total within the remaining authorization. |

**Splitting is never required.** One high-conviction purchase, or `WAIT`,
remains a first-class and frequently better answer. A `SPLIT_BUY_PLAN` must
argue for itself against concentrating the same dollars into the single best
candidate.

## The design rule that keeps this safe

> **A plan is a container. Every leg is an ordinary decision.**

Each BUY leg is a complete decision payload that goes through the **unchanged**
single-decision validator in `src/guardrails.py`, and keeps its own
`decision_id`. Downstream, each leg independently earns:

- its own **fingerprint** (`src/approval.binding_view`);
- its own **approval** — one `scripts/approve_decision.py <decision_id>` per leg,
  with its own typed challenge phrase and its own 24-hour expiry;
- its own **submission ticket** (`src/submission.py`, single-use);
- its own **reconciliation** by `ref_id`;
- its own **idempotency** protection, including replay rejection.

So the plan is a *presentation and arithmetic* layer. Approval and execution stay
exactly as granular, and exactly as auditable, as they were in Stage 5. There is
no "approve the plan" command, by design.

## What the plan layer adds

Cross-leg arithmetic a single-decision validator structurally cannot do.

### 1. Legs are validated cumulatively

Leg *N* is checked against a budget state that already reflects legs *0..N-1*.
Three $10 legs cannot each pass on the grounds that $25 was available:

```
$10 + $10 + $10  ->  EXCEEDS_REMAINING_BUDGET (leg 2)
                     EXCEEDS_CUMULATIVE_MONTHLY_BUDGET (leg 2)
                     PLAN_EXCEEDS_REMAINING_BUDGET (plan)
```

This also makes the **mid-month optionality gate** fire on whichever leg actually
crosses 50% of the authorization, which is the correct behaviour.

### 2. The four-way ceiling

The broker knows about **filled** and **pending** orders. It does not know about
a sibling leg a human has already **approved but not yet submitted**, and it
knows nothing about legs still merely **proposed**. In a multi-leg month those
are exactly the amounts that could silently push the total past $25.

```
executed + pending + approved-but-unsubmitted + proposed  <=  $25.00
```

`src.allocation.combined_exposure()` computes it;
`assert_plan_within_authorization()` enforces it.

`src.execution.preflight()` gained one optional parameter,
`sibling_reservations_usd`, defaulting to `ZERO` — so single-leg behaviour is
byte-identical to Stage 5. Compute it with
`src.allocation.sibling_reservations_usd()`; `execution.py` still performs no
I/O of its own. Already-submitted siblings are excluded, because the broker
reports those itself and counting them twice would understate the authorization.

### 2a. When a leg stops reserving

A leg reserves its dollars for exactly as long as it could still be submitted on
the strength of an approval it has, or one a human could still give it. Two
groups of states end that, and one deliberately does not:

| Execution state | Reserves? | Why |
|---|---|---|
| `PROPOSED` | yes | it is still a live candidate for this month's budget |
| `APPROVED` | **yes** | approved and unsubmitted is the whole reason this exists |
| `PRE_EXECUTION_VALIDATED` | yes | a ticket is minted; submission is imminent |
| `REAPPROVAL_REQUIRED` | **yes** | the *same* leg, same price, same amount, can legally become `APPROVED` again — releasing it would let the month be double-spent |
| `REEVALUATION_REQUIRED` | **no** | the priced premise is gone; this leg can never be submitted, so holding dollars against it only shrinks the month for no one |
| `SUBMITTED` / `SUBMISSION_UNCERTAIN` / `PARTIALLY_FILLED` / `FILLED` | no | the broker reports these itself |
| `REJECTED` / `EXPIRED` / `CANCELLED` / `EXECUTION_FAILED` | no | terminal |

`src.execution.RELEASED_STATES` is the second group: every terminal state plus
`REEVALUATION_REQUIRED`. `sibling_reservations_usd()` skips it.

**This is not automatic, and it must not be.** A leg does not release its
dollars because time passed or because a check happened to fail; it releases
them when a human closes it out through `scripts/close_stale_decision.py` on a
checkable ground. See `CLAUDE.md` §11a, Stage 8.

Closing one leg leaves every sibling exactly where it was: reservations are
computed per decision id, there is no plan-level state, and the released dollars
return to the *month's* pool rather than to the plan.

### 3. Cross-leg reasoning

Each leg of a split must carry an `allocation_rationale`:

| Key | Must answer |
|---|---|
| `why_better_than_other_legs` | Why is this money better here than on the siblings? |
| `why_this_amount` | Why this size, rather than more or less? |
| `legs_compared_against` | Which sibling legs, **named by ticker** |

The validator checks that `legs_compared_against` actually names a sibling in
this plan. A leg that cannot say what it beat has not earned its slice.

### 4. Sprawl, at the plan level

A split creates several positions at once — exactly when sprawl is most likely
and least noticed. A `SPLIT_BUY_PLAN` must carry:

- `plan_sprawl_assessment` — `positions_after_this_plan`,
  `why_not_concentrate_into_one`, `smallest_leg_significance`;
- `split_rationale` — a substantive argument (≥120 chars) that splitting beats
  concentrating.

### 5. Other cross-leg rules

- Every leg needs a **distinct** `decision_id` (`DUPLICATE_LEG_DECISION_ID`).
- Two legs may not buy the **same asset** (`DUPLICATE_LEG_ASSET`) — that is one
  position split across two approvals, not an allocation.
- A plan may not carry `approved`, `approval`, `approved_by`, `user_approved` or
  `execution_state` (`SELF_APPROVAL_ATTEMPTED`). A recommendation is never
  permission.

## Usage

```bash
python3 scripts/validate_plan.py plan.json          # validate
python3 scripts/validate_plan.py plan.json --log    # validate and log each leg
python3 scripts/validate_plan.py plan.json --json   # machine-readable
```

`--log` appends each leg to `logs/decisions.jsonl` as an ordinary decision,
computing each leg's budget arithmetic against what the earlier legs consumed. It
changes no budget state and executes nothing.

## Approving a plan

There is no plan-level approval. Approve **each leg separately**:

```bash
python3 scripts/show_pending_decision.py
python3 scripts/approve_decision.py <leg-0-decision-id>   # type the challenge phrase
python3 scripts/approve_decision.py <leg-1-decision-id>   # again, for the second leg
```

Each approval binds one decision id, one asset, one asset class, one action, the
**exact** amount, one calendar month, for 24 hours. Approving leg 0 authorizes
nothing about leg 1.

> **Exact, not a ceiling.** `proposed_amount_usd` is a fingerprint binding
> field, so re-pricing a leg in either direction — $10.00 down to $5.00 included
> — produces a decision the approval does not cover and fails as
> `DECISION_MODIFIED`. Re-sizing a leg means a new decision and a new approval
> for that leg. See `docs/FIRST_LIVE_PURCHASE.md` §5.

> Claude must never run `approve_decision.py`. That rule is unchanged, and it
> applies to every leg of every plan.
