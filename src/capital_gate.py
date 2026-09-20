"""The deployable-capital gate: should today's evaluation run at all?

A scheduled weekday evaluation is the expensive part of this system. It is also
pointless on a day when nothing could be bought no matter what it concluded,
and there are exactly two such days:

* **the authorization is spent** — the month's budget is committed or reserved,
  so even a perfect BUY could not be funded from it;
* **the account is not funded** — authorization remains, but the Agentic
  account's *settled* cash cannot cover the smallest purchase the broker will
  accept.

Both are decided by arithmetic over data the repository already has, so the gate
runs before Claude is invoked and costs nothing. That is the point: a cheap,
deterministic check in front of an expensive, non-deterministic one.

    deployable_capital = min(remaining_authorization, settled_cash)

The two skips are **not** the same and must not be reported as though they were.
An exhausted authorization is a success — the month did its job, and the right
response is to wait for the next one. Unfunded is a request: the owner has
authorization they cannot use, and only they can fix it.

Nothing here decides what to buy, and nothing here can authorize anything. It
decides only whether asking the question is worth the money.

Pure functions over plain data. No I/O, no network, no subprocess. The caller
gathers the broker side with read-only tools, exactly as ``src/execution.py``
requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .models import CENTS, ZERO, MoneyError, money_str, parse_money, usd

# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------

#: Run the evaluation: there is money and permission to use it.
GATE_EVALUATE = "EVALUATE"
#: Skip, quietly. The month's authorization is spent. This is a success.
GATE_AUTHORIZATION_EXHAUSTED = "AUTHORIZATION_EXHAUSTED"
#: Skip, and say so. Authorization remains but settled cash cannot fund it.
GATE_FUNDING_REQUIRED = "FUNDING_REQUIRED"
#: Skip, and say so loudly. The broker could not be read.
GATE_BROKER_UNREADABLE = "BROKER_UNREADABLE"

GATE_OUTCOMES = (
    GATE_EVALUATE,
    GATE_AUTHORIZATION_EXHAUSTED,
    GATE_FUNDING_REQUIRED,
    GATE_BROKER_UNREADABLE,
)

#: Outcomes that skip the expensive Claude invocation.
SKIP_OUTCOMES = frozenset(
    {GATE_AUTHORIZATION_EXHAUSTED, GATE_FUNDING_REQUIRED, GATE_BROKER_UNREADABLE}
)

#: Outcomes that are a normal, healthy state of the world rather than a problem.
QUIET_OUTCOMES = frozenset({GATE_AUTHORIZATION_EXHAUSTED})

#: Robinhood's minimum dollar-based equity order. Below this nothing is
#: purchasable, so remaining capital under it is not deployable in practice.
MIN_DEPLOYABLE_USD = Decimal("1.00")

#: How old a broker snapshot may be before the gate refuses to believe it.
#:
#: This exists because the first version of this gate read a *persisted*
#: ``broker.json`` that nothing regenerated — a file written by hand during a
#: live-purchase attempt. A cash figure from days ago is not a cash figure: it
#: would have let an exhausted account run expensive evaluations forever, or
#: suppressed evaluation forever after a deposit that the file never learned
#: about. Both failures are silent, which is what makes them dangerous.
#:
#: Two hours is comfortably longer than a morning refresh takes and far shorter
#: than the gap between runs, so a snapshot that survives into the next day is
#: always rejected.
MAX_SNAPSHOT_AGE_SECONDS = 2 * 60 * 60

#: Sentinel meaning "do not check cash in this phase". Distinct from None, which
#: means "cash was supposed to be known and is not" and fails closed.
_UNCHECKED_CASH = object()


@dataclass
class GateDecision:
    """Why today's evaluation will or will not run."""

    outcome: str
    month: str = ""
    remaining_authorization_usd: Decimal = ZERO
    settled_cash_usd: Optional[Decimal] = None
    deployable_usd: Decimal = ZERO
    shortfall_usd: Decimal = ZERO
    reason: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def should_evaluate(self) -> bool:
        return self.outcome == GATE_EVALUATE

    @property
    def is_quiet(self) -> bool:
        """True when this outcome should not generate a notification."""
        return self.outcome in QUIET_OUTCOMES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "month": self.month,
            "remaining_authorization_usd": money_str(self.remaining_authorization_usd),
            "settled_cash_usd": (
                money_str(self.settled_cash_usd)
                if self.settled_cash_usd is not None
                else None
            ),
            "deployable_usd": money_str(self.deployable_usd),
            "shortfall_usd": money_str(self.shortfall_usd),
            "reason": self.reason,
            "notes": list(self.notes),
        }


def _money_or_none(value: Any, label: str) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return parse_money(value, label)
    except MoneyError:
        return None


def settled_cash_from_snapshot(snapshot: Any) -> Optional[Decimal]:
    """Settled cash from a read-only broker snapshot, or None if unknowable.

    ``buying_power`` is deliberately **not** a fallback. On a ``limited_margin``
    account buying power exceeds settled cash by the margin the account is
    entitled to borrow, and borrowing is prohibited. A snapshot that reports
    only buying power reports nothing this function will use.
    """
    if snapshot is None:
        return None
    raw = snapshot.get("cash_usd") if isinstance(snapshot, dict) else getattr(
        snapshot, "cash_usd", None)
    cash = _money_or_none(raw, "cash_usd")
    if cash is None:
        return None

    unsettled_raw = (
        snapshot.get("unsettled_funds_usd") if isinstance(snapshot, dict)
        else getattr(snapshot, "unsettled_funds_usd", None)
    )
    unsettled = _money_or_none(unsettled_raw, "unsettled_funds_usd")
    if unsettled is not None and unsettled > ZERO:
        # Robinhood reports `cash` inclusive of funds that have not settled.
        # Only the settled remainder may fund a purchase.
        cash = cash - unsettled
    return max(cash, ZERO).quantize(CENTS)


def snapshot_age_seconds(snapshot: Any, now: Optional[datetime] = None) -> Optional[float]:
    """Seconds since the snapshot was taken, or None if it does not say.

    Reads ``as_of``. A snapshot that carries no timestamp cannot be aged and is
    therefore never trusted — the caller treats None as unusable.
    """
    if not isinstance(snapshot, dict):
        return None
    raw = snapshot.get("as_of")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        taken = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - taken).total_seconds()


#: Fields a refreshed snapshot must actually carry to be usable.
REQUIRED_SNAPSHOT_FIELDS = ("as_of", "account_is_agentic", "cash_usd")


def refresh_problems(
    snapshot: Any,
    mtime: Optional[float],
    started_at: float,
    now: Optional[datetime] = None,
    max_age_seconds: float = MAX_SNAPSHOT_AGE_SECONDS,
) -> List[str]:
    """Why a refresh did NOT produce a usable snapshot. Empty means it did.

    A refresh script that only checks "does a parseable snapshot exist" will
    report success against a file written days ago, because a subprocess that
    exits 0 having written nothing leaves the previous file untouched. That
    happened: a three-day-old snapshot was announced as ``snapshot refreshed``,
    and only the capital gate's own freshness check stopped it being used.

    So the question here is not "is there a snapshot" but "did *this
    invocation* produce one". ``mtime`` must post-date ``started_at``; the
    content must be fresh on its own ``as_of``; and the fields the gate depends
    on must be present. Pure over values the caller has already read, so the
    whole thing is testable without a broker or a subprocess.
    """
    problems: List[str] = []

    if mtime is None:
        problems.append(
            "no snapshot file exists after the refresh: the subprocess wrote "
            "nothing, so there is no new broker reading to use")
        return problems

    if mtime < started_at:
        problems.append(
            "the snapshot predates this invocation (written %.0fs before it "
            "started), so it is a leftover from an earlier run rather than a "
            "fresh reading" % (started_at - mtime))

    if not isinstance(snapshot, dict):
        problems.append("the snapshot is not a JSON object")
        return problems

    missing = [f for f in REQUIRED_SNAPSHOT_FIELDS if f not in snapshot]
    if missing:
        problems.append("the snapshot is missing %s" % ", ".join(missing))

    age = snapshot_age_seconds(snapshot, now)
    if age is None:
        problems.append("the snapshot carries no usable as_of timestamp")
    elif age > max_age_seconds:
        problems.append(
            "the snapshot's as_of is %.0fs old, over the %.0fs limit"
            % (age, max_age_seconds))
    elif age < -60:
        problems.append("the snapshot's as_of is in the future")

    if snapshot.get("account_is_agentic") is not True:
        problems.append("the snapshot does not identify the Agentic account")

    if settled_cash_from_snapshot(snapshot) is None:
        problems.append("the snapshot carries no usable cash_usd")

    return problems


def authorization_only_gate(
    month: str,
    remaining_authorization_usd: Any,
    reservations_usd: Any = ZERO,
    min_deployable_usd: Decimal = MIN_DEPLOYABLE_USD,
) -> GateDecision:
    """Phase one: can this month buy anything at all, ignoring the account?

    Deliberately needs **no broker data**. An exhausted authorization is decided
    from the ledger alone, which is what keeps the commonest skip completely
    free — no snapshot refresh, no Claude invocation, nothing.

    Returns AUTHORIZATION_EXHAUSTED, or EVALUATE meaning "authorization remains;
    now go and find out whether the account can fund it".
    """
    return evaluate_gate(
        month, remaining_authorization_usd,
        snapshot=_UNCHECKED_CASH,
        reservations_usd=reservations_usd,
        min_deployable_usd=min_deployable_usd,
    )


def evaluate_gate(
    month: str,
    remaining_authorization_usd: Any,
    snapshot: Any = None,
    reservations_usd: Any = ZERO,
    min_deployable_usd: Decimal = MIN_DEPLOYABLE_USD,
    now: Optional[datetime] = None,
    max_snapshot_age_seconds: float = MAX_SNAPSHOT_AGE_SECONDS,
) -> GateDecision:
    """Decide whether today's evaluation is worth running.

    ``reservations_usd`` covers dollars already spoken for by approved-but-
    unsubmitted legs — invisible to both the broker and the local ledger, and
    therefore subtracted before anything is called deployable.
    """
    try:
        remaining = parse_money(remaining_authorization_usd, "remaining_authorization_usd")
    except MoneyError as exc:
        return GateDecision(
            outcome=GATE_BROKER_UNREADABLE,
            month=month,
            reason="the remaining monthly authorization could not be read: %s" % exc,
        )

    try:
        reserved = parse_money(reservations_usd, "reservations_usd")
    except MoneyError:
        reserved = ZERO

    available = max(remaining - reserved, ZERO).quantize(CENTS)
    decision = GateDecision(
        outcome=GATE_BROKER_UNREADABLE,   # replaced below; never returned as-is
        month=month,
        remaining_authorization_usd=available,
    )
    if reserved > ZERO:
        decision.notes.append(
            "%s of this month's authorization is reserved by approved-but-unsubmitted "
            "legs and is not deployable" % usd(reserved)
        )

    # --- 1. is there authorization left at all? --------------------------
    if available < min_deployable_usd:
        decision.outcome = GATE_AUTHORIZATION_EXHAUSTED
        decision.deployable_usd = ZERO
        decision.reason = (
            "the %s authorization is spent: %s remains, below the %s broker minimum. "
            "Evaluation is skipped until the next month's authorization begins. "
            "A fully-used month is a normal outcome, not a failure."
            % (month, usd(available), usd(min_deployable_usd))
        )
        return decision

    # --- 1a. phase one stops here -----------------------------------------
    if snapshot is _UNCHECKED_CASH:
        decision.outcome = GATE_EVALUATE
        decision.deployable_usd = available
        decision.reason = (
            "%s of %s authorization remains. Whether the account can fund it has "
            "not been checked in this phase." % (usd(available), month)
        )
        decision.notes.append("cash was not checked: authorization-only phase")
        return decision

    # --- 2. is the broker data fresh enough to believe? -------------------
    age = snapshot_age_seconds(snapshot, now)
    if snapshot is not None and (age is None or age > max_snapshot_age_seconds):
        decision.outcome = GATE_BROKER_UNREADABLE
        decision.reason = (
            "the broker snapshot is %s. A cash figure that old is not a cash "
            "figure: believing it would either run expensive evaluations against "
            "an empty account or suppress them forever after a deposit it never "
            "learned about. Refresh the snapshot and re-run."
            % ("undated" if age is None else "%.0f seconds old, over the %.0f second limit"
               % (age, max_snapshot_age_seconds))
        )
        return decision

    # --- 3. can the broker even be read? ---------------------------------
    cash = settled_cash_from_snapshot(snapshot)
    decision.settled_cash_usd = cash
    if cash is None:
        decision.outcome = GATE_BROKER_UNREADABLE
        decision.reason = (
            "settled cash could not be determined from the broker snapshot. A failed "
            "or missing read is never treated as a funded account; the evaluation is "
            "skipped and the condition reported."
        )
        return decision

    # --- 4. is the account funded enough to act on that authorization? ---
    deployable = min(available, cash).quantize(CENTS)
    decision.deployable_usd = deployable
    if deployable < min_deployable_usd:
        decision.outcome = GATE_FUNDING_REQUIRED
        decision.shortfall_usd = (available - cash).quantize(CENTS)
        decision.reason = (
            "%s of %s authorization remains but the Agentic account holds only %s in "
            "settled cash, so nothing could be purchased today. Deposit at least %s to "
            "use this month's authorization. Buying power is not a substitute: "
            "execution is cash-only."
            % (usd(available), month, usd(cash), usd(decision.shortfall_usd))
        )
        return decision

    decision.outcome = GATE_EVALUATE
    decision.reason = (
        "%s is deployable today (%s authorization, %s settled cash)."
        % (usd(deployable), usd(available), usd(cash))
    )
    if cash < available:
        decision.notes.append(
            "settled cash %s is below the remaining authorization %s, so the "
            "evaluation should size against cash rather than authorization"
            % (usd(cash), usd(available))
        )
    return decision
