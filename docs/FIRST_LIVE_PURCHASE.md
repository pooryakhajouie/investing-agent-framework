# First Live Purchase — Exact Procedure

**Status: NOT ARMED.** `execution_mode = DRY_RUN`, `agent_enabled = false`,
`live_trading = false`, and all eleven order/review/preview/cancel tools denied
in `.claude/settings.json`. The Agentic account (`••••0002`) **does** now hold
settled cash, so the funding precondition is satisfied and the switches are the
only thing standing between this document and a real order. Treat it
accordingly.

This document is the whole procedure for **one** approval-required real purchase
of at most the configured monthly budget (`config.json` → `monthly_budget_usd`).
Read it end to end before arming anything.

> **Arm before you approve.** The policy fingerprint covers `config.json`, so
> arming after approving voids the approval you just gave. The step order below
> reflects that and is not negotiable. (Since the canonical-JSON change, a
> whitespace-only rewrite of `config.json` no longer voids approvals — but any
> change to a *value* still does.)

---

## 0. Who does what

| Step | Who |
|---|---|
| Evaluation, research, decision | Claude |
| **Approval** | **You, and only you** |
| Arming the switches | You |
| The single MCP order call | You, or a session you have explicitly permitted for that one call |
| Reconciliation and ledger commit | Claude or you |

> **Claude must never run `scripts/approve_decision.py`.** A conversational
> "yes", "go ahead", or "you have my permission to invest" is **not** approval.
> Approval is one command, naming one decision id, with a typed challenge phrase.

---

## 1. Preconditions

- [ ] The Agentic account (`••••0002`) has **settled cash ≥ the order amount**.
      It is a `limited_margin` account, so buying power can exceed cash —
      execution is cash-only and will refuse to touch the difference.
- [ ] `python3 -m unittest discover -s tests -t .` passes.
- [ ] `python3 scripts/check_status.py` shows the right month and remaining
      authorization.
- [ ] A **submission adapter decision** has been made: you are performing the MCP
      call yourself from the handoff file. There is deliberately no code that
      calls the broker.

## 2. Produce a decision

```bash
# in Claude Code, with the robinhood-trading MCP server connected
Follow prompts/evaluate_market.md and run one dry-run market evaluation now.
```

Outcome is `BUY` or `WAIT`. A `WAIT` ends the process — that is a success, not a
failure. A `BUY` is logged to `logs/decisions.jsonl` in state `PROPOSED`.

## 3. Review the proposal

```bash
python3 scripts/show_pending_decision.py
```

Check the asset, the amount, the classification, and the thesis. Nothing has been
approved. Nothing can execute.

## 4. Arm the three switches — BEFORE approving

Edit `config.json`:

```json
"execution_mode": "APPROVAL_REQUIRED",
"agent_enabled": true,
"live_trading": true
```

Then **verify it took**, because a typo here fails closed and a silent
half-armed state is the worst outcome:

```bash
python3 -c "import sys;sys.path.insert(0,'.');from src.state import load_config;c=load_config();print(c.execution_mode,c.agent_enabled,c.live_trading)"
python3 -c "import sys;sys.path.insert(0,'.');from src.approval import policy_fingerprint;print(policy_fingerprint())"
python3 scripts/check_status.py
```

Expect `APPROVAL_REQUIRED True True`. An unrecognized `execution_mode` — even a
one-character typo like `PPROVAL_REQUIRED` — raises `ConfigError` and makes the
whole repository unreadable rather than defaulting to anything. **Record the
policy fingerprint**; it must be unchanged when you approve and when you
preflight.

Arming does **not** invalidate a validated decision. `validate()` judges
investment quality only; the switches are enforced by
`src.execution.execution_gate_blockers()`. (This was not always true — see
§"Why the order matters" below.)

### Expose exactly one order tool, for the approved asset class only

In `.claude/settings.json`, remove **one** deny rule:

| Approved asset class | Remove only | Leave denied |
|---|---|---|
| Equity / ETF | `mcp__robinhood-trading__place_equity_order` | `place_crypto_order` and all nine others |
| Crypto | `mcp__robinhood-trading__place_crypto_order` | `place_equity_order` and all nine others |

Never expose both. An equity purchase has no need of crypto ordering, and the
narrowest exposure that can complete the trade is the correct one. Restore the
rule in §11 regardless of how the attempt ends.

## 5. Approve exactly one decision

```bash
python3 scripts/approve_decision.py <decision_id>
```

You will see the asset, asset class, dollar amount, month, remaining
authorization, decision fingerprint, and policy fingerprint. Confirm the policy
fingerprint matches the one you recorded in §4. Type the challenge phrase
verbatim, e.g. `APPROVE SNDK $10.00`.

This authorizes **only**: that decision id, that asset, that asset class, that
action, **exactly** that amount, in that calendar month, for **24 hours**.

> **The amount is bound exactly, not as a ceiling.** `proposed_amount_usd` is one
> of the approval's fingerprint binding fields, so changing it at all produces a
> different decision that this approval does not cover — **downward included**.
> Approving $10.00 and then presenting a $5.00 decision fails with
> `DECISION_MODIFIED`, not "within the approved maximum". Verified in
> `tests/test_execution.LivePathRegressionTests`.
>
> The `max_amount_usd` ceiling and its `AMOUNT_EXCEEDS_APPROVAL` code still
> exist, and an over-sized decision trips both. But the ceiling is belt and
> braces: the fingerprint has already refused any change, in either direction.
>
> The practical consequence is the one that matters at the keyboard: if you want
> to buy a different amount, you need a **new decision and a new approval**. You
> cannot approve $25 and then decide to spend $10 of it. What the approval *does*
> bound loosely is the **fill** — a market order that fills slightly under the
> dollar amount is fine, because the decision payload never changed.

Verify:

```bash
python3 scripts/show_approval.py <decision_id>
```

If it reports `POLICY_CHANGED`, something edited a policy-surface file between
arming and approving. Find out what before re-approving.

## 6. Gather a read-only broker snapshot

In Claude Code, using **read-only** tools only, write `broker.json`:

```json
{
  "as_of": "2026-09-05T18:00:00Z",
  "account_is_agentic": true,
  "account_masked": "••••0002",
  "account_type": "limited_margin",
  "cash_usd": "25.00",
  "unsettled_funds_usd": "0.00",
  "buying_power_usd": "25.00",
  "equity_orders": { "...get_equity_orders for this month..." },
  "crypto_orders": { "...get_crypto_orders for this month..." },
  "quote_price_usd": "366.67",
  "quote_timestamp": "2026-09-05T17:59:40Z",
  "tradable": true,
  "fractional_tradable": true,
  "account_type_tradable": true
}
```

Sources: `get_accounts`, `get_portfolio`, `get_equity_orders`,
`get_crypto_orders`, `get_equity_quotes` / `get_crypto_quotes`,
`get_equity_tradability` / `get_currency_pairs`. **Any missing field fails
closed** — that is the point.

## 7. Preflight and mint the ticket

```bash
python3 scripts/prepare_submission.py <decision_id> --snapshot broker.json
```

This re-checks everything: the three switches, prohibitions, the approval and its
fingerprint, the reconciled monthly authorization, **settled cash**, tradability,
quote freshness, and price movement since the decision was priced (2% equities /
5% crypto). If it passes, it mints a **single-use submission ticket** valid for
**5 minutes** and prints the exact order parameters.

If it fails, stop. Fix the cause or start again. Do not edit the ticket.

## 8. Write-ahead and hand off

```bash
python3 scripts/submit_approved.py <decision_id>
```

In order: the ticket is marked consumed and persisted; `SUBMIT_INTENT` is written
and **fsynced**; the record moves to `SUBMISSION_UNCERTAIN`; only then does the
handoff file `state/pending_submission.json` appear.

**From this moment, assume an order may exist.**

## 9. Make exactly ONE MCP call

Open `state/pending_submission.json` and call the named tool with those parameters
**verbatim**, including `ref_id`.

| Asset class | Tool | Form |
|---|---|---|
| Equity | `place_equity_order` | `type=market`, `dollar_amount`, `market_hours=regular_hours`, `gfd`, `ref_id` |
| Crypto | `place_crypto_order` | `type=limit`, `dollar_amount`, `limit_price`, `gtc`, `ref_id` |

Equities have no limit price available for a dollar-based fractional order — that
is a schema constraint, which is why the pre-trade price tolerance is tighter.

**Call it once.** If it errors, times out, or you are unsure — **go to step 11.**
Do not call it again.

Save the response as `resp.json`.

## 10. Record, then reconcile

```bash
python3 scripts/record_submission.py <decision_id> --response resp.json
```

Moves `SUBMISSION_UNCERTAIN → SUBMITTED`. It records that an order *exists*; it
does **not** conclude it filled.

Then fetch `get_equity_orders` / `get_crypto_orders` into `orders.json`:

```bash
python3 scripts/reconcile_submission.py <decision_id> --orders orders.json --commit
```

Matches by `ref_id`, settles to `FILLED` / `PARTIALLY_FILLED` / `REJECTED` /
`CANCELLED`, and with `--commit` records the **actually filled** amount against
the monthly authorization.

## 11. Disarm — immediately, and whatever happened

Do this the moment the order is placed or abandoned. Do not leave the repository
armed while you investigate, reconcile, or think. An armed repository with
settled cash and an exposed order tool is the single most dangerous state this
system has, and it earns nothing while you are not actively submitting.

```bash
python3 - <<'EOF'
import json
c = json.load(open("config.json"))
c["execution_mode"] = "DRY_RUN"; c["agent_enabled"] = False; c["live_trading"] = False
json.dump(c, open("config.json", "w"), indent=2)
EOF
```

Restore the **one** deny rule you removed in §4. Then confirm all three:

```bash
grep -E "execution_mode|agent_enabled|live_trading" config.json   # DRY_RUN / false / false
grep -c '"mcp__robinhood-trading__' .claude/settings.json          # 26
python3 scripts/check_status.py                                    # LIVE TRADING: DISABLED
```

Reconciliation (§10) works fine disarmed — it only reads. Disarm first, then
reconcile.

---

## Why the order matters — a contradiction that used to make this impossible

Until 2026-09-11, `src/guardrails.validate()` emitted
`LIVE_TRADING_NOT_PERMITTED` whenever `config.live_trading` was true. Because
the policy fingerprint forces arm-then-approve, and `approve_decision.py`
re-validates through `validate()`, arming was *guaranteed* to invalidate the
decision you were about to approve. The documented sequence could not complete,
in either order:

- approve first → arming changes the fingerprint → `POLICY_CHANGED`, approval void;
- arm first → `validate()` fails → `approve_decision.py` refuses with
  `LIVE_TRADING_NOT_PERMITTED`.

The fix separated the four questions this system actually asks — investment
validity, human approval, execution readiness, broker authority — so that
arming, which is an execution fact, stopped being treated as an investment one.
See README §4b. No guardrail was removed: the switches are enforced in
`execution_gate_blockers()`, which every path to a submission ticket runs
through.

---

## Emergency recovery

### Kill everything, now

```bash
python3 -c "import json;c=json.load(open('config.json'));c.update(agent_enabled=False,live_trading=False,execution_mode='DRY_RUN');json.dump(c,open('config.json','w'),indent=2)"
```

Any **one** of those three stops execution. Editing `config.json` also changes the
policy fingerprint, which voids every outstanding approval and every ticket.

### "I don't know whether the order went through"

**Do not resubmit.** The record is `SUBMISSION_UNCERTAIN` and both
`execute_approved.py` and `submit_once` refuse to act on it.

1. Read `state/pending_submission.json` for the `ref_id`.
2. Fetch `get_equity_orders` / `get_crypto_orders` into `orders.json`.
3. `python3 scripts/reconcile_submission.py <decision_id> --orders orders.json`

- **Found** → it settles the state. Done.
- **Not found** → it stays `SUBMISSION_UNCERTAIN` and says so. Not-found is *not*
  proof nothing happened; the broker may not have indexed it. Wait and re-run.
  Only after repeated not-found results, and a human decision, mint a new ticket.
  The `ref_id` is derived from the `decision_id`, so even a duplicate submission
  would be deduplicated by Robinhood rather than creating a second order.

### "The order filled but the ledger wasn't updated"

Re-run `reconcile_submission.py ... --commit`. `commit_purchase` refuses a
duplicate `decision_id`, so it cannot double-count.

### "I approved the wrong thing"

Delete the entry from `state/approvals.json`, or wait 24 hours. Any edit to
`config.json`, `INVESTMENT_POLICY.md`, `src/guardrails.py` or `src/models.py`
voids all approvals immediately via the policy fingerprint.

### "A ticket looks wrong"

Tickets are fingerprinted and single-use. A tampered ticket fails to load; a
consumed one is refused. Mint a fresh one after a fresh preflight.

---

## What still blocks the first live purchase

1. ~~The account holds $0.00.~~ **No longer true.** The Agentic account holds
   settled cash and this precondition is satisfied. Verify the amount at the
   time with `get_portfolio` plus `get_accounts` (`unsettled_funds`) — execution
   is cash-only and will not touch the difference between cash and buying power.
2. **The switches are closed** and all eleven order tools are denied.
3. **You have approved nothing.**
4. **No automated submission adapter exists**, by design. The single MCP call is
   a deliberate human action against the handoff file.

Items 2–4 are the live ones. Item 2 is the only one this document asks you to
change, and §11 changes it back.
