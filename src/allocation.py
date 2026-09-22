"""Stage 7 — multi-buy allocation plans.

An evaluation now produces a **plan**, one of ``WAIT``, ``SINGLE_BUY`` or
``SPLIT_BUY_PLAN``. This module validates the plan. It does not replace any
existing check and it does not execute anything.

The design rule that makes this safe:

    A plan is a container. Every BUY leg inside it is an ordinary, complete
    decision payload that goes through the unchanged single-decision validator
    in :mod:`src.guardrails`, and keeps its own ``decision_id``. Downstream,
    each leg earns its own fingerprint, its own approval, its own submission
    ticket, its own reconciliation and its own idempotency protection.

So the plan is a *presentation* and *arithmetic* layer. Approval and execution
stay exactly as granular, and exactly as auditable, as they were in Stage 5.
Nothing here can approve anything, and there is still no order path.

The cross-leg arithmetic is the part a single-decision validator structurally
cannot do:

* legs are validated against a **cumulative** budget state, so leg 2 is checked
  against what is left after leg 1. Three $10 legs cannot each pass on the
  grounds that $25 was available;
* the combined total must fit the remaining authorization;
* ``combined_exposure`` adds the fourth quantity the broker cannot see —
  sibling legs that are already **approved but not yet submitted** — so that
  executed + pending + approved + proposed never exceeds the month's $25.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.guardrails import (  # noqa: E402
    SELF_APPROVAL_FIELDS,
    CryptoUniverse,
    utc_now,
    validate,
)
from src.models import (  # noqa: E402
    CENTS,
    LEG_COUNT_FOR_PLAN,
    PLAN_SINGLE_BUY,
    PLAN_SPLIT_BUY,
    PLAN_WAIT,
    REQUIRED_LEG_ALLOCATION_KEYS,
    REQUIRED_PLAN_SPRAWL_KEYS,
    VALID_PLAN_TYPES,
    ZERO,
    MoneyError,
    ValidationResult,
    Violation,
    money_str,
    parse_money,
    usd,
)
from src.state import BudgetState, Config  # noqa: E402


class AllocationError(Exception):
    """Raised when a plan is so malformed it cannot even be inspected."""


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass
class LegOutcome:
    """One BUY leg and the verdict of the ordinary single-decision validator."""

    index: int
    decision_id: str
    ticker: Optional[str]
    asset_class: Optional[str]
    position_type: Optional[str]
    amount_usd: Decimal
    result: Optional[ValidationResult] = None
    # Authorization already consumed by the legs ahead of this one.
    preceding_committed_usd: Decimal = ZERO

    @property
    def valid(self) -> bool:
        return self.result is not None and self.result.valid

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "decision_id": self.decision_id,
            "ticker": self.ticker,
            "asset_class": self.asset_class,
            "position_type": self.position_type,
            "amount_usd": money_str(self.amount_usd),
            "preceding_committed_usd": money_str(self.preceding_committed_usd),
            "valid": self.valid,
            "result": self.result.to_dict() if self.result else None,
        }


@dataclass
class PlanValidation:
    """Outcome of validating a whole allocation plan.

    ``executable`` is always False, for the same reason it is always False on a
    single decision: there is no execution path in this repository.
    """

    valid: bool
    plan_type: str = ""
    plan_id: str = ""
    violations: List[Violation] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks_run: List[str] = field(default_factory=list)
    legs: List[LegOutcome] = field(default_factory=list)
    total_amount_usd: Decimal = ZERO
    remaining_before_usd: Decimal = ZERO
    remaining_after_usd: Decimal = ZERO
    executable: bool = False
    execution_status: str = "DRY_RUN_NOT_EXECUTED"

    @property
    def violation_codes(self) -> List[str]:
        return [v.code for v in self.violations]

    @property
    def leg_count(self) -> int:
        return len(self.legs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "executable": self.executable,
            "plan_type": self.plan_type,
            "plan_id": self.plan_id,
            "leg_count": self.leg_count,
            "total_amount_usd": money_str(self.total_amount_usd),
            "remaining_before_usd": money_str(self.remaining_before_usd),
            "remaining_after_usd": money_str(self.remaining_after_usd),
            "violations": [v.to_dict() for v in self.violations],
            "warnings": list(self.warnings),
            "checks_run": list(self.checks_run),
            "legs": [leg.to_dict() for leg in self.legs],
            "execution_status": self.execution_status,
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _nonempty(value: Any, min_len: int = 1) -> bool:
    return isinstance(value, str) and len(value.strip()) >= min_len


def _missing(block: Dict[str, Any], keys: Sequence[str]) -> List[str]:
    return [k for k in keys if not _nonempty(block.get(k))]


def _cumulative_state(state: BudgetState, already: Decimal) -> BudgetState:
    """A copy of ``state`` with ``already`` extra dollars treated as committed.

    This is what lets each leg be checked by the *unchanged* single-decision
    validator while still seeing the truth: the authorization the legs before
    it have already consumed. It also makes the mid-month optionality gate fire
    on whichever leg actually crosses the halfway mark.
    """
    return BudgetState(
        month=state.month,
        authorized_budget_usd=state.authorized_budget_usd,
        committed_usd=(state.committed_usd + already).quantize(CENTS),
        acted_decision_ids=list(state.acted_decision_ids),
        last_updated=state.last_updated,
        schema_version=state.schema_version,
        path=state.path,
    )


def cumulative_state(state: BudgetState, already: Decimal) -> BudgetState:
    """Public alias of :func:`_cumulative_state`, for loggers and reporters.

    A leg's ``monthly_budget_after`` is only correct when it is computed against
    the authorization the earlier legs already consumed.
    """
    return _cumulative_state(state, already)


def _leg_amount(leg: Dict[str, Any]) -> Tuple[Decimal, Optional[str]]:
    raw = leg.get("proposed_amount_usd", leg.get("proposed_amount"))
    if raw is None:
        return ZERO, "leg is missing proposed_amount_usd"
    try:
        return parse_money(raw, "proposed_amount_usd").quantize(CENTS), None
    except MoneyError as exc:
        return ZERO, str(exc)


def _asset_key(leg: Dict[str, Any]) -> str:
    ticker = str(leg.get("ticker") or leg.get("symbol") or "").strip().upper()
    asset_class = str(leg.get("asset_class") or "").strip().upper()
    return "%s/%s" % (ticker, asset_class)


# --------------------------------------------------------------------------
# Plan validation
# --------------------------------------------------------------------------


def validate_plan(
    raw_plan: Any,
    config: Config,
    state: BudgetState,
    crypto_universe: Optional[CryptoUniverse] = None,
    now: Optional[datetime] = None,
) -> PlanValidation:
    """Validate an allocation plan and every leg inside it.

    Each leg is validated by :func:`src.guardrails.validate` unchanged, against
    a budget state that already reflects the legs ahead of it.
    """
    now = now or utc_now()
    violations: List[Violation] = []
    warnings: List[str] = []
    checks: List[str] = ["plan_wellformed"]

    if not isinstance(raw_plan, dict):
        return PlanValidation(
            valid=False,
            violations=[Violation("MALFORMED_PLAN", "a plan must be a JSON object")],
            checks_run=checks,
            remaining_before_usd=state.remaining_usd,
            remaining_after_usd=state.remaining_usd,
        )

    plan_type = str(raw_plan.get("plan_type") or "").strip().upper()
    plan_id = str(raw_plan.get("plan_id") or "").strip()

    # --- a plan can never approve itself, exactly like a decision --------
    checks.append("no_self_approval_fields")
    for key in SELF_APPROVAL_FIELDS:
        if key in raw_plan:
            violations.append(
                Violation(
                    "SELF_APPROVAL_ATTEMPTED",
                    "the plan payload carries %r. A plan is a recommendation, never "
                    "permission: approvals live only in state/approvals.json and are "
                    "created only by scripts/approve_decision.py, one leg at a time." % key,
                )
            )
            break

    # --- plan type -------------------------------------------------------
    checks.append("plan_type_recognized")
    if plan_type not in VALID_PLAN_TYPES:
        violations.append(
            Violation(
                "INVALID_PLAN_TYPE",
                "plan_type must be one of %s, got %r"
                % (list(VALID_PLAN_TYPES), raw_plan.get("plan_type")),
            )
        )
        return PlanValidation(
            valid=False,
            plan_type=plan_type,
            plan_id=plan_id,
            violations=violations,
            warnings=warnings,
            checks_run=checks,
            remaining_before_usd=state.remaining_usd,
            remaining_after_usd=state.remaining_usd,
        )

    checks.append("plan_id_present")
    if not plan_id:
        violations.append(
            Violation("MISSING_PLAN_ID", "every plan must carry a non-empty plan_id")
        )

    raw_legs = raw_plan.get("legs")
    if raw_legs is None:
        raw_legs = []
    if not isinstance(raw_legs, list):
        violations.append(Violation("MALFORMED_PLAN", "plan.legs must be a list"))
        raw_legs = []

    # --- a WAIT plan is just a WAIT decision -----------------------------
    if plan_type == PLAN_WAIT:
        checks.append("wait_plan_has_no_legs")
        if raw_legs:
            violations.append(
                Violation(
                    "WAIT_PLAN_WITH_LEGS",
                    "a WAIT plan must contain no BUY legs (got %d)" % len(raw_legs),
                )
            )
        decision = raw_plan.get("decision")
        if isinstance(decision, dict):
            checks.append("wait_decision_delegated_to_guardrails")
            result = validate(decision, config, state, crypto_universe=crypto_universe, now=now)
            violations.extend(result.violations)
            warnings.extend(result.warnings)
            checks.extend("leg0::" + c for c in result.checks_run)
        else:
            violations.append(
                Violation(
                    "MISSING_WAIT_DECISION",
                    "a WAIT plan must carry a full 'decision' object so the WAIT rules "
                    "(best existing / new-equity / crypto candidate, wait_basis) still apply",
                )
            )
        return PlanValidation(
            valid=not violations,
            plan_type=plan_type,
            plan_id=plan_id,
            violations=_dedupe(violations),
            warnings=warnings,
            checks_run=checks,
            legs=[],
            total_amount_usd=ZERO,
            remaining_before_usd=state.remaining_usd,
            remaining_after_usd=state.remaining_usd,
        )

    # --- leg count -------------------------------------------------------
    checks.append("leg_count_within_plan_type")
    low, high = LEG_COUNT_FOR_PLAN[plan_type]
    if not (low <= len(raw_legs) <= high):
        violations.append(
            Violation(
                "INVALID_LEG_COUNT",
                "a %s must carry between %d and %d BUY legs, got %d"
                % (plan_type, low, high, len(raw_legs)),
            )
        )

    # --- per-leg validation, cumulatively ---------------------------------
    legs: List[LegOutcome] = []
    running = ZERO
    seen_ids: Dict[str, int] = {}
    seen_assets: Dict[str, int] = {}

    for index, raw_leg in enumerate(raw_legs):
        if not isinstance(raw_leg, dict):
            violations.append(
                Violation("MALFORMED_PLAN", "leg %d is not a JSON object" % index)
            )
            continue

        amount, amount_error = _leg_amount(raw_leg)
        if amount_error:
            violations.append(
                Violation("MALFORMED_NUMBER", "leg %d: %s" % (index, amount_error))
            )

        decision_id = str(raw_leg.get("decision_id") or "").strip()

        # Each leg keeps its own identity all the way to the broker.
        checks.append("leg%d::own_decision_id" % index)
        if decision_id:
            if decision_id in seen_ids:
                violations.append(
                    Violation(
                        "DUPLICATE_LEG_DECISION_ID",
                        "legs %d and %d share decision_id %r. Every leg must be "
                        "separately approvable, fingerprintable and idempotent, which "
                        "requires a distinct id." % (seen_ids[decision_id], index, decision_id),
                    )
                )
            else:
                seen_ids[decision_id] = index

        # Two legs buying the same asset is a split that should have been one leg.
        checks.append("leg%d::asset_not_duplicated" % index)
        key = _asset_key(raw_leg)
        if key.strip("/"):
            if key in seen_assets:
                violations.append(
                    Violation(
                        "DUPLICATE_LEG_ASSET",
                        "legs %d and %d both buy %s. Combine them into one leg rather "
                        "than splitting a single position across two approvals."
                        % (seen_assets[key], index, key),
                    )
                )
            else:
                seen_assets[key] = index

        # The cross-leg reasoning requirement, for splits only.
        if plan_type == PLAN_SPLIT_BUY:
            checks.append("leg%d::allocation_rationale" % index)
            block = raw_leg.get("allocation_rationale")
            if not isinstance(block, dict):
                violations.append(
                    Violation(
                        "MISSING_ALLOCATION_RATIONALE",
                        "leg %d must carry an 'allocation_rationale' object explaining why "
                        "this money is better here than on the other legs (keys: %s)"
                        % (index, ", ".join(REQUIRED_LEG_ALLOCATION_KEYS)),
                    )
                )
            else:
                missing = _missing(block, REQUIRED_LEG_ALLOCATION_KEYS)
                if missing:
                    violations.append(
                        Violation(
                            "MISSING_ALLOCATION_RATIONALE",
                            "leg %d allocation_rationale is missing non-empty %s"
                            % (index, ", ".join(missing)),
                        )
                    )
                else:
                    compared = str(block.get("legs_compared_against", ""))
                    others = [
                        str(other.get("ticker") or other.get("symbol") or "").strip().upper()
                        for j, other in enumerate(raw_legs)
                        if j != index and isinstance(other, dict)
                    ]
                    named = [t for t in others if t and re.search(
                        r"\b%s\b" % re.escape(t), compared.upper())]
                    if not named:
                        violations.append(
                            Violation(
                                "ALLOCATION_RATIONALE_NAMES_NO_SIBLING",
                                "leg %d allocation_rationale.legs_compared_against must name at "
                                "least one other leg in this plan by ticker (siblings: %s)"
                                % (index, ", ".join(t for t in others if t) or "none"),
                            )
                        )

        # The ordinary, unchanged single-decision validator does the real work,
        # against a state that already reflects the legs ahead of this one.
        checks.append("leg%d::delegated_to_guardrails" % index)
        leg_state = _cumulative_state(state, running)
        result = validate(raw_leg, config, leg_state, crypto_universe=crypto_universe, now=now)
        for violation in result.violations:
            violations.append(
                Violation(violation.code, "leg %d (%s): %s" % (
                    index, raw_leg.get("ticker") or "?", violation.message))
            )
        for warning in result.warnings:
            warnings.append("leg %d: %s" % (index, warning))
        checks.extend("leg%d::%s" % (index, c) for c in result.checks_run)

        legs.append(
            LegOutcome(
                index=index,
                decision_id=decision_id,
                ticker=raw_leg.get("ticker") or raw_leg.get("symbol"),
                asset_class=raw_leg.get("asset_class"),
                position_type=raw_leg.get("position_type"),
                amount_usd=amount,
                result=result,
                preceding_committed_usd=running,
            )
        )
        running = (running + amount).quantize(CENTS)

    total = running

    # --- the combined total must fit ------------------------------------
    checks.append("combined_legs_within_remaining_authorization")
    remaining_before = state.remaining_usd
    if total > remaining_before:
        violations.append(
            Violation(
                "PLAN_EXCEEDS_REMAINING_BUDGET",
                "the plan's legs total %s but only %s of the %s authorization remains "
                "for %s. The $25 is one shared authorization across equities, ETFs and "
                "crypto." % (
                    usd(total), usd(remaining_before),
                    usd(state.authorized_budget_usd), state.month),
            )
        )

    # --- a split must actually be a split --------------------------------
    if plan_type == PLAN_SPLIT_BUY:
        checks.append("plan_sprawl_assessment")
        block = raw_plan.get("plan_sprawl_assessment")
        if not isinstance(block, dict):
            violations.append(
                Violation(
                    "MISSING_PLAN_SPRAWL_ASSESSMENT",
                    "a SPLIT_BUY_PLAN creates several positions at once and must carry a "
                    "'plan_sprawl_assessment' object (keys: %s). A new ticker is never "
                    "inherently preferable to increasing a high-conviction holding."
                    % ", ".join(REQUIRED_PLAN_SPRAWL_KEYS),
                )
            )
        else:
            missing = _missing(block, REQUIRED_PLAN_SPRAWL_KEYS)
            if missing:
                violations.append(
                    Violation(
                        "MISSING_PLAN_SPRAWL_ASSESSMENT",
                        "plan_sprawl_assessment is missing non-empty %s" % ", ".join(missing),
                    )
                )

        checks.append("split_rationale_present")
        if not _nonempty(raw_plan.get("split_rationale"), 120):
            violations.append(
                Violation(
                    "MISSING_SPLIT_RATIONALE",
                    "a SPLIT_BUY_PLAN must carry a substantive 'split_rationale' explaining "
                    "why splitting beats concentrating the same dollars into the single "
                    "best candidate. Splitting must never be a way to avoid judgement.",
                )
            )

    return PlanValidation(
        valid=not violations,
        plan_type=plan_type,
        plan_id=plan_id,
        violations=_dedupe(violations),
        warnings=warnings,
        checks_run=checks,
        legs=legs,
        total_amount_usd=total,
        remaining_before_usd=remaining_before,
        remaining_after_usd=(remaining_before - total).quantize(CENTS),
        executable=False,  # never, under any circumstances
    )


def _dedupe(violations: List[Violation]) -> List[Violation]:
    seen = set()
    unique: List[Violation] = []
    for violation in violations:
        key = (violation.code, violation.message)
        if key not in seen:
            seen.add(key)
            unique.append(violation)
    return unique


# --------------------------------------------------------------------------
# Combined exposure — the four-way ceiling
# --------------------------------------------------------------------------


@dataclass
class CombinedExposure:
    """Every dollar of the month's authorization that is spoken for.

    The broker knows about filled and pending orders. It does not know about a
    sibling leg that a human has already approved but not yet submitted, nor
    about legs still merely proposed. In a multi-leg month those are exactly
    the amounts that could silently push the total past $25, so they are
    counted here.
    """

    month: str
    authorized_usd: Decimal
    executed_usd: Decimal
    pending_usd: Decimal
    approved_unsubmitted_usd: Decimal
    proposed_usd: Decimal

    @property
    def committed_usd(self) -> Decimal:
        return (
            self.executed_usd + self.pending_usd + self.approved_unsubmitted_usd
        ).quantize(CENTS)

    @property
    def total_usd(self) -> Decimal:
        return (self.committed_usd + self.proposed_usd).quantize(CENTS)

    @property
    def remaining_usd(self) -> Decimal:
        return (self.authorized_usd - self.committed_usd).quantize(CENTS)

    @property
    def within_authorization(self) -> bool:
        return self.total_usd <= self.authorized_usd

    @property
    def overage_usd(self) -> Decimal:
        over = self.total_usd - self.authorized_usd
        return over.quantize(CENTS) if over > ZERO else ZERO

    def to_dict(self) -> Dict[str, Any]:
        return {
            "month": self.month,
            "authorized_usd": money_str(self.authorized_usd),
            "executed_usd": money_str(self.executed_usd),
            "pending_usd": money_str(self.pending_usd),
            "approved_unsubmitted_usd": money_str(self.approved_unsubmitted_usd),
            "proposed_usd": money_str(self.proposed_usd),
            "committed_usd": money_str(self.committed_usd),
            "total_usd": money_str(self.total_usd),
            "remaining_usd": money_str(self.remaining_usd),
            "within_authorization": self.within_authorization,
            "overage_usd": money_str(self.overage_usd),
        }


def combined_exposure(
    reconciliation: Any,
    approved_unsubmitted_usd: Decimal = ZERO,
    proposed_usd: Decimal = ZERO,
) -> CombinedExposure:
    """Build the four-way ceiling from a :class:`~src.reconciliation.Reconciliation`.

    ``reconciliation`` supplies executed and pending from the broker — the two
    quantities that are authoritative. The caller supplies the two the broker
    cannot see: sibling legs already approved but not yet submitted, and legs
    still only proposed.
    """
    return CombinedExposure(
        month=reconciliation.month,
        authorized_usd=reconciliation.authorized_usd,
        executed_usd=reconciliation.broker_filled_usd,
        pending_usd=reconciliation.broker_pending_usd,
        approved_unsubmitted_usd=Decimal(approved_unsubmitted_usd).quantize(CENTS),
        proposed_usd=Decimal(proposed_usd).quantize(CENTS),
    )


def assert_plan_within_authorization(exposure: CombinedExposure) -> None:
    """Raise unless executed + pending + approved + proposed still fits.

    Like :func:`src.reconciliation.assert_order_within_authorization`, nothing
    calls this today, because nothing executes. It exists so the multi-leg rule
    is written down and tested before it could ever matter.
    """
    if not exposure.within_authorization:
        raise AllocationError(
            "combined exposure of %s (executed %s + pending %s + approved %s + proposed %s) "
            "exceeds the %s authorization for %s by %s"
            % (
                usd(exposure.total_usd),
                usd(exposure.executed_usd),
                usd(exposure.pending_usd),
                usd(exposure.approved_unsubmitted_usd),
                usd(exposure.proposed_usd),
                usd(exposure.authorized_usd),
                exposure.month,
                usd(exposure.overage_usd),
            )
        )


def sibling_reservations_usd(
    approvals: Dict[str, Any],
    execution_states: Dict[str, str],
    month: str,
    exclude_decision_id: str = "",
) -> Decimal:
    """Dollars held by sibling legs that are approved but not yet submitted.

    These are invisible to the broker and invisible to the local ledger (which
    only records *executed* purchases), so without this they would be double
    spent. Anything already submitted is excluded — the broker reports that
    itself, and counting it twice would understate the remaining authorization.

    A decision also stops reserving once it enters a **released** state — any
    terminal state, or ``REEVALUATION_REQUIRED``, which means the priced
    premise the approval rested on is gone and only a *new* decision with a
    *new* approval could act on the asset. Holding dollars against a decision
    that can never be submitted quietly shrinks the month's authorization for
    no reason anyone can act on, while nothing is pending and no order exists.
    """
    from src.execution import IN_FLIGHT_STATES, RELEASED_STATES  # local: avoid cycle

    counted = ZERO
    for decision_id, record in (approvals or {}).items():
        if decision_id == exclude_decision_id:
            continue
        record_month = getattr(record, "month", None)
        if record_month is None and isinstance(record, dict):
            record_month = record.get("month")
        if record_month != month:
            continue
        state = (execution_states or {}).get(decision_id, "APPROVED")
        if state in IN_FLIGHT_STATES or state in RELEASED_STATES:
            continue
        amount = getattr(record, "max_amount_usd", None)
        if amount is None and isinstance(record, dict):
            amount = record.get("max_amount_usd")
        if amount is None:
            continue
        try:
            counted += parse_money(amount, "max_amount_usd")
        except MoneyError:
            continue
    return counted.quantize(CENTS)
