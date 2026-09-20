"""Portfolio history — normalizing the broker's record of what was actually done.

## What this module is for

Three years of decisions exist in the broker's records. This module turns them
into a normalized, provenance-tagged history that a later evaluation can learn
from, and it encodes the things that were *verified* about that data rather than
assumed.

Four findings from the capability audit are baked in here, because each one
silently corrupts the analysis if forgotten:

1. **Order history is authoritative for buys and sells.** The realized-P&L
   endpoints disagree with it: ``get_pnl_trade_history`` returns an empty list
   for every span on every account, and ``get_realized_pnl`` reports zero trades
   in every bucket, yet the crypto order record contains real sells. Where they
   conflict, the order record wins and the conflict is recorded as an anomaly.
2. **Order prices are not split-adjusted; tax lots are.** An order placed
   before a 10:1 split shows ``$500.00`` while its lot shows ``$50.00``. Any basis or
   entry-quality analysis must read lots. :func:`basis_drift` accepts lots only
   and there is deliberately no order-priced equivalent.
3. **``open_tran_type`` is the only transfer signal available.** No deposit,
   withdrawal, ACATS or crypto-transfer tool exists in the MCP surface. A lot
   whose ``open_tran_type`` is not ``buy`` (``deposit``, ``baselinetaxlot``)
   arrived without a purchase, and is tagged ``NON_PURCHASE_LOT``.
4. **Attribution is asymmetric.** Equity orders carry ``placed_agent``
   (``user`` / ``agentic`` / ``recurring`` / ``drip``); crypto orders carry no
   attribution field at all — the ``initiator_type`` named in the tool's own
   guide is never returned. Crypto therefore normalizes to ``UNKNOWN`` unless
   this repository's own decision log claims the order.

## Privacy

``state/portfolio_history.json`` is personal financial history. It is
gitignored, and account identifiers are stored **masked** (``••••6789``).
:func:`unmasked_account_numbers` scans a serialized history and is used by the
ingest script to refuse to write a file that leaked one.

Pure functions over plain data. No I/O, no network, no subprocess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Provenance — who did this, and how do we know
# --------------------------------------------------------------------------

#: A human placed it. Equity: ``placed_agent`` in {user, recurring, drip}.
MANUAL_ACTION = "MANUAL_ACTION"
#: This agent placed it. Equity: ``placed_agent == "agentic"``. Crypto: only
#: when this repository's decision log claims the order.
AGENT_ACTION = "AGENT_ACTION"
#: A tax lot that did not arrive by purchase — a transfer, deposit, reward, or
#: a baseline lot reconstructed at account migration.
NON_PURCHASE_LOT = "NON_PURCHASE_LOT"
#: Genuinely unknown. Every pre-agent crypto order lands here, because the
#: broker exposes no attribution field for crypto.
UNKNOWN = "UNKNOWN"

PROVENANCES = (MANUAL_ACTION, AGENT_ACTION, NON_PURCHASE_LOT, UNKNOWN)

#: ``placed_agent`` values that mean a human or a human's standing instruction.
MANUAL_PLACED_AGENTS = ("user", "recurring", "drip", "dca")
#: ``placed_agent`` values that mean this agent.
AGENT_PLACED_AGENTS = ("agentic", "mcp", "agent")

#: ``open_tran_type`` values that represent an actual purchase.
PURCHASE_TRAN_TYPES = ("buy", "purchase")

SOURCE_ORDER_RECORD = "ORDER_RECORD"
SOURCE_TAX_LOT = "TAX_LOT"
SOURCE_DECISION_LOG = "DECISION_LOG"

ASSET_EQUITY = "EQUITY"
ASSET_CRYPTO = "CRYPTO"

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------

_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?")
# A run of 9+ digits that is NOT part of a decimal number. The lookarounds
# matter: a crypto average_price legitimately carries 18 decimal places
# (60000.987654321098765432 is a real value from the audited data), and its
# fractional part is not an account identifier.
_LONG_DIGITS_RE = re.compile(r"(?<![\d.])\d{9,}(?![\d.])")


def mask_account(number: Any) -> str:
    """``123456789`` -> ``••••6789``. Never returns the full identifier."""
    digits = re.sub(r"\D", "", str(number or ""))
    if not digits:
        return "••••????"
    return "••••" + digits[-4:]


def unmasked_account_numbers(serialized: str) -> List[str]:
    """Long digit runs left in a serialized history — a privacy leak.

    UUIDs and ISO timestamps are stripped first, since both legitimately carry
    digit runs and neither identifies an account. What remains and still looks
    like a 9-or-more digit identifier is treated as a leak.
    """
    scrubbed = _UUID_RE.sub(" ", serialized or "")
    scrubbed = _ISO_RE.sub(" ", scrubbed)
    return sorted(set(_LONG_DIGITS_RE.findall(scrubbed)))


# --------------------------------------------------------------------------
# Money / dates
# --------------------------------------------------------------------------


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _iso_date(value: Any) -> Optional[str]:
    """The ``YYYY-MM-DD`` prefix of a timestamp or date string."""
    text = str(value or "").strip()
    if len(text) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    return None


def parse_day(value: Any) -> Optional[date]:
    iso = _iso_date(value)
    if iso is None:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass
class OrderRecord:
    """One historical order, normalized across the two asset classes."""

    key: str = ""
    masked_account: str = ""
    asset_class: str = ""
    symbol: str = ""
    side: str = ""
    state: str = ""
    executed_on: Optional[str] = None
    executed_at: Optional[str] = None
    dollars: Optional[str] = None
    quantity: Optional[str] = None
    price: Optional[str] = None
    provenance: str = UNKNOWN
    source: str = SOURCE_ORDER_RECORD
    #: Why the provenance is what it is — kept so a later reader can audit it.
    provenance_basis: str = ""
    #: True when this price is NOT split-adjusted (all order prices are not).
    split_adjusted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class LotRecord:
    """One open tax lot. Split-adjusted, and the basis source of truth."""

    key: str = ""
    masked_account: str = ""
    symbol: str = ""
    open_date: Optional[str] = None
    quantity: Optional[str] = None
    cost_per_share: Optional[str] = None
    tax_cost_basis: Optional[str] = None
    term: str = ""
    open_tran_type: str = ""
    provenance: str = UNKNOWN
    source: str = SOURCE_TAX_LOT
    provenance_basis: str = ""
    #: Tax lots ARE split-adjusted. This is the field that makes it explicit.
    split_adjusted: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def equity_order_provenance(placed_agent: Any) -> Tuple[str, str]:
    """Provenance for an equity order, from its ``placed_agent`` field."""
    value = str(placed_agent or "").strip().lower()
    if not value:
        return UNKNOWN, "the order carried no placed_agent value"
    if value in AGENT_PLACED_AGENTS:
        return AGENT_ACTION, "placed_agent=%r" % value
    if value in MANUAL_PLACED_AGENTS:
        return MANUAL_ACTION, "placed_agent=%r" % value
    return UNKNOWN, "placed_agent=%r is not a recognised source" % value


def crypto_order_provenance(
    order_id: str, agent_order_ids: Sequence[str] = ()
) -> Tuple[str, str]:
    """Provenance for a crypto order.

    Crypto orders carry **no** attribution field — not in list mode and not in
    single-order mode, despite ``initiator_type`` being named in the tool's own
    guide. So the only way an order can be attributed to this agent is for this
    repository's decision log to claim it. Everything else is ``UNKNOWN``, and
    saying so is the honest answer rather than assuming a human.
    """
    if order_id and order_id in set(agent_order_ids):
        return AGENT_ACTION, "claimed by logs/decisions.jsonl"
    return (
        UNKNOWN,
        "crypto orders expose no attribution field; not claimed by this "
        "repository's decision log",
    )


def normalize_equity_order(raw: Dict[str, Any], account: Any) -> OrderRecord:
    provenance, basis = equity_order_provenance(raw.get("placed_agent"))
    dollars = (raw.get("dollar_based_amount") or {}).get("amount")
    executed_at = raw.get("last_transaction_at") or raw.get("created_at")
    return OrderRecord(
        key="eq:" + str(raw.get("id") or ""),
        masked_account=mask_account(account),
        asset_class=ASSET_EQUITY,
        symbol=str(raw.get("symbol") or "").upper(),
        side=str(raw.get("side") or "").lower(),
        state=str(raw.get("state") or "").lower(),
        executed_on=_iso_date(executed_at),
        executed_at=str(executed_at) if executed_at else None,
        dollars=str(_dec(dollars)) if _dec(dollars) is not None else None,
        quantity=str(_dec(raw.get("cumulative_quantity") or raw.get("quantity")) or ""),
        price=str(_dec(raw.get("average_price")) or ""),
        provenance=provenance,
        provenance_basis=basis,
        split_adjusted=False,
    )


def normalize_crypto_order(
    raw: Dict[str, Any], account: Any, agent_order_ids: Sequence[str] = ()
) -> OrderRecord:
    order_id = str(raw.get("id") or "")
    provenance, basis = crypto_order_provenance(order_id, agent_order_ids)
    symbol = str(raw.get("currency_code") or "").upper()
    executed_at = raw.get("updated_at") or raw.get("created_at")
    # `entered_price` is the DOLLAR AMOUNT entered, not a price. The executed
    # notional is the honest dollar figure.
    dollars = raw.get("total_executed_notional") or raw.get("rounded_executed_notional")
    return OrderRecord(
        key="cx:" + order_id,
        masked_account=mask_account(account),
        asset_class=ASSET_CRYPTO,
        symbol=symbol + "-USD" if symbol and "-" not in symbol else symbol,
        side=str(raw.get("side") or "").lower(),
        state=str(raw.get("state") or "").lower(),
        executed_on=_iso_date(executed_at),
        executed_at=str(executed_at) if executed_at else None,
        dollars=str(_dec(dollars)) if _dec(dollars) is not None else None,
        quantity=str(_dec(raw.get("cumulative_quantity") or raw.get("quantity")) or ""),
        price=str(_dec(raw.get("average_price")) or ""),
        provenance=provenance,
        provenance_basis=basis,
        split_adjusted=False,
    )


def normalize_tax_lot(raw: Dict[str, Any], account: Any, symbol: str) -> LotRecord:
    tran_type = str(raw.get("open_tran_type") or "").strip().lower()
    if tran_type in PURCHASE_TRAN_TYPES:
        provenance, basis = UNKNOWN, (
            "a purchase lot; attribution comes from the matching order record, "
            "not from the lot"
        )
    else:
        provenance, basis = NON_PURCHASE_LOT, (
            "open_tran_type=%r — this lot did not arrive by purchase. No "
            "deposit/withdrawal/ACATS tool exists to say how." % tran_type
        )
    # The lot's own order_id is deliberately dropped: it is a different
    # identifier space from get_equity_orders.id (numeric vs UUID), joins to
    # nothing, and is a long digit run in a file that must carry none.
    return LotRecord(
        key="lot:%s:%s" % (symbol.upper(), raw.get("open_lot_id") or ""),
        masked_account=mask_account(account),
        symbol=symbol.upper(),
        open_date=_iso_date(raw.get("open_date")),
        quantity=str(_dec(raw.get("quantity")) or ""),
        cost_per_share=str(_dec(raw.get("cost_per_share")) or ""),
        tax_cost_basis=str(_dec(raw.get("tax_cost_basis")) or ""),
        term=str(raw.get("term") or "").lower(),
        open_tran_type=tran_type,
        provenance=provenance,
        provenance_basis=basis,
        split_adjusted=True,
    )


# --------------------------------------------------------------------------
# Merge — the incremental-update guard
# --------------------------------------------------------------------------

#: Fields on an existing record that a later ingest may never change. A
#: historical fact does not get rewritten; if the broker reports it differently,
#: that is an anomaly to surface, not an edit to apply.
IMMUTABLE_RECORD_FIELDS = (
    "masked_account", "asset_class", "symbol", "side",
    "executed_on", "dollars", "quantity", "open_date", "cost_per_share",
    "tax_cost_basis", "open_tran_type",
)


def _by_key(records: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(r.get("key")): r for r in records if r.get("key")}


def merge_records(
    existing: Sequence[Dict[str, Any]], incoming: Sequence[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Merge an incremental ingest into an existing record set.

    Fails loudly rather than losing history. Three rules:

    * a record present before must still be present after (no silent drops);
    * an immutable field may not change value (no silent rewrites);
    * anything genuinely new is added.

    Returns ``(merged, problems)``. A non-empty ``problems`` list means the
    caller must not write the result.
    """
    problems: List[str] = []
    before = _by_key(existing)
    arriving = _by_key(incoming)

    merged = dict(before)
    for key, record in arriving.items():
        if key not in merged:
            merged[key] = record
            continue
        old = merged[key]
        for field_name in IMMUTABLE_RECORD_FIELDS:
            if field_name not in old and field_name not in record:
                continue
            if old.get(field_name) != record.get(field_name):
                problems.append(
                    "%s: %s changed from %r to %r. A historical fact must not be "
                    "rewritten — investigate before ingesting."
                    % (key, field_name, old.get(field_name), record.get(field_name))
                )
        # Provenance may only improve: UNKNOWN -> something specific.
        if old.get("provenance") != record.get("provenance"):
            if old.get("provenance") == UNKNOWN:
                merged[key] = record
            else:
                problems.append(
                    "%s: provenance changed from %r to %r"
                    % (key, old.get("provenance"), record.get("provenance"))
                )

    missing = sorted(set(before) - set(merged))
    if missing:
        problems.append(
            "the merge would drop %d existing record(s): %s"
            % (len(missing), ", ".join(missing[:5]))
        )

    ordered = sorted(
        merged.values(),
        key=lambda r: (str(r.get("executed_on") or r.get("open_date") or ""),
                       str(r.get("key"))),
    )
    return ordered, problems


def validate_history(history: Dict[str, Any]) -> List[str]:
    """Structural problems with a history document."""
    problems: List[str] = []
    if not isinstance(history, dict):
        return ["history is not an object"]

    if history.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            "schema_version is %r, expected %d"
            % (history.get("schema_version"), SCHEMA_VERSION)
        )

    for section in ("orders", "lots"):
        records = history.get(section)
        if not isinstance(records, list):
            problems.append("%s is missing or not a list" % section)
            continue
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                problems.append("%s contains a non-object record" % section)
                continue
            key = record.get("key")
            if not key:
                problems.append("%s contains a record with no key" % section)
            elif key in seen:
                problems.append("%s contains duplicate key %r" % (section, key))
            else:
                seen.add(key)
            if record.get("provenance") not in PROVENANCES:
                problems.append(
                    "%s record %r has provenance %r, not one of %s"
                    % (section, key, record.get("provenance"), list(PROVENANCES))
                )

    for account in history.get("accounts") or []:
        if isinstance(account, dict) and not str(account.get("masked", "")).startswith("••••"):
            problems.append(
                "account entry %r is not masked" % account.get("masked")
            )
    return problems


# --------------------------------------------------------------------------
# Detectors — what the data genuinely supports
# --------------------------------------------------------------------------

INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

#: Below this many closing trades, no sell-timing statement may be made. The
#: audited account has two in three years, so these detectors refuse.
MIN_SELLS_FOR_TIMING = 3


def buys(orders: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [o for o in orders if o.get("side") == "buy" and o.get("state") == "filled"]


def sells(orders: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [o for o in orders if o.get("side") == "sell" and o.get("state") == "filled"]


def by_provenance(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for record in records:
        counts[str(record.get("provenance"))] = counts.get(
            str(record.get("provenance")), 0) + 1
    return counts


def ticket_sizes(orders: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Distribution of dollar ticket sizes — the position-sizing signal."""
    counts: Dict[str, int] = {}
    for order in buys(orders):
        dollars = _dec(order.get("dollars"))
        label = str(dollars.quantize(Decimal("0.01"))) if dollars is not None else "unknown"
        counts[label] = counts.get(label, 0) + 1
    return counts


def sizing_consistency(orders: Sequence[Dict[str, Any]]) -> Optional[Decimal]:
    """Share of buys placed at the single most common ticket size."""
    counts = ticket_sizes(orders)
    total = sum(counts.values())
    if not total:
        return None
    return (Decimal(max(counts.values())) / Decimal(total)).quantize(Decimal("0.001"))


def symbols_opened_by_day(orders: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
    """First-ever buy date per symbol, grouped by day — the sprawl signal."""
    first: Dict[str, str] = {}
    for order in sorted(buys(orders), key=lambda o: str(o.get("executed_on") or "")):
        symbol = str(order.get("symbol") or "")
        if symbol and symbol not in first:
            first[symbol] = str(order.get("executed_on") or "")
    days: Dict[str, List[str]] = {}
    for symbol, day in first.items():
        days.setdefault(day, []).append(symbol)
    for day in days:
        days[day].sort()
    return days


def sprawl_events(
    orders: Sequence[Dict[str, Any]], threshold: int = 3
) -> List[Dict[str, Any]]:
    """Days on which ``threshold`` or more brand-new positions were opened."""
    events = []
    for day, symbols in sorted(symbols_opened_by_day(orders).items()):
        if len(symbols) >= threshold:
            events.append({"date": day, "count": len(symbols), "symbols": symbols})
    return events


def position_count_over_time(orders: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Cumulative distinct-symbol count after each new position is opened."""
    series = []
    running = 0
    for day, symbols in sorted(symbols_opened_by_day(orders).items()):
        running += len(symbols)
        series.append({"date": day, "positions": running, "added": symbols})
    return series


class SplitUnsafeError(ValueError):
    """Raised when split-sensitive analysis is handed order-priced data."""


def basis_drift(lots: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """How a position's cost basis moved across its lots, over time.

    **Lots only.** Order prices are not split-adjusted and produce the opposite
    answer on a split: NVDA's raw orders read as a 59% *decline* in entry price
    where the lots show a ~311% *rise*. Handing this function order-priced
    records raises rather than quietly misleading.
    """
    for lot in lots:
        if lot.get("source") != SOURCE_TAX_LOT or not lot.get("split_adjusted"):
            raise SplitUnsafeError(
                "basis_drift accepts tax-lot records only. Order prices are not "
                "split-adjusted and invert the answer across a split."
            )

    dated = [l for l in lots if l.get("open_date") and _dec(l.get("cost_per_share"))]
    if len(dated) < 2:
        return {"verdict": INSUFFICIENT_DATA, "lots": len(dated)}

    ordered = sorted(dated, key=lambda l: str(l.get("open_date")))
    first = _dec(ordered[0].get("cost_per_share"))
    last = _dec(ordered[-1].get("cost_per_share"))
    quantities = [_dec(l.get("quantity")) or Decimal(0) for l in ordered]
    bases = [_dec(l.get("tax_cost_basis")) or Decimal(0) for l in ordered]
    total_qty = sum(quantities)
    weighted = (sum(bases) / total_qty) if total_qty else None

    direction = "FLAT"
    if first and last:
        if last > first:
            direction = "AVERAGED_UP"
        elif last < first:
            direction = "AVERAGED_DOWN"

    return {
        "verdict": direction,
        "lots": len(ordered),
        "first_date": ordered[0].get("open_date"),
        "last_date": ordered[-1].get("open_date"),
        "first_cost_per_share": str(first),
        "last_cost_per_share": str(last),
        "weighted_average_cost": str(weighted.quantize(Decimal("0.0001"))) if weighted else None,
        "total_basis": str(sum(bases).quantize(Decimal("0.01"))),
        "split_adjusted": True,
    }


def non_purchase_lots(lots: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lots that arrived without a purchase — the only transfer signal there is."""
    return [l for l in lots if l.get("provenance") == NON_PURCHASE_LOT]


def sell_timing(orders: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Refuses unless there are enough closing trades to say anything.

    'Sold too early' and 'held too long' are the two patterns this dataset
    cannot support: with two sells in three years there is no distribution to
    reason about. Returning INSUFFICIENT_DATA is the finding.
    """
    closed = sells(orders)
    if len(closed) < MIN_SELLS_FOR_TIMING:
        return {
            "verdict": INSUFFICIENT_DATA,
            "sells": len(closed),
            "minimum": MIN_SELLS_FOR_TIMING,
            "explanation": (
                "sell-timing patterns need at least %d closing trades; this "
                "history has %d. No conclusion about selling too early or "
                "holding too long can be drawn, and none will be."
                % (MIN_SELLS_FOR_TIMING, len(closed))
            ),
        }
    return {"verdict": "ANALYZABLE", "sells": len(closed)}


def cadence_by_month(orders: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Buys and dollars per calendar month."""
    out: Dict[str, Dict[str, Any]] = {}
    for order in buys(orders):
        day = str(order.get("executed_on") or "")
        if len(day) < 7:
            continue
        month = day[:7]
        bucket = out.setdefault(month, {"orders": 0, "dollars": Decimal(0)})
        bucket["orders"] += 1
        bucket["dollars"] += _dec(order.get("dollars")) or Decimal(0)
    return {
        month: {"orders": b["orders"], "dollars": str(b["dollars"].quantize(Decimal("0.01")))}
        for month, b in sorted(out.items())
    }


def coverage_summary(history: Dict[str, Any]) -> Dict[str, Any]:
    """What the history actually covers, per asset class."""
    orders = history.get("orders") or []
    lots = history.get("lots") or []
    out: Dict[str, Any] = {}
    for asset_class in (ASSET_EQUITY, ASSET_CRYPTO):
        subset = [o for o in orders if o.get("asset_class") == asset_class]
        days = sorted(str(o.get("executed_on")) for o in subset if o.get("executed_on"))
        out[asset_class.lower() + "_orders"] = {
            "count": len(subset),
            "buys": len(buys(subset)),
            "sells": len(sells(subset)),
            "first": days[0] if days else None,
            "last": days[-1] if days else None,
            "provenance": by_provenance(subset),
        }
    lot_days = sorted(str(l.get("open_date")) for l in lots if l.get("open_date"))
    out["tax_lots"] = {
        "count": len(lots),
        "symbols": len({str(l.get("symbol")) for l in lots}),
        "first": lot_days[0] if lot_days else None,
        "last": lot_days[-1] if lot_days else None,
        "non_purchase": len(non_purchase_lots(lots)),
    }
    return out
