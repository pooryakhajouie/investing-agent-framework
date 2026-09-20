"""Broker reconciliation — the future live budget source of truth.

**This module is not wired to any execution path, because none exists.** It is
built and tested now so that a future live version has exactly one audited place
to answer the question: *how much am I actually still authorized to spend?*

The rule it enforces:

    local state/budget.json is NEVER trusted on its own.

Before any live purchase, the executor must reconcile the local ledger against
what the broker actually shows for the agentic account this calendar month, and
take the **most conservative** result:

    reconciled_committed = max(local_committed, broker_filled + broker_pending)
    safe_remaining       = authorized - reconciled_committed

Worked example from the policy::

    authorized                 $25.00
    completed purchases        $10.00
    pending authorized order    $5.00
    -> maximum additional order $10.00

A crash, a manual purchase made in the app, a partial fill, or a stale local
file must never make additional authorization appear available. Every failure
mode resolves toward spending less, and anything unparseable raises
:class:`ReconciliationError` rather than being skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import CENTS, ZERO, MoneyError, money_str, parse_money
from .state import BudgetState, Config, current_month, utc_now

# Order states, lowercased. Anything not listed is unknown and fails closed.
OPEN_STATES = frozenset(
    {"new", "queued", "confirmed", "unconfirmed", "partially_filled", "pending"}
)
FILLED_STATES = frozenset({"filled"})
DEAD_STATES = frozenset({"cancelled", "canceled", "rejected", "failed", "voided", "expired"})
KNOWN_STATES = OPEN_STATES | FILLED_STATES | DEAD_STATES

BUY_SIDES = frozenset({"buy", "buy_to_open"})

_MONTH_RE = re.compile(r"^(\d{4}-\d{2})")


class ReconciliationError(Exception):
    """Raised when broker data cannot be trusted. Always fails closed."""


# --------------------------------------------------------------------------
# Normalized order record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderRecord:
    """One broker order, reduced to what the budget cares about."""

    order_id: str
    symbol: str
    side: str
    state: str
    created_at: str
    asset_class: str
    placed_agent: str
    notional_requested: Decimal
    notional_executed: Decimal
    # The client idempotency key, when the broker echoes it back. This is how a
    # submitted order is matched to the ticket that produced it.
    ref_id: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_filled(self) -> bool:
        return self.state in FILLED_STATES

    @property
    def is_dead(self) -> bool:
        return self.state in DEAD_STATES

    @property
    def committed_usd(self) -> Decimal:
        """The conservative dollar amount this order ties up.

        Open orders reserve their **full** requested notional even when only
        partly filled, because the remainder can still execute. Dead orders count
        only what actually executed before they died.
        """
        if self.is_open:
            return max(self.notional_requested, self.notional_executed)
        if self.is_filled:
            return max(self.notional_executed, ZERO)
        return max(self.notional_executed, ZERO)


def _money_or_none(value: Any, label: str) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return parse_money(value, label)
    except MoneyError as exc:
        raise ReconciliationError("order %s: %s" % (label, exc))


def _order_month(created_at: Any) -> str:
    if not isinstance(created_at, str) or not created_at.strip():
        raise ReconciliationError("order is missing a parseable created_at timestamp")
    match = _MONTH_RE.match(created_at.strip())
    if not match:
        raise ReconciliationError("order created_at %r is not ISO-8601" % created_at)
    return match.group(1)


def normalize_order(raw: Any, asset_class: str = "EQUITY") -> OrderRecord:
    """Reduce a broker order payload to an :class:`OrderRecord`.

    Raises :class:`ReconciliationError` on anything it cannot read. Being unable
    to understand an order must never be treated as that order not existing.
    """
    if not isinstance(raw, dict):
        raise ReconciliationError("order must be an object, got %s" % type(raw).__name__)

    order_id = raw.get("id") or raw.get("order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        raise ReconciliationError("order is missing an id")

    state = str(raw.get("state") or "").strip().lower()
    if state not in KNOWN_STATES:
        raise ReconciliationError(
            "order %s has unrecognized state %r; refusing to guess whether it "
            "consumes authorization" % (order_id, raw.get("state"))
        )

    side = str(raw.get("side") or "").strip().lower()
    symbol = str(raw.get("symbol") or "").strip().upper() or "?"
    created_at = str(raw.get("created_at") or "")
    _order_month(created_at)  # validate now, fail closed on garbage

    # --- requested notional ---
    requested: Optional[Decimal] = None
    dollar_based = raw.get("dollar_based_amount")
    if isinstance(dollar_based, dict):
        requested = _money_or_none(dollar_based.get("amount"), "dollar_based_amount.amount")
    elif dollar_based is not None:
        requested = _money_or_none(dollar_based, "dollar_based_amount")
    if requested is None:
        quantity = _money_or_none(raw.get("quantity"), "quantity")
        price = _money_or_none(raw.get("price"), "price")
        if quantity is not None and price is not None:
            requested = quantity * price
    if requested is None:
        requested = ZERO

    # --- executed notional ---
    executed_qty = _money_or_none(raw.get("cumulative_quantity"), "cumulative_quantity")
    avg_price = _money_or_none(raw.get("average_price"), "average_price")
    executed: Optional[Decimal] = None
    if executed_qty is not None and avg_price is not None:
        executed = executed_qty * avg_price
    elif executed_qty is not None and executed_qty == ZERO:
        executed = ZERO
    if executed is None:
        executed = requested if state in FILLED_STATES else ZERO

    if requested < ZERO or executed < ZERO:
        raise ReconciliationError("order %s has a negative notional" % order_id)

    return OrderRecord(
        order_id=order_id,
        symbol=symbol,
        side=side,
        state=state,
        created_at=created_at,
        asset_class=asset_class,
        placed_agent=str(raw.get("placed_agent") or "unknown"),
        notional_requested=requested.quantize(CENTS),
        notional_executed=executed.quantize(CENTS),
        ref_id=(str(raw["ref_id"]) if raw.get("ref_id") else None),
    )


def extract_orders(payload: Any, asset_class: str = "EQUITY") -> List[OrderRecord]:
    """Pull orders out of an MCP response envelope, a bare list, or ``{"orders": []}``.

    ``None`` is an error, not an empty list: "the broker call failed" and "the
    broker reports no orders" must never be conflated.
    """
    if payload is None:
        raise ReconciliationError(
            "%s order data is unavailable; authorization cannot be reconciled" % asset_class
        )
    rows: Any = None
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("orders", "results"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        if rows is None and isinstance(payload.get("data"), dict):
            data = payload["data"]
            for key in ("orders", "results"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
    if rows is None:
        raise ReconciliationError(
            "could not find an order list in the %s payload" % asset_class
        )
    return [normalize_order(row, asset_class) for row in rows]


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


@dataclass
class Reconciliation:
    month: str
    authorized_usd: Decimal
    local_committed_usd: Decimal
    broker_filled_usd: Decimal
    broker_pending_usd: Decimal
    reconciled_committed_usd: Decimal
    safe_remaining_usd: Decimal
    orders_considered: int
    orders_this_month: List[OrderRecord] = field(default_factory=list)
    discrepancies: List[str] = field(default_factory=list)
    blocking: bool = False

    @property
    def broker_committed_usd(self) -> Decimal:
        return (self.broker_filled_usd + self.broker_pending_usd).quantize(CENTS)

    @property
    def max_additional_order_usd(self) -> Decimal:
        """The headline number: the most a new order may be for, right now."""
        return ZERO if self.blocking else self.safe_remaining_usd

    def to_dict(self) -> Dict[str, Any]:
        return {
            "month": self.month,
            "authorized_usd": money_str(self.authorized_usd),
            "local_committed_usd": money_str(self.local_committed_usd),
            "broker_filled_usd": money_str(self.broker_filled_usd),
            "broker_pending_usd": money_str(self.broker_pending_usd),
            "broker_committed_usd": money_str(self.broker_committed_usd),
            "reconciled_committed_usd": money_str(self.reconciled_committed_usd),
            "safe_remaining_usd": money_str(self.safe_remaining_usd),
            "max_additional_order_usd": money_str(self.max_additional_order_usd),
            "orders_considered": self.orders_considered,
            "orders_this_month": len(self.orders_this_month),
            "discrepancies": list(self.discrepancies),
            "blocking": self.blocking,
        }


def reconcile(
    config: Config,
    state: BudgetState,
    equity_orders: Any,
    crypto_orders: Any = None,
    now: Optional[datetime] = None,
    acted_decision_ids: Optional[Sequence[str]] = None,
) -> Reconciliation:
    """Reconcile local budget state against actual broker orders.

    ``equity_orders`` and ``crypto_orders`` accept an MCP response envelope, a
    bare list, or ``None`` — but ``None`` for equities is an error, because a
    failed broker read must not read as "nothing was bought".

    The result is always the most conservative reading available.
    """
    now = now or utc_now()
    month = current_month(now)

    orders: List[OrderRecord] = extract_orders(equity_orders, "EQUITY")
    if crypto_orders is not None:
        orders.extend(extract_orders(crypto_orders, "CRYPTO"))

    discrepancies: List[str] = []
    blocking = False

    # --- duplicate order ids anywhere in the payload ---
    seen_ids = set()
    for order in orders:
        if order.order_id in seen_ids:
            discrepancies.append(
                "duplicate broker order id %s appears more than once" % order.order_id
            )
            blocking = True
        seen_ids.add(order.order_id)

    # --- this calendar month only ---
    this_month = [o for o in orders if _order_month(o.created_at) == month]

    # --- sells in the agentic account are a policy breach, not a credit ---
    sells = [o for o in this_month if o.side and o.side not in BUY_SIDES and not o.is_dead]
    if sells:
        discrepancies.append(
            "the agentic account shows %d non-buy order(s) this month (%s); selling is "
            "forbidden and proceeds must never be treated as new authorization"
            % (len(sells), ", ".join(sorted({o.symbol for o in sells})))
        )
        blocking = True

    buys = [o for o in this_month if not o.side or o.side in BUY_SIDES]

    filled = sum((o.committed_usd for o in buys if o.is_filled), ZERO).quantize(CENTS)
    pending = sum((o.committed_usd for o in buys if o.is_open), ZERO).quantize(CENTS)
    dead_partial = sum((o.committed_usd for o in buys if o.is_dead), ZERO).quantize(CENTS)
    if dead_partial > ZERO:
        discrepancies.append(
            "%s executed on orders that were later cancelled or rejected; counted as spent"
            % money_str(dead_partial)
        )
    filled = (filled + dead_partial).quantize(CENTS)

    broker_committed = (filled + pending).quantize(CENTS)

    # --- local ledger ---
    local_committed = state.committed_usd.quantize(CENTS)
    if state.month != month:
        discrepancies.append(
            "local budget state is for %s but the current month is %s; local committed "
            "spending is treated as $0.00 for %s and only broker data is trusted"
            % (state.month, month, month)
        )
        local_committed = ZERO

    if broker_committed > local_committed:
        discrepancies.append(
            "broker shows %s committed this month but the local ledger records %s — a "
            "manual purchase, an unrecorded fill, or a crash between order and write. "
            "Using the broker figure."
            % (money_str(broker_committed), money_str(local_committed))
        )
    elif local_committed > broker_committed:
        discrepancies.append(
            "local ledger records %s committed but the broker shows only %s; using the "
            "local figure until the difference is explained"
            % (money_str(local_committed), money_str(broker_committed))
        )

    reconciled = max(local_committed, broker_committed).quantize(CENTS)

    authorized = config.monthly_budget_usd.quantize(CENTS)
    if state.authorized_budget_usd.quantize(CENTS) > authorized:
        discrepancies.append(
            "local state claims a %s authorization above the configured %s; using the "
            "configured value"
            % (money_str(state.authorized_budget_usd), money_str(authorized))
        )

    if reconciled > authorized:
        discrepancies.append(
            "reconciled spending of %s already exceeds the %s monthly authorization; no "
            "further purchase is permitted this month"
            % (money_str(reconciled), money_str(authorized))
        )
        blocking = True

    safe_remaining = (authorized - reconciled).quantize(CENTS)
    if safe_remaining < ZERO:
        safe_remaining = ZERO
    if safe_remaining > authorized:  # pragma: no cover - arithmetic guard
        safe_remaining = authorized

    # --- replay protection across the ledger ---
    acted = list(acted_decision_ids if acted_decision_ids is not None else state.acted_decision_ids)
    if len(set(acted)) != len(acted):
        discrepancies.append("local acted_decision_ids contains duplicates")
        blocking = True

    return Reconciliation(
        month=month,
        authorized_usd=authorized,
        local_committed_usd=local_committed,
        broker_filled_usd=filled,
        broker_pending_usd=pending,
        reconciled_committed_usd=reconciled,
        safe_remaining_usd=safe_remaining,
        orders_considered=len(orders),
        orders_this_month=this_month,
        discrepancies=discrepancies,
        blocking=blocking,
    )


def assert_order_within_authorization(
    reconciliation: Reconciliation, amount: Decimal
) -> None:
    """Final gate a live executor would call. Raises unless the amount fits.

    Nothing calls this today — there is no executor. It exists so the rule is
    written down and tested before it is ever needed.
    """
    if reconciliation.blocking:
        raise ReconciliationError(
            "reconciliation is blocking: %s" % "; ".join(reconciliation.discrepancies)
        )
    if amount <= ZERO:
        raise ReconciliationError("order amount must be greater than zero")
    if amount > reconciliation.max_additional_order_usd:
        raise ReconciliationError(
            "order of %s exceeds the reconciled maximum additional order of %s"
            % (money_str(amount), money_str(reconciliation.max_additional_order_usd))
        )
