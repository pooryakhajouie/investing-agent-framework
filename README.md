# investing-agent-framework

An **experimental framework** for a long-horizon investing agent that researches
candidates, writes a reasoned recommendation, and then stops — because the step
that turns a recommendation into an order is reserved for a human being.

It is built around one idea: **an LLM's judgement is fallible, so the system is
designed to survive that.** Investment reasoning is done by a model; safety is
enforced by deterministic code that the model cannot talk its way past. When the
two disagree, the code wins.

> ### This is not financial advice
>
> This is research software, provided **as is and without warranty** (see
> `LICENSE`). It is not investment advice, not a recommendation to buy or sell
> anything, and it makes **no claim or promise of profitability**. It is not an
> autonomous trader and must not be used as one. Anyone who runs it against a
> real brokerage account is solely responsible for their own investment
> decisions and their own losses.
>
> The project is not affiliated with, endorsed by, or connected to any broker.

---

## What it does

Once a month, the agent decides how to allocate a small, fixed research budget
across equities, non-leveraged ETFs, and broker-supported crypto. Every
evaluation returns exactly one of three outcomes:

| Outcome | Legs | Meaning |
|---|---|---|
| `WAIT` | 0 | Nothing clears the bar. Preserve the budget as cash. |
| `SINGLE_BUY` | 1 | One purchase. |
| `SPLIT_BUY_PLAN` | 2–5 | Several independently justified purchases. |

**`WAIT` is a first-class answer, not a failure.** A month that ends with the
budget unspent is a success if nothing was worth buying. The framework
deliberately makes doing nothing easy and doing something hard.

### The $25 is an example, not a product

The examples throughout this repository use a **$25 monthly budget** and a
**24-month minimum holding period**. Both are configuration
(`monthly_budget_usd`, `min_investment_horizon_months` in `config.json`), chosen
to keep a live experiment small enough that its mistakes are affordable. They
are illustrative. They are not a guarantee, a recommended amount, or a
product tier — set whatever values you can afford to be wrong about.

---

## Architecture

The pipeline is deliberately long, and every arrow is a place where the process
can stop:

```
  research  →  recommendation  →  proposal  →  HUMAN APPROVAL  →  execution gates
                                                                        ↓
                          reconciliation  ←  one broker submission  ←  single-use ticket
                                   ↓
                                disarm
```

| Stage | What it produces | Who does it |
|---|---|---|
| **Research** | Standing per-candidate notes in `research/` | Model, read-only broker + web tools |
| **Recommendation** | A report under `reports/`, with no decision id | Model |
| **Proposal** | A `PROPOSED` decision with a real id and fingerprint | Code (`promotion`) |
| **Approval** | An approval record bound to that exact decision | **Human only** |
| **Execution gates** | Pass/fail against the live account | Code (`preflight`) |
| **Ticket** | One single-use, fingerprinted submission ticket | Code (`submission`) |
| **Submission** | Exactly one broker order call | **Human only** |
| **Reconciliation** | Fill state settled against the broker by `ref_id` | Code |
| **Disarm** | Execution switches closed again | Human |

### Four questions, four layers

These are kept strictly separate, because conflating them is how a safety system
quietly stops working:

| Layer | Question | Owner |
|---|---|---|
| Investment validity | Is this a sound purchase under the policy? | `src/guardrails.py` |
| Human approval | Did a human authorize *this exact thing*? | `src/approval.py` |
| Execution readiness | Could it execute right now? | `src/execution.py` |
| Broker authority | May a broker call happen at all? | `src/submission.py` |

A sound decision stays sound whether or not execution is armed — arming is an
execution fact, not an investment one. An earlier version checked the execution
switches inside `validate()`, which made the documented live path structurally
impossible to complete. That separation is the fix.

---

## The safety model

### Fail closed

Every ambiguous state resolves to "stop". Corrupt state files raise rather than
being read as empty. A missing crypto eligibility snapshot blocks crypto
entirely. An unreadable policy file fails rather than falling back to raw bytes.
A `SUBMISSION_UNCERTAIN` record is never retried automatically — a not-found
order is not proof that nothing happened.

### Exact-decision fingerprinting

An approval is bound by a SHA-256 over the decision's binding fields — id,
action, side, asset, asset class, asset type, position type, **amount**, and
month. Change any of them and the approval no longer applies.

The amount is bound **exactly, not as a ceiling**. Approving $10.00 and then
presenting a $5.00 decision fails as `DECISION_MODIFIED` — downward included.
To buy a different amount you need a new decision and a new approval.

### Policy fingerprinting

A second SHA-256 covers the policy surface: `config.json`,
`INVESTMENT_POLICY.md`, `src/guardrails.py`, `src/models.py`. Change the rules
and **every outstanding approval is void**. This is what forces the
arm-then-approve ordering: arming edits `config.json`, so approving first would
void the approval you just gave.

When that happens the record moves `APPROVED → REAPPROVAL_REQUIRED → APPROVED`
through the human approval script. There is no `APPROVED → APPROVED` edge, so a
superseded approval is recorded as invalidated rather than silently overwritten.

### Pre-submission checks

Before a ticket is minted, `preflight()` re-establishes everything from scratch:

- **Price drift** — the quote must be within **2%** (equities) or **5%**
  (crypto) of the price the decision was reasoned about. Beyond that, the
  decision is re-derived, never carried.
- **Quote freshness** — a stale quote blocks.
- **Tradability** — tradable, fractionally tradable, tradable in this account
  type, not halted.
- **Settled cash** — execution is cash-only. Buying power above settled cash is
  margin and is never used.
- **Monthly authorization** — reconciled against the broker, counting executed,
  pending, approved-but-unsubmitted, and proposed amounts together.
- **Replay protection** — a decision id that has been acted on cannot be reused.

### Human approval is not a formality

The model can produce a recommendation. It cannot produce an approval.
`create_approval()` is reachable only from `scripts/approve_decision.py`, which
refuses to run without a TTY and demands a verbatim challenge phrase
(`APPROVE IDXF $10.00`). The execution store refuses to mark any record
`APPROVED` without an approval id, so "a decision cannot approve itself" is a
property of the code rather than a convention.

**Be honest about what this is worth:** nothing in a local repository can
cryptographically prove a human typed at the keyboard. The protections are
layered — TTY check, challenge phrase, fingerprinting, audit log — and the one
that actually holds is that execution is disabled by three independent switches
that a human has to open deliberately.

### No submission adapter exists

`src/execution.py` performs **no I/O at all**. It is a pure function over a
broker snapshot that a caller gathers separately with read-only tools. The
shipped `Submitter` is `DisabledSubmitter` and refuses before any call. A test
walks the AST of every module in `src/` and fails the build on a network or
subprocess import. The single broker call is a deliberate human action against a
handoff file — which is what makes "the code cannot place an order" structural
rather than a promise.

---

## Repository layout

```
src/            the framework: guardrails, approval, execution, reporting, ...
scripts/        operator CLI — status, validate, approve, preflight, reconcile
prompts/        the evaluation, scheduled-run and discovery prompts
tests/          1,410 tests, standard library only
docs/           architecture and the live-purchase runbook
examples/       synthetic decision, approval, execution and broker-snapshot files
reports/        ONE synthetic digest and ONE synthetic audit record
research/       four synthetic research notes
scheduler/      launchd plists and installer for unattended report-only runs
data/           broker crypto-pair eligibility snapshot
```

**Everything in `reports/`, `research/` and `examples/` is synthetic.** See
`SECURITY.md`.

---

## Local development

Requires **Python 3.9+**. There are **no third-party dependencies** — standard
library only, and money is `decimal.Decimal` throughout, never `float`.

```bash
git clone <your fork>
cd investing-agent-framework

# Create your local configuration from the checked-in templates.
cp config.example.json config.json
cp .claude/settings.example.json .claude/settings.json

python3 -m unittest discover -s tests -t .
python3 scripts/check_status.py
```

`config.json` and `.claude/settings.json` are gitignored on purpose: they are
the live operating configuration of whoever runs this.

### Configuration

| Key | Default | Meaning |
|---|---|---|
| `monthly_budget_usd` | `"25.00"` | Maximum new purchases per calendar month |
| `min_investment_horizon_months` | `24` | Minimum stated holding period |
| `execution_mode` | `"DRY_RUN"` | `DRY_RUN` or `APPROVAL_REQUIRED` |
| `agent_enabled` | `false` | Execution switch 1 of 3 |
| `live_trading` | `false` | Execution switch 2 of 3 |
| `allow_crypto` | `true` | Whether crypto competes for the budget |
| `allow_selling`, `allow_options`, `allow_margin`, `allow_shorting`, `allow_leveraged_etfs`, `allow_inverse_etfs`, `allow_transfers` | `false` | Hard prohibitions |

**The three execution switches ship closed and should stay closed** unless you
are deliberately walking the live-purchase runbook in `docs/`. Setting
`agent_enabled` to `false` is the emergency stop: it halts every execution path
while leaving research and dry-run evaluation working.

### Broker access and credentials

Market and account data come from a **separately configured MCP server**. This
repository contains no credentials, reads no `.env`, and has no code path that
authenticates to anything. Configure the broker connection outside this
repository and keep it there.

**Never put a credential in this repository.** See `SECURITY.md`.

### Testing

```bash
python3 -m unittest discover -s tests -t .
```

All tests must pass before any change to `src/` is considered done. The suite is
hermetic: it uses temporary directories, injected clocks and synthetic fixtures,
so it depends on no live account, no network and no wall-clock date. Some tests
skip when no local portfolio history has been ingested — that is expected on a
fresh clone.

---

## Roadmap

Where this is going, roughly in order:

- **Broaden broker support.** The guardrail, approval and execution layers are
  broker-agnostic; the adapter boundary is not yet formalized.
- **Backtesting harness** for the guardrail set — replay historical candidate
  sets against the policy to find rules that are too strict or too loose.
- **Richer discovery** beyond watchlists and popular lists, with the
  attention-bias penalty kept explicit.
- **Better lesson derivation** — the decision-quality quadrants
  (`CONFIRMED` / `ACCEPTED_RISK` / `LUCK` / `CORRECTABLE`) exist, but the
  three-instance floor means real lessons accumulate slowly.
- **Portfolio-level optimization** across legs rather than per-leg justification.
- **A formal policy DSL**, so the investment policy is machine-checkable rather
  than prose plus hand-written validators.

Explicitly **not** on the roadmap: autonomous execution without human approval,
selling, options, margin, shorting, or leveraged and inverse products.

---

## License

Apache License 2.0 — see `LICENSE` and `NOTICE`.
