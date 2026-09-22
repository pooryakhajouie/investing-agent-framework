"""Approval-required execution: state machine, gates, and preflight.

**Nothing in this module submits an order.** :func:`execute` raises
unconditionally, and no Robinhood MCP tool is imported, referenced, or called
anywhere in this file. What it does is decide, deterministically, whether an
order *would* be permitted — and build the exact parameters that would be sent.

Design choice worth stating plainly: **this module performs no I/O against the
broker.** It is a pure function over a :class:`BrokerSnapshot` that the calling
script gathers with read-only tools. That makes "the executor cannot place an
order" a structural property rather than a promise.

Three independent switches must ALL be open before execution is even considered:

    execution_mode == APPROVAL_REQUIRED   (config.json)
    agent_enabled  == true                (kill switch)
    live_trading   == true                (legacy interlock, retained)

Today all three are closed. Opening one, or two, changes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .approval import (
    ApprovalError,
    ApprovalRecord,
    broker_ref_id,
    fingerprint,
    verify_approval,
)
from .models import (
    ASSET_CLASS_CRYPTO,
    ASSET_CRYPTO,
    CENTS,
    MIN_EQUITY_ORDER_USD,
    ZERO,
    MoneyError,
    money_str,
    parse_money,
)
from .guardrails import validate as validate_decision
from .reconciliation import Reconciliation, ReconciliationError, reconcile
from .state import (
    EXECUTION_MODE_APPROVAL_REQUIRED,
    EXECUTION_MODE_AUTONOMOUS,
    EXECUTION_MODE_DRY_RUN,
    BudgetState,
    Config,
    CryptoUniverse,
    current_month,
    utc_now,
)

# --------------------------------------------------------------------------
# Freshness and slippage thresholds
#
# These follow from the ACTUAL tool schemas, not from a trading preference:
#
#   place_equity_order  -- `dollar_amount` is valid ONLY with type=market, and
#     fractional shares require type=market + market_hours=regular_hours. There
#     is therefore NO broker-side limit price available for a $25 fractional
#     equity buy. All price protection must be pre-trade, which is why the
#     equity tolerance is tight.
#
#   place_crypto_order  -- `dollar_amount` works with every type INCLUDING limit,
#     and for a limit buy "a buy's debit is capped at dollar_amount (no collar
#     buffer)". A market crypto buy instead carries a "~1% buy collar". A limit
#     order is therefore strictly better and is what this design specifies.
# --------------------------------------------------------------------------

# Account types where `buying_power` can legitimately exceed settled cash. The
# agentic account is `limited_margin`, so this is a live concern, not theory.
MARGIN_CAPABLE_ACCOUNT_TYPES = frozenset({"margin", "limited_margin"})

MAX_EQUITY_QUOTE_AGE_SECONDS = 300      # 5 minutes
MAX_CRYPTO_QUOTE_AGE_SECONDS = 60       # crypto moves faster and trades 24/7
MAX_EQUITY_SLIPPAGE_PCT = Decimal("2.0")
MAX_CRYPTO_SLIPPAGE_PCT = Decimal("5.0")
# Buffer added to the crypto limit price so a marketable limit can actually fill
# while still capping the debit at dollar_amount.
CRYPTO_LIMIT_BUFFER_PCT = Decimal("0.5")


# Violations that can NEVER become acceptable between evaluation and execution.
# The executor re-checks these immediately before submitting. It deliberately
# does NOT re-run the investment thesis (see INVESTMENT_POLICY section 7): the
# question here is "is this action still permitted?", not "is it still the best
# idea?". A decision that has become a sell, an option, or a margin trade since
# it was logged must never reach the broker.
PROHIBITION_CODES = frozenset({
    "SELL_FORBIDDEN", "SHORT_FORBIDDEN", "OPTIONS_FORBIDDEN", "MARGIN_FORBIDDEN",
    "TRANSFER_FORBIDDEN", "SETTINGS_CHANGE_FORBIDDEN", "FUTURES_FORBIDDEN",
    "LEVERAGED_PRODUCT_FORBIDDEN", "INVERSE_PRODUCT_FORBIDDEN", "OTC_FORBIDDEN",
    "PENNY_STOCK_FORBIDDEN", "ASSET_TYPE_NOT_ALLOWED", "CRYPTO_FORBIDDEN",
    "NOT_EXPLICIT_BUY", "SELF_APPROVAL_ATTEMPTED", "INVALID_DECISION",
    "CRYPTO_PAIR_HALTED", "CRYPTO_PAIR_NOT_SUPPORTED", "CRYPTO_PAIR_NOT_TRADABLE",
    "MALFORMED_CRYPTO_PAIR", "CRYPTO_UNIVERSE_UNAVAILABLE", "MISSING_TICKER",
    "MALFORMED_TICKER", "ZERO_AMOUNT", "NEGATIVE_AMOUNT", "SUB_CENT_AMOUNT",
    "ASSET_CLASS_MISMATCH", "BELOW_BROKER_MINIMUM", "CRYPTO_BELOW_MIN_ORDER_SIZE",
    "CRYPTO_FIELD_MISMATCH", "HORIZON_TOO_SHORT",
})


class ExecutionDisabled(RuntimeError):
    """Raised by :func:`execute`. Always, in this version."""


class InvalidTransition(RuntimeError):
    """Raised when a state change is not permitted by the state machine."""


# --------------------------------------------------------------------------
# State machine
# --------------------------------------------------------------------------


class ExecutionState:
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    PRE_EXECUTION_VALIDATED = "PRE_EXECUTION_VALIDATED"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_UNCERTAIN = "SUBMISSION_UNCERTAIN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    # failure / halt states
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REAPPROVAL_REQUIRED = "REAPPROVAL_REQUIRED"
    REEVALUATION_REQUIRED = "REEVALUATION_REQUIRED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    CANCELLED = "CANCELLED"


ALL_STATES = tuple(
    v for k, v in vars(ExecutionState).items() if not k.startswith("_") and isinstance(v, str)
)

# SUBMISSION_UNCERTAIN is an addition to the sketched machine. It is the
# crash-safety state: the intent to submit was durably recorded but the outcome
# is unknown. It can only be left by reconciling against the broker, never by
# resubmitting.
VALID_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    ExecutionState.PROPOSED: (
        ExecutionState.APPROVED, ExecutionState.REJECTED, ExecutionState.EXPIRED,
        ExecutionState.CANCELLED, ExecutionState.REEVALUATION_REQUIRED,
    ),
    ExecutionState.APPROVED: (
        ExecutionState.PRE_EXECUTION_VALIDATED, ExecutionState.EXPIRED,
        ExecutionState.REAPPROVAL_REQUIRED, ExecutionState.REEVALUATION_REQUIRED,
        ExecutionState.REJECTED, ExecutionState.CANCELLED,
    ),
    ExecutionState.PRE_EXECUTION_VALIDATED: (
        ExecutionState.SUBMITTED, ExecutionState.SUBMISSION_UNCERTAIN,
        ExecutionState.EXECUTION_FAILED, ExecutionState.REAPPROVAL_REQUIRED,
        ExecutionState.REEVALUATION_REQUIRED, ExecutionState.CANCELLED,
        ExecutionState.EXPIRED,
    ),
    ExecutionState.SUBMITTED: (
        ExecutionState.PARTIALLY_FILLED, ExecutionState.FILLED,
        ExecutionState.CANCELLED, ExecutionState.REJECTED,
        ExecutionState.EXECUTION_FAILED,
    ),
    ExecutionState.SUBMISSION_UNCERTAIN: (
        ExecutionState.SUBMITTED, ExecutionState.PARTIALLY_FILLED,
        ExecutionState.FILLED, ExecutionState.EXECUTION_FAILED,
        ExecutionState.CANCELLED, ExecutionState.REJECTED,
    ),
    ExecutionState.PARTIALLY_FILLED: (
        ExecutionState.FILLED, ExecutionState.CANCELLED,
    ),
    # terminal
    ExecutionState.FILLED: (),
    ExecutionState.REJECTED: (),
    ExecutionState.EXPIRED: (),
    ExecutionState.CANCELLED: (),
    ExecutionState.EXECUTION_FAILED: (),
    ExecutionState.REAPPROVAL_REQUIRED: (
        ExecutionState.APPROVED, ExecutionState.CANCELLED, ExecutionState.EXPIRED,
        # Strictly de-escalating: a decision sent back for re-approval can be
        # found to need a whole new evaluation instead, and closing that route
        # off must not require cancelling it outright. There is deliberately no
        # edge back -- REEVALUATION_REQUIRED can never become APPROVED again.
        ExecutionState.REEVALUATION_REQUIRED,
    ),
    ExecutionState.REEVALUATION_REQUIRED: (
        ExecutionState.CANCELLED, ExecutionState.EXPIRED,
    ),
}

TERMINAL_STATES = frozenset(
    s for s, targets in VALID_TRANSITIONS.items() if not targets
)
# States in which a decision no longer holds any of the month's authorization.
#
# A decision reserves dollars for exactly as long as it could still be
# submitted on the strength of the approval it already has, or of one a human
# could still give it. Two kinds of state end that:
#
#   terminal              nothing further can happen to this decision at all;
#   REEVALUATION_REQUIRED the decision's own premise is gone -- the price it
#                         was approved at is no longer the market, or the quote
#                         it rested on is stale. There is no edge from here to
#                         APPROVED, by design, so it can never be submitted:
#                         acting on this asset again needs a *new* decision, a
#                         new decision_id and a new approval, and that new
#                         decision reserves its own dollars.
#
# REAPPROVAL_REQUIRED is deliberately NOT here. The same decision, at the same
# price, for the same amount, can still legally become APPROVED again, so its
# dollars are still spoken for and releasing them would let the month be
# double-spent.
RELEASED_STATES = frozenset(TERMINAL_STATES | {ExecutionState.REEVALUATION_REQUIRED})
# The only state from which a submission may be attempted.
EXECUTABLE_STATES = frozenset({ExecutionState.PRE_EXECUTION_VALIDATED})
# States that mean an order may already exist at the broker.
IN_FLIGHT_STATES = frozenset({
    ExecutionState.SUBMITTED,
    ExecutionState.SUBMISSION_UNCERTAIN,
    ExecutionState.PARTIALLY_FILLED,
    ExecutionState.FILLED,
})


# --------------------------------------------------------------------------
# Closing out an approved-but-unsubmitted decision
# --------------------------------------------------------------------------
#
# An approval is permission to buy *this* asset, for *this* amount, at roughly
# *this* price. When the price stops being roughly that price, the permission
# has not merely paused — its premise is gone.
#
# Nothing used to write that down. ``preflight`` computes a ``next_state`` on
# failure and every caller discarded it, so a decision whose live preflight had
# failed on price drift stayed ``APPROVED`` indefinitely, and
# ``sibling_reservations_usd`` went on reserving its dollars against a month's
# authorization it could never legally spend. The month silently shrank for a
# purchase that would never happen, and nothing in the system said so.
#
# The close is a deliberate, human-invoked act and it must be *grounded*: time
# passing is not a ground, and neither is finding the reservation inconvenient.
# Exactly three grounds exist, and each is checkable rather than asserted.
#
# What the close does NOT do: it deletes nothing. The approval record stays in
# ``state/approvals.json``, the audit trail stays in
# ``logs/execution_audit.jsonl``, and the decision stays in
# ``logs/decisions.jsonl``. It also never pretends the decision executed. It
# records that this decision can no longer be acted on, which is a disposition,
# not a deletion — and it cannot be laundered back into an approval, because
# ``REEVALUATION_REQUIRED`` has no edge to ``APPROVED``.

#: A failed live preflight, on grounds that invalidate the decision itself.
CLOSURE_GROUND_PREFLIGHT = "PREFLIGHT_INVALIDATED"
#: The approval's own TTL or month boundary has passed — an existing rule.
CLOSURE_GROUND_EXPIRED = "APPROVAL_EXPIRED"
#: A human says, explicitly and in writing, that they are not doing this.
CLOSURE_GROUND_ABANDONED = "HUMAN_ABANDONED"

CLOSURE_GROUNDS = (
    CLOSURE_GROUND_PREFLIGHT,
    CLOSURE_GROUND_EXPIRED,
    CLOSURE_GROUND_ABANDONED,
)

#: Preflight blockers that invalidate the *decision*, not merely the moment.
#:
#: Each of these means the priced premise the human approved no longer holds,
#: so no amount of re-approving the same payload makes it executable: the
#: payload states a price that is not the market. Contrast ``POLICY_CHANGED``
#: and ``DECISION_MODIFIED``, which are deliberately absent — those have their
#: own, narrower lifecycle (``REAPPROVAL_REQUIRED`` -> ``APPROVED``) in which
#: the same decision legitimately survives a fresh human look.
DECISION_INVALIDATING_CODES = frozenset({
    "PRICE_MOVED_BEYOND_TOLERANCE",
    "STALE_QUOTE",
    "APPROVAL_EXPIRED",
    "APPROVAL_MONTH_ROLLED_OVER",
})

#: The codes that on their own establish :data:`CLOSURE_GROUND_EXPIRED`.
EXPIRY_CODES = frozenset({"APPROVAL_EXPIRED", "APPROVAL_MONTH_ROLLED_OVER"})


@dataclass
class ClosurePlan:
    """Whether, and how, an unsubmitted decision may be closed out.

    Pure data. Nothing here writes, and a plan is not a transition: a caller
    still has to perform it through :func:`src.execution_store.transition`,
    which re-checks the state machine.
    """

    ok: bool
    target_state: str = ""
    ground: str = ""
    codes: Tuple[str, ...] = ()
    reason: str = ""
    already_closed: bool = False
    refusals: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def refusal_codes(self) -> List[str]:
        return [code for code, _ in self.refusals]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "target_state": self.target_state,
            "ground": self.ground,
            "codes": list(self.codes),
            "reason": self.reason,
            "already_closed": self.already_closed,
            "refusals": [{"code": c, "message": m} for c, m in self.refusals],
        }


def plan_closure(
    current_state: str,
    ground: str,
    codes: Any = (),
    note: str = "",
) -> ClosurePlan:
    """Decide whether this decision may be closed, and to which state.

    Idempotent by construction: a decision already in a released state returns
    ``ok=True, already_closed=True`` and asks for no transition at all, so
    re-running a close is safe and changes nothing.
    """
    codes = tuple(str(c) for c in (codes or ()))

    if current_state in RELEASED_STATES:
        return ClosurePlan(
            ok=True,
            already_closed=True,
            ground=ground,
            codes=codes,
            reason="already in %s; it reserves nothing and can no longer be "
                   "submitted" % current_state,
        )

    if current_state in IN_FLIGHT_STATES:
        return ClosurePlan(
            ok=False,
            refusals=[(
                "ORDER_MAY_EXIST",
                "decision is in %s: an order may exist at the broker. Reconcile "
                "with scripts/reconcile_submission.py first. A reservation is "
                "never released by assuming nothing happened." % current_state,
            )],
        )

    if ground not in CLOSURE_GROUNDS:
        return ClosurePlan(
            ok=False,
            refusals=[(
                "UNKNOWN_CLOSURE_GROUND",
                "%r is not a recognised ground; one of %s is required"
                % (ground, ", ".join(CLOSURE_GROUNDS)),
            )],
        )

    matched: Tuple[str, ...] = ()
    if ground == CLOSURE_GROUND_PREFLIGHT:
        matched = tuple(c for c in codes if c in DECISION_INVALIDATING_CODES)
        if not matched:
            return ClosurePlan(
                ok=False,
                ground=ground,
                codes=codes,
                refusals=[(
                    "UNGROUNDED_CLOSURE",
                    "a live preflight reported %s, none of which invalidates the "
                    "decision itself (%s do). A decision is not closed out "
                    "because a check failed; it is closed out because its priced "
                    "premise is gone."
                    % (", ".join(codes) or "no blockers",
                       ", ".join(sorted(DECISION_INVALIDATING_CODES))),
                )],
            )
        reason = ("live preflight invalidated the decision: %s" % ", ".join(matched))
    elif ground == CLOSURE_GROUND_EXPIRED:
        matched = tuple(c for c in codes if c in EXPIRY_CODES)
        if not matched:
            return ClosurePlan(
                ok=False,
                ground=ground,
                codes=codes,
                refusals=[(
                    "UNGROUNDED_CLOSURE",
                    "the approval is not expired: re-verification reported %s. "
                    "Time passing is not a ground for closing an approved "
                    "decision." % (", ".join(codes) or "no blockers"),
                )],
            )
        reason = "the approval is no longer valid: %s" % ", ".join(matched)
    else:
        if not (note or "").strip():
            return ClosurePlan(
                ok=False,
                ground=ground,
                codes=codes,
                refusals=[(
                    "ABANDONMENT_UNEXPLAINED",
                    "human abandonment must be written down: supply the reason "
                    "in the record's own history, or use a checkable ground.",
                )],
            )
        reason = "abandoned by the owner: %s" % note.strip()

    target = ExecutionState.REEVALUATION_REQUIRED
    try:
        assert_transition(current_state, target)
    except InvalidTransition as exc:
        return ClosurePlan(
            ok=False,
            ground=ground,
            codes=codes,
            refusals=[("ILLEGAL_TRANSITION", str(exc))],
        )

    if note.strip() and ground != CLOSURE_GROUND_ABANDONED:
        reason = "%s (%s)" % (reason, note.strip())

    return ClosurePlan(
        ok=True,
        target_state=target,
        ground=ground,
        codes=matched or codes,
        reason=reason,
    )


def assert_transition(current: str, target: str) -> None:
    """Raise unless ``current -> target`` is a permitted move."""
    if current not in VALID_TRANSITIONS:
        raise InvalidTransition("unknown current state %r" % current)
    if target not in VALID_TRANSITIONS:
        raise InvalidTransition("unknown target state %r" % target)
    if target not in VALID_TRANSITIONS[current]:
        raise InvalidTransition(
            "%s -> %s is not a permitted transition (allowed: %s)"
            % (current, target, list(VALID_TRANSITIONS[current]) or ["<terminal>"])
        )


# --------------------------------------------------------------------------
# Broker snapshot: everything the executor needs, gathered read-only elsewhere
# --------------------------------------------------------------------------


@dataclass
class BrokerSnapshot:
    """Read-only broker state, collected by the caller and passed in.

    The executor never fetches this itself. Anything missing is a blocker, not a
    default: a failed read must never look like a clean account.
    """

    as_of: datetime
    account_is_agentic: bool
    account_masked: str
    equity_orders: Any = None
    crypto_orders: Any = None
    buying_power_usd: Optional[Decimal] = None
    # Cash-only funding. `cash_usd` is the account's own cash balance; on a
    # margin-capable account `buying_power_usd` may be far larger and must never
    # be used as the funding basis.
    cash_usd: Optional[Decimal] = None
    unsettled_funds_usd: Optional[Decimal] = None
    account_type: Optional[str] = None
    quote_price_usd: Optional[Decimal] = None
    quote_timestamp: Optional[datetime] = None
    tradable: Optional[bool] = None
    fractional_tradable: Optional[bool] = None
    account_type_tradable: Optional[bool] = None
    crypto_pair_halted: Optional[bool] = None
    crypto_min_order_size: Optional[Decimal] = None
    read_errors: List[str] = field(default_factory=list)


@dataclass
class PreflightResult:
    ok: bool
    next_state: str
    blockers: List[Tuple[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)
    reconciliation: Optional[Reconciliation] = None
    max_executable_usd: Decimal = ZERO

    @property
    def codes(self) -> List[str]:
        return [code for code, _ in self.blockers]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "next_state": self.next_state,
            "blockers": [{"code": c, "message": m} for c, m in self.blockers],
            "warnings": list(self.warnings),
            "checks": list(self.checks),
            "max_executable_usd": money_str(self.max_executable_usd),
            "reconciliation": self.reconciliation.to_dict() if self.reconciliation else None,
        }


# --------------------------------------------------------------------------
# Gate 0: the three switches
# --------------------------------------------------------------------------


def execution_gate_blockers(config: Config) -> List[Tuple[str, str]]:
    """Why execution is not permitted right now. Empty means all switches open."""
    blockers: List[Tuple[str, str]] = []

    if not config.agent_enabled:
        blockers.append(
            ("AGENT_DISABLED",
             "kill switch: agent_enabled is false in %s. No execution, no submission, "
             "no live-order path. Research and dry-run evaluation still work."
             % config.path)
        )

    if config.execution_mode == EXECUTION_MODE_DRY_RUN:
        blockers.append(
            ("EXECUTION_MODE_DRY_RUN",
             "execution_mode is DRY_RUN; decisions are produced and logged but never executed")
        )
    elif config.execution_mode == EXECUTION_MODE_AUTONOMOUS:
        blockers.append(
            ("AUTONOMOUS_NOT_IMPLEMENTED",
             "execution_mode AUTONOMOUS is not implemented and is refused unconditionally. "
             "Unattended execution has not been designed, reviewed, or approved.")
        )
    elif config.execution_mode != EXECUTION_MODE_APPROVAL_REQUIRED:  # pragma: no cover
        blockers.append(
            ("EXECUTION_MODE_UNKNOWN",
             "unrecognized execution_mode %r" % config.execution_mode)
        )

    if not config.live_trading:
        blockers.append(
            ("LIVE_TRADING_DISABLED",
             "the live_trading interlock is false; all three switches "
             "(execution_mode, agent_enabled, live_trading) must be open")
        )

    return blockers


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def _age_seconds(when: Optional[datetime], now: datetime) -> Optional[float]:
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds()


def preflight(
    decision: Dict[str, Any],
    approval: Optional[ApprovalRecord],
    config: Config,
    budget_state: BudgetState,
    snapshot: Optional[BrokerSnapshot],
    execution_state: str = ExecutionState.PROPOSED,
    now: Optional[datetime] = None,
    crypto_universe: Optional[CryptoUniverse] = None,
    sibling_reservations_usd: Decimal = ZERO,
) -> PreflightResult:
    """Everything that must be true immediately before an order could be sent.

    Returns a :class:`PreflightResult`. ``ok`` never means "an order was placed";
    it means "if execution were enabled, this specific order would be permitted".

    ``sibling_reservations_usd`` (Stage 7) is the total held by *other* legs of
    the same allocation plan that a human has already approved but that have not
    yet been submitted. The broker cannot see those dollars and the local ledger
    does not record them either, so without this a two-leg plan could approve
    $15 and then approve another $15 against the same $25. It defaults to zero,
    which is exactly the single-leg behaviour of Stage 5. Compute it with
    :func:`src.allocation.sibling_reservations_usd` — this module still performs
    no I/O of its own.
    """
    now = now or utc_now()
    blockers: List[Tuple[str, str]] = []
    warnings: List[str] = []
    checks: List[str] = []
    next_state = execution_state
    reconciliation: Optional[Reconciliation] = None
    max_executable = ZERO

    # --- 1. the three switches ------------------------------------------
    checks.append("execution_switches")
    gate = execution_gate_blockers(config)
    blockers.extend(gate)

    # --- 2. idempotency: has this decision already been acted on? --------
    checks.append("not_already_in_flight")
    if execution_state in IN_FLIGHT_STATES:
        if execution_state == ExecutionState.SUBMISSION_UNCERTAIN:
            blockers.append(
                ("RECONCILIATION_REQUIRED",
                 "this decision is in SUBMISSION_UNCERTAIN: a submission intent was "
                 "durably recorded but the outcome is unknown. Reconcile the broker "
                 "before anything else. Never retry blindly.")
            )
        else:
            blockers.append(
                ("ALREADY_SUBMITTED",
                 "decision %s is already in state %s; re-running the executor must never "
                 "create a second order"
                 % (str(decision.get("decision_id")), execution_state))
            )
    if execution_state in TERMINAL_STATES:
        blockers.append(
            ("TERMINAL_STATE", "decision is in terminal state %s" % execution_state)
        )

    checks.append("decision_id_not_already_committed")
    decision_id = str(decision.get("decision_id") or "").strip()
    if decision_id and budget_state.has_acted(decision_id):
        blockers.append(
            ("DUPLICATE_DECISION_ID",
             "decision_id %s has already been acted upon in the budget ledger" % decision_id)
        )

    # --- 3. self-approval can never happen ------------------------------
    checks.append("no_self_approval_in_payload")
    for key in ("approved", "approval", "approved_by", "user_approved", "execution_state"):
        if key in decision:
            blockers.append(
                ("SELF_APPROVAL_ATTEMPTED",
                 "the decision payload carries %r. A decision cannot approve itself; "
                 "approval lives only in state/approvals.json and is created only by "
                 "scripts/approve_decision.py." % key)
            )
            break

    # --- 3b. prohibitions are re-checked at execution time ---------------
    checks.append("prohibitions_still_satisfied")
    try:
        revalidation = validate_decision(
            decision, config, budget_state,
            crypto_universe=crypto_universe, now=now,
        )
        still_prohibited = [
            v for v in revalidation.violations if v.code in PROHIBITION_CODES
        ]
        for violation in still_prohibited:
            blockers.append((violation.code, "re-check before execution: " + violation.message))
    except Exception as exc:  # noqa: BLE001 - any failure here must fail closed
        blockers.append(
            ("REVALIDATION_FAILED",
             "the decision could not be re-validated before execution (%s); failing closed"
             % exc)
        )

    # --- 4. approval -----------------------------------------------------
    checks.append("approval_valid_for_this_exact_decision")
    verdict = verify_approval(approval, decision, now=now)
    checks.extend("approval::" + c for c in verdict.checks)
    if not verdict.ok:
        blockers.extend(verdict.blockers)
        if any(c in ("APPROVAL_EXPIRED", "APPROVAL_MONTH_ROLLED_OVER") for c in verdict.codes):
            next_state = ExecutionState.EXPIRED
        elif any(c in ("DECISION_MODIFIED", "POLICY_CHANGED", "AMOUNT_EXCEEDS_APPROVAL",
                       "APPROVAL_FIELD_MISMATCH") for c in verdict.codes):
            next_state = ExecutionState.REAPPROVAL_REQUIRED

    # --- 5. broker snapshot must exist ----------------------------------
    checks.append("broker_readable")
    if snapshot is None:
        blockers.append(
            ("BROKER_UNREADABLE",
             "no broker snapshot was supplied. A failed or missing read must never be "
             "treated as a clean account. Failing closed.")
        )
        return PreflightResult(False, ExecutionState.REEVALUATION_REQUIRED,
                               blockers, warnings, checks, None, ZERO)
    if snapshot.read_errors:
        blockers.append(
            ("BROKER_UNREADABLE",
             "broker reads reported errors: %s" % "; ".join(snapshot.read_errors))
        )
    if not snapshot.account_is_agentic:
        blockers.append(
            ("WRONG_ACCOUNT",
             "the snapshot is not for the agent-accessible account (%s)" % snapshot.account_masked)
        )

    # --- 6. reconciliation is authoritative over the local ledger --------
    checks.append("broker_reconciliation")
    try:
        reconciliation = reconcile(
            config, budget_state, snapshot.equity_orders,
            crypto_orders=snapshot.crypto_orders, now=now,
        )
    except ReconciliationError as exc:
        blockers.append(("RECONCILIATION_FAILED", str(exc)))
        reconciliation = None

    amount = ZERO
    try:
        amount = parse_money(
            decision.get("proposed_amount_usd", decision.get("proposed_amount", "0")),
            "proposed_amount_usd",
        ).quantize(CENTS)
    except MoneyError as exc:
        blockers.append(("MALFORMED_AMOUNT", str(exc)))

    if reconciliation is not None:
        if reconciliation.blocking:
            blockers.append(
                ("RECONCILIATION_BLOCKING",
                 "; ".join(reconciliation.discrepancies) or "reconciliation is blocking")
            )
        max_executable = reconciliation.max_additional_order_usd

        # Stage 7: sibling legs already approved but not yet submitted are
        # invisible to both the broker and the local ledger. Reserve them
        # before deciding how much this leg may take.
        reserved = ZERO
        if sibling_reservations_usd:
            try:
                reserved = parse_money(
                    sibling_reservations_usd, "sibling_reservations_usd"
                ).quantize(CENTS)
            except MoneyError as exc:
                blockers.append(("MALFORMED_NUMBER", str(exc)))
                reserved = ZERO
            if reserved < ZERO:
                blockers.append(
                    ("MALFORMED_NUMBER", "sibling_reservations_usd is negative")
                )
                reserved = ZERO
        if reserved > ZERO:
            checks.append("sibling_leg_reservations")
            max_executable = (max_executable - reserved).quantize(CENTS)
            if max_executable < ZERO:
                max_executable = ZERO
            warnings.append(
                "reserving %s for sibling plan legs already approved but not yet "
                "submitted; this leg may take at most %s"
                % (money_str(reserved), money_str(max_executable))
            )

        if amount > max_executable:
            blockers.append(
                ("EXCEEDS_RECONCILED_AUTHORIZATION",
                 "order of %s exceeds the reconciled maximum additional order of %s "
                 "for %s%s" % (
                     money_str(amount), money_str(max_executable), reconciliation.month,
                     (" (after reserving %s for sibling plan legs already approved "
                      "but not yet submitted)" % money_str(reserved)) if reserved > ZERO else ""))
            )
        for note in reconciliation.discrepancies:
            warnings.append("reconciliation: " + note)

    checks.append("month_is_current")
    if budget_state.month != current_month(now):
        blockers.append(
            ("MONTH_ROLLED_OVER",
             "budget state month %s is not the current month %s"
             % (budget_state.month, current_month(now)))
        )

    # --- 7. funds: SETTLED CASH ONLY -------------------------------------
    # Policy is cash-only. Buying power is checked as an additional ceiling but
    # is NEVER the funding basis: on a margin-capable account it can include
    # borrowed money, and this agent must never borrow.
    checks.append("settled_cash_covers_the_order")
    settled_cash: Optional[Decimal] = None
    if snapshot.cash_usd is None:
        blockers.append(
            ("CASH_UNKNOWN",
             "the account's cash balance could not be established. Execution is "
             "cash-only, so an unknown cash position fails closed rather than "
             "falling back to buying power, which may include margin.")
        )
    else:
        unsettled = snapshot.unsettled_funds_usd or ZERO
        if unsettled < ZERO:
            blockers.append(("MALFORMED_NUMBER", "unsettled_funds_usd is negative"))
            unsettled = ZERO
        settled_cash = (snapshot.cash_usd - unsettled).quantize(CENTS)
        if settled_cash < ZERO:
            settled_cash = ZERO
        if unsettled > ZERO:
            warnings.append(
                "%s of the cash balance is unsettled and is excluded from the "
                "spendable figure" % money_str(unsettled)
            )
        if amount > settled_cash:
            blockers.append(
                ("INSUFFICIENT_SETTLED_CASH",
                 "order of %s exceeds settled cash of %s. Deposit settled funds; the "
                 "agent will not borrow to cover the difference."
                 % (money_str(amount), money_str(settled_cash)))
            )

    checks.append("no_margin_would_be_used")
    account_type = (snapshot.account_type or "").strip().lower()
    if account_type in MARGIN_CAPABLE_ACCOUNT_TYPES and settled_cash is not None:
        if amount > settled_cash:
            blockers.append(
                ("MARGIN_RISK",
                 "this is a %s account and the order of %s exceeds settled cash of %s, "
                 "so the broker could fund the difference on margin. Refusing."
                 % (account_type, money_str(amount), money_str(settled_cash)))
            )
    elif not account_type:
        warnings.append(
            "account_type was not supplied; the margin-capability check could not run"
        )

    checks.append("buying_power_ceiling")
    if snapshot.buying_power_usd is None:
        blockers.append(
            ("BUYING_POWER_UNKNOWN", "buying power was not read; cannot confirm funds")
        )
    elif amount > snapshot.buying_power_usd:
        blockers.append(
            ("INSUFFICIENT_BUYING_POWER",
             "order of %s exceeds buying power of %s"
             % (money_str(amount), money_str(snapshot.buying_power_usd)))
        )

    # --- 8. tradability --------------------------------------------------
    is_crypto = (
        str(decision.get("asset_class", "")).upper() == ASSET_CLASS_CRYPTO
        or str(decision.get("asset_type", "")).lower() == ASSET_CRYPTO
    )
    checks.append("security_tradable")
    if is_crypto:
        if snapshot.crypto_pair_halted is None:
            blockers.append(("TRADABILITY_UNKNOWN", "crypto pair halt status was not read"))
        elif snapshot.crypto_pair_halted:
            blockers.append(("SECURITY_UNTRADABLE", "the currency pair is halted"))
        if snapshot.crypto_min_order_size is not None and snapshot.quote_price_usd:
            try:
                units = amount / snapshot.quote_price_usd
            except (InvalidOperation, ZeroDivisionError):
                units = ZERO
            if units < snapshot.crypto_min_order_size:
                blockers.append(
                    ("BELOW_MIN_ORDER_SIZE",
                     "%s buys %s units, below the pair minimum of %s"
                     % (money_str(amount), units, snapshot.crypto_min_order_size))
                )
    else:
        if snapshot.tradable is None or snapshot.account_type_tradable is None:
            blockers.append(("TRADABILITY_UNKNOWN", "equity tradability was not read"))
        else:
            if not snapshot.tradable:
                blockers.append(("SECURITY_UNTRADABLE", "the security is not tradable"))
            if not snapshot.account_type_tradable:
                blockers.append(
                    ("SECURITY_UNTRADABLE", "the security is not tradable in this account type")
                )
        if amount < MIN_EQUITY_ORDER_USD:
            blockers.append(
                ("BELOW_BROKER_MINIMUM",
                 "%s is below the %s dollar-based equity minimum"
                 % (money_str(amount), money_str(MIN_EQUITY_ORDER_USD)))
            )
        if snapshot.fractional_tradable is False and snapshot.quote_price_usd:
            if amount < snapshot.quote_price_usd:
                blockers.append(
                    ("FRACTIONAL_NOT_ELIGIBLE",
                     "%s buys less than one share and the security is not fractional-eligible"
                     % money_str(amount))
                )

    # --- 9. quote freshness ---------------------------------------------
    checks.append("quote_fresh")
    max_age = MAX_CRYPTO_QUOTE_AGE_SECONDS if is_crypto else MAX_EQUITY_QUOTE_AGE_SECONDS
    age = _age_seconds(snapshot.quote_timestamp, now)
    if snapshot.quote_price_usd is None or age is None:
        blockers.append(
            ("STALE_QUOTE", "no fresh quote was supplied; approval does not authorize "
                            "executing on stale research")
        )
        next_state = ExecutionState.REEVALUATION_REQUIRED
    elif age > max_age:
        blockers.append(
            ("STALE_QUOTE",
             "quote is %.0fs old, over the %ds limit for %s"
             % (age, max_age, "crypto" if is_crypto else "equities"))
        )
        next_state = ExecutionState.REEVALUATION_REQUIRED
    elif age < -5:
        blockers.append(("STALE_QUOTE", "quote timestamp is in the future"))

    # --- 10. slippage since approval ------------------------------------
    checks.append("price_movement_within_tolerance")
    approved_price = None
    try:
        raw_price = decision.get("current_price_usd")
        if raw_price not in (None, ""):
            approved_price = parse_money(raw_price, "current_price_usd")
    except MoneyError as exc:
        blockers.append(("MALFORMED_NUMBER", str(exc)))

    if approved_price and approved_price > ZERO and snapshot.quote_price_usd:
        move = (snapshot.quote_price_usd - approved_price) / approved_price * 100
        limit = MAX_CRYPTO_SLIPPAGE_PCT if is_crypto else MAX_EQUITY_SLIPPAGE_PCT
        if abs(move) > limit:
            blockers.append(
                ("PRICE_MOVED_BEYOND_TOLERANCE",
                 "price moved %+.2f%% since the decision was priced (%s -> %s), beyond the "
                 "%.1f%% %s tolerance. Not executing; re-approval required."
                 % (move, money_str(approved_price), money_str(snapshot.quote_price_usd),
                    limit, "crypto" if is_crypto else "equity"))
            )
            next_state = ExecutionState.REAPPROVAL_REQUIRED
        elif abs(move) > limit / 2:
            warnings.append(
                "price moved %+.2f%% since pricing, within tolerance but material" % move
            )

    ok = not blockers
    if ok:
        next_state = ExecutionState.PRE_EXECUTION_VALIDATED
    elif next_state == execution_state:
        next_state = ExecutionState.REEVALUATION_REQUIRED

    return PreflightResult(
        ok, next_state, blockers, warnings, checks, reconciliation, max_executable
    )


# --------------------------------------------------------------------------
# Order construction (built, never sent)
# --------------------------------------------------------------------------


def build_order_request(
    decision: Dict[str, Any],
    snapshot: BrokerSnapshot,
    account_number: str,
) -> Dict[str, Any]:
    """The exact parameters that WOULD be sent. Nothing is sent.

    Equity: `dollar_amount` requires `type=market`, and fractional shares require
    `market_hours=regular_hours`, so a $25 fractional buy has no broker-side limit
    price available. Protection is entirely pre-trade.

    Crypto: `dollar_amount` is valid with `type=limit`, and a limit buy's debit is
    capped at `dollar_amount` with no collar, versus a ~1% buy collar on a market
    order. The limit form is therefore specified.
    """
    amount = parse_money(
        decision.get("proposed_amount_usd", decision.get("proposed_amount", "0"))
    ).quantize(CENTS)
    symbol = str(decision.get("ticker") or decision.get("symbol") or "").upper()
    decision_id = str(decision.get("decision_id") or "")
    is_crypto = (
        str(decision.get("asset_class", "")).upper() == ASSET_CLASS_CRYPTO
        or str(decision.get("asset_type", "")).lower() == ASSET_CRYPTO
    )

    if str(decision.get("side", "")).lower() != "buy":
        raise ExecutionDisabled("only a long BUY can ever be constructed")

    if is_crypto:
        if not snapshot.quote_price_usd:
            raise ExecutionDisabled("a crypto limit order requires a fresh quote")
        limit_price = (
            snapshot.quote_price_usd * (1 + CRYPTO_LIMIT_BUFFER_PCT / 100)
        ).quantize(Decimal("0.00000001"))
        return {
            "_tool": "place_crypto_order",
            "_note": "NOT SENT. Limit chosen over market: a limit buy's debit is capped "
                     "at dollar_amount, while a market buy carries a ~1% collar.",
            "rhs_account_number": account_number,
            "symbol": symbol,
            "side": "buy",
            "type": "limit",
            "dollar_amount": money_str(amount),
            "limit_price": str(limit_price),
            "time_in_force": "gtc",
            "ref_id": broker_ref_id(decision_id),
        }

    return {
        "_tool": "place_equity_order",
        "_note": "NOT SENT. type=market is forced: dollar_amount is only valid with "
                 "market, and fractional requires market + regular_hours.",
        "account_number": account_number,
        "symbol": symbol,
        "side": "buy",
        "type": "market",
        "dollar_amount": money_str(amount),
        "market_hours": "regular_hours",
        "time_in_force": "gfd",
        "ref_id": broker_ref_id(decision_id),
    }


def execute(*args: Any, **kwargs: Any) -> None:
    """The submission entry point. Raises unconditionally.

    There is no broker client in this repository and no MCP tool is reachable
    from this module. Enabling the three configuration switches would not make
    this function work; a reviewed submission adapter would still have to be
    written.
    """
    raise ExecutionDisabled(
        "Live execution is not implemented. Stage 4 built the approval, "
        "reconciliation, freshness, slippage, idempotency and audit machinery; it "
        "deliberately did not build an order-submission path. No Robinhood order "
        "tool is imported or callable from src/."
    )
