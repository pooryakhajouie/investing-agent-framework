"""The bridge from one approved decision to exactly one broker order.

Python cannot call an MCP tool. That is not a limitation to work around — it is
the safety property this design is built on. `src/` never touches the network,
and an AST test enforces it. The bridge is therefore a **file handoff**:

1. ``create_ticket``      builds an immutable, single-use submission ticket
                          carrying the exact order parameters.
2. ``submit_once``        marks the ticket consumed, writes and fsyncs
                          ``SUBMISSION_UNCERTAIN``, and only then calls a
                          :class:`Submitter`.
3. the Submitter          in production, :class:`ManualHandoffSubmitter` writes
                          the parameters to a handoff file and raises. A human
                          or an explicitly permitted session makes **one** MCP
                          call and records the response.
4. ``ingest_response``    moves ``SUBMISSION_UNCERTAIN -> SUBMITTED``.
5. ``reconcile_submission`` asks the broker what actually happened, rather than
                          trusting the immediate response.

The ordering matters. The write-ahead happens **before** the call, so a crash at
any point leaves the record in ``SUBMISSION_UNCERTAIN``, which the executor
refuses to act on until it has been reconciled. Nothing here ever resubmits.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .approval import (
    ApprovalError,
    ApprovalRecord,
    broker_ref_id,
    fingerprint,
    policy_fingerprint,
)
from .execution import (
    BrokerSnapshot,
    ExecutionDisabled,
    ExecutionState,
    PreflightResult,
    build_order_request,
    execution_gate_blockers,
)
from .execution_store import (
    EVENT_FAILURE,
    EVENT_FILL_RECONCILED,
    EVENT_SUBMIT_INTENT,
    EVENT_SUBMITTED,
    ExecutionRecord,
    transition,
    write_audit,
)
from .models import CENTS, ZERO, MoneyError, money_str, parse_money
from .reconciliation import (
    DEAD_STATES,
    FILLED_STATES,
    OPEN_STATES,
    ReconciliationError,
    extract_orders,
)
from .state import REPO_ROOT, Config, atomic_write_json, iso_now, utc_now

TICKETS_PATH = os.path.join(REPO_ROOT, "state", "submission_tickets.json")
HANDOFF_PATH = os.path.join(REPO_ROOT, "state", "pending_submission.json")

# A ticket is short-lived by construction: it embeds a quote, and a quote goes
# stale. Five minutes matches the equity freshness window.
TICKET_TTL_SECONDS = 300


class SubmissionError(Exception):
    """Raised when a submission cannot proceed safely. Always fails closed."""


class SubmissionHandoffRequired(SubmissionError):
    """The write-ahead is done; a single MCP call must now be made by hand."""


class TicketConsumed(SubmissionError):
    """A ticket is single-use. This one has already been used."""


# --------------------------------------------------------------------------
# Ticket
# --------------------------------------------------------------------------


@dataclass
class SubmissionTicket:
    ticket_id: str
    decision_id: str
    approval_id: str
    decision_fingerprint: str
    policy_fingerprint: str
    asset: str
    asset_class: str
    asset_type: str
    amount_usd: Decimal
    month: str
    order_params: Dict[str, Any]
    ref_id: str
    quote_price_usd: Decimal
    quote_timestamp: str
    settled_cash_usd: Decimal
    created_at: str
    expires_at: str
    ticket_fingerprint: str = ""
    consumed_at: Optional[str] = None
    schema_version: int = 1

    # --- immutability -----------------------------------------------------
    def compute_fingerprint(self) -> str:
        payload = {
            "ticket_id": self.ticket_id,
            "decision_id": self.decision_id,
            "approval_id": self.approval_id,
            "decision_fingerprint": self.decision_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "asset": self.asset,
            "asset_class": self.asset_class,
            "asset_type": self.asset_type,
            "amount_usd": money_str(self.amount_usd),
            "month": self.month,
            "ref_id": self.ref_id,
            "order_params": {
                k: v for k, v in sorted(self.order_params.items()) if not k.startswith("_")
            },
            "quote_price_usd": str(self.quote_price_usd),
            "quote_timestamp": self.quote_timestamp,
            "expires_at": self.expires_at,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def is_consumed(self) -> bool:
        return bool(self.consumed_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ticket_id": self.ticket_id,
            "decision_id": self.decision_id,
            "approval_id": self.approval_id,
            "decision_fingerprint": self.decision_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "asset": self.asset,
            "asset_class": self.asset_class,
            "asset_type": self.asset_type,
            "amount_usd": money_str(self.amount_usd),
            "month": self.month,
            "order_params": self.order_params,
            "ref_id": self.ref_id,
            "quote_price_usd": str(self.quote_price_usd),
            "quote_timestamp": self.quote_timestamp,
            "settled_cash_usd": money_str(self.settled_cash_usd),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "ticket_fingerprint": self.ticket_fingerprint,
            "consumed_at": self.consumed_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SubmissionTicket":
        if not isinstance(raw, dict):
            raise SubmissionError("submission ticket must be an object")
        required = ("ticket_id", "decision_id", "approval_id", "decision_fingerprint",
                    "policy_fingerprint", "asset", "amount_usd", "order_params",
                    "ref_id", "expires_at", "ticket_fingerprint")
        missing = [k for k in required if not raw.get(k)]
        if missing:
            raise SubmissionError("submission ticket is missing %s" % ", ".join(missing))
        try:
            ticket = cls(
                ticket_id=str(raw["ticket_id"]),
                decision_id=str(raw["decision_id"]),
                approval_id=str(raw["approval_id"]),
                decision_fingerprint=str(raw["decision_fingerprint"]),
                policy_fingerprint=str(raw["policy_fingerprint"]),
                asset=str(raw["asset"]),
                asset_class=str(raw.get("asset_class") or ""),
                asset_type=str(raw.get("asset_type") or ""),
                amount_usd=parse_money(raw["amount_usd"], "amount_usd"),
                month=str(raw.get("month") or ""),
                order_params=dict(raw["order_params"]),
                ref_id=str(raw["ref_id"]),
                quote_price_usd=parse_money(raw.get("quote_price_usd", "0"), "quote_price_usd"),
                quote_timestamp=str(raw.get("quote_timestamp") or ""),
                settled_cash_usd=parse_money(raw.get("settled_cash_usd", "0"), "settled_cash_usd"),
                created_at=str(raw.get("created_at") or ""),
                expires_at=str(raw["expires_at"]),
                ticket_fingerprint=str(raw["ticket_fingerprint"]),
                consumed_at=raw.get("consumed_at"),
            )
        except MoneyError as exc:
            raise SubmissionError(str(exc))
        if ticket.compute_fingerprint() != ticket.ticket_fingerprint:
            raise SubmissionError(
                "submission ticket %s has been modified since it was created; its "
                "fingerprint no longer matches. Refusing." % ticket.ticket_id
            )
        return ticket


def _stamp(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_stamp(value: str, label: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise SubmissionError("%s %r is not an ISO-8601 UTC timestamp" % (label, value))


def create_ticket(
    decision: Dict[str, Any],
    approval: ApprovalRecord,
    preflight_result: PreflightResult,
    snapshot: BrokerSnapshot,
    account_number: str,
    now: Optional[datetime] = None,
) -> SubmissionTicket:
    """Build a single-use ticket for exactly one order.

    Refuses unless preflight passed. A ticket is never a way to bypass a check —
    it is a frozen record of checks that already passed.
    """
    now = now or utc_now()
    if not preflight_result.ok:
        raise SubmissionError(
            "cannot create a submission ticket: preflight did not pass (%s)"
            % ", ".join(preflight_result.codes)
        )
    if snapshot.quote_price_usd is None or snapshot.quote_timestamp is None:
        raise SubmissionError("a submission ticket requires a fresh quote")
    if snapshot.cash_usd is None:
        raise SubmissionError("a submission ticket requires an established cash balance")

    amount = parse_money(
        decision.get("proposed_amount_usd", decision.get("proposed_amount", "0"))
    ).quantize(CENTS)
    if amount > approval.max_amount_usd:
        raise SubmissionError("amount exceeds the approved maximum")

    order_params = build_order_request(decision, snapshot, account_number)
    settled = (snapshot.cash_usd - (snapshot.unsettled_funds_usd or ZERO)).quantize(CENTS)

    ticket = SubmissionTicket(
        ticket_id="tkt_" + uuid.uuid4().hex[:16],
        decision_id=approval.decision_id,
        approval_id=approval.approval_id,
        decision_fingerprint=fingerprint(decision),
        policy_fingerprint=policy_fingerprint(),
        asset=approval.asset,
        asset_class=approval.asset_class,
        asset_type=approval.asset_type,
        amount_usd=amount,
        month=approval.month,
        order_params=order_params,
        ref_id=broker_ref_id(approval.decision_id),
        quote_price_usd=snapshot.quote_price_usd,
        quote_timestamp=_stamp(snapshot.quote_timestamp),
        settled_cash_usd=settled if settled > ZERO else ZERO,
        created_at=_stamp(now),
        expires_at=_stamp(now + timedelta(seconds=TICKET_TTL_SECONDS)),
    )
    ticket.ticket_fingerprint = ticket.compute_fingerprint()
    return ticket


def verify_ticket(
    ticket: SubmissionTicket,
    decision: Dict[str, Any],
    approval: Optional[ApprovalRecord],
    config: Config,
    now: Optional[datetime] = None,
) -> List[Tuple[str, str]]:
    """Re-verify a ticket immediately before use. Empty list means usable."""
    now = now or utc_now()
    blockers: List[Tuple[str, str]] = []

    blockers.extend(execution_gate_blockers(config))

    if ticket.is_consumed:
        blockers.append(
            ("TICKET_ALREADY_CONSUMED",
             "ticket %s was consumed at %s. Tickets are single-use; a second "
             "submission must never be built from one."
             % (ticket.ticket_id, ticket.consumed_at))
        )

    if ticket.compute_fingerprint() != ticket.ticket_fingerprint:
        blockers.append(
            ("TICKET_MODIFIED", "the ticket's contents no longer match its fingerprint")
        )

    if now >= _parse_stamp(ticket.expires_at, "expires_at"):
        blockers.append(
            ("TICKET_EXPIRED",
             "ticket expired at %s; the embedded quote is stale and a new preflight "
             "is required" % ticket.expires_at)
        )

    if approval is None:
        blockers.append(("NOT_APPROVED", "the approval backing this ticket is gone"))
    else:
        if approval.approval_id != ticket.approval_id:
            blockers.append(
                ("APPROVAL_MISMATCH",
                 "ticket cites approval %s but the stored approval is %s"
                 % (ticket.approval_id, approval.approval_id))
            )
        if approval.decision_fingerprint != ticket.decision_fingerprint:
            blockers.append(
                ("DECISION_MODIFIED", "the decision changed after the ticket was built")
            )
        if ticket.amount_usd > approval.max_amount_usd:
            blockers.append(
                ("AMOUNT_EXCEEDS_APPROVAL",
                 "ticket is for %s but approval caps the order at %s"
                 % (money_str(ticket.amount_usd), money_str(approval.max_amount_usd)))
            )

    if fingerprint(decision) != ticket.decision_fingerprint:
        blockers.append(
            ("DECISION_MODIFIED", "the decision payload no longer matches the ticket")
        )

    try:
        if policy_fingerprint() != ticket.policy_fingerprint:
            blockers.append(
                ("POLICY_CHANGED", "the policy surface changed after the ticket was built")
            )
    except ApprovalError as exc:
        blockers.append(("POLICY_FINGERPRINT_UNAVAILABLE", str(exc)))

    if ticket.ref_id != broker_ref_id(ticket.decision_id):
        blockers.append(
            ("REF_ID_MISMATCH",
             "the ticket's ref_id is not the deterministic key for this decision; "
             "broker-side deduplication would not protect this submission")
        )

    side = str(ticket.order_params.get("side", "")).lower()
    if side != "buy":
        blockers.append(("NOT_A_BUY", "ticket order side is %r" % side))

    return blockers


# --------------------------------------------------------------------------
# Ticket persistence
# --------------------------------------------------------------------------


def load_tickets(path: str = TICKETS_PATH) -> Dict[str, SubmissionTicket]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SubmissionError("ticket store could not be read: %s" % exc)
    if not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SubmissionError(
            "ticket store at %s is not valid JSON (%s). An unreadable ticket store "
            "must never be treated as 'no ticket was used'." % (path, exc)
        )
    if not isinstance(raw, dict) or not isinstance(raw.get("tickets"), dict):
        raise SubmissionError("ticket store must contain a 'tickets' object")
    return {k: SubmissionTicket.from_dict(v) for k, v in raw["tickets"].items()}


def save_tickets(tickets: Dict[str, SubmissionTicket], path: str = TICKETS_PATH) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "updated_at": iso_now(),
            "note": "Submission tickets are single-use. A consumed ticket can never "
                    "produce a second order.",
            "tickets": {k: v.to_dict() for k, v in tickets.items()},
        },
    )


# --------------------------------------------------------------------------
# Submitters
# --------------------------------------------------------------------------


class Submitter:
    """Interface for whatever actually talks to the broker.

    ``src/`` ships only implementations that refuse or hand off. A real MCP call
    happens outside this process, under explicit permission.
    """

    name = "abstract"

    def submit(self, ticket: SubmissionTicket) -> Dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


class DisabledSubmitter(Submitter):
    """The shipped default. Refuses everything."""

    name = "disabled"

    def submit(self, ticket: SubmissionTicket) -> Dict[str, Any]:
        raise ExecutionDisabled(
            "Live submission is disabled. No order was sent for ticket %s."
            % ticket.ticket_id
        )


class ManualHandoffSubmitter(Submitter):
    """Writes the exact order parameters out and stops.

    This is the production bridge. By the time it runs, the write-ahead is
    already durable, so the operator can make exactly one MCP call knowing that
    a crash mid-call cannot produce a silent second order.
    """

    name = "manual-handoff"

    def __init__(self, path: str = HANDOFF_PATH):
        self.path = path

    def submit(self, ticket: SubmissionTicket) -> Dict[str, Any]:
        atomic_write_json(
            self.path,
            {
                "schema_version": 1,
                "written_at": iso_now(),
                "instruction": (
                    "Make EXACTLY ONE MCP call with these parameters, then record the "
                    "response with scripts/record_submission.py. Do not retry. If the "
                    "call fails or its outcome is unclear, run "
                    "scripts/reconcile_submission.py instead."
                ),
                "ticket_id": ticket.ticket_id,
                "decision_id": ticket.decision_id,
                "ref_id": ticket.ref_id,
                "order_params": ticket.order_params,
            },
        )
        raise SubmissionHandoffRequired(
            "Write-ahead complete and the order parameters are at %s. Exactly one "
            "MCP call must now be made by hand, then recorded. This process will "
            "not make it." % self.path
        )


# --------------------------------------------------------------------------
# The write-ahead submission path
# --------------------------------------------------------------------------


def submit_once(
    ticket: SubmissionTicket,
    record: ExecutionRecord,
    submitter: Submitter,
    tickets: Dict[str, SubmissionTicket],
    tickets_path: str = TICKETS_PATH,
    audit_path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Consume the ticket, write-ahead, then call the submitter exactly once.

    The order of operations is the whole point:

    1. refuse a consumed ticket;
    2. mark it consumed and persist that, fsynced;
    3. move the record to ``SUBMISSION_UNCERTAIN`` and persist that, fsynced;
    4. only then call the submitter.

    Any failure after step 3 leaves ``SUBMISSION_UNCERTAIN``, which the executor
    refuses to act on until reconciled. This function never retries.
    """
    now = now or utc_now()

    if ticket.is_consumed:
        raise TicketConsumed(
            "ticket %s was already consumed at %s" % (ticket.ticket_id, ticket.consumed_at)
        )
    if record.state != ExecutionState.PRE_EXECUTION_VALIDATED:
        raise SubmissionError(
            "cannot submit from state %s; only PRE_EXECUTION_VALIDATED may submit"
            % record.state
        )

    # --- 2. consume the ticket, durably ---------------------------------
    ticket.consumed_at = _stamp(now)
    tickets[ticket.ticket_id] = ticket
    save_tickets(tickets, tickets_path)

    # --- 3. write-ahead: intent is durable BEFORE the call --------------
    if audit_path:
        write_audit(
            EVENT_SUBMIT_INTENT,
            decision_id=ticket.decision_id,
            detail={
                "ticket_id": ticket.ticket_id,
                "ref_id": ticket.ref_id,
                "amount_usd": money_str(ticket.amount_usd),
                "asset": ticket.asset,
                "order_params": {
                    k: v for k, v in ticket.order_params.items() if not k.startswith("_")
                },
                "note": "written and fsynced BEFORE any broker call",
            },
            path=audit_path,
        )
    transition(
        record, ExecutionState.SUBMISSION_UNCERTAIN,
        reason="write-ahead before broker call",
        audit_path=audit_path, ref_id=ticket.ref_id,
    )

    # --- 4. the single call ---------------------------------------------
    try:
        response = submitter.submit(ticket)
    except BaseException as exc:
        if audit_path:
            write_audit(
                EVENT_FAILURE,
                decision_id=ticket.decision_id,
                detail={
                    "stage": "submission",
                    "submitter": submitter.name,
                    "error": "%s: %s" % (type(exc).__name__, exc),
                    "state": record.state,
                    "next": "reconcile the broker; do NOT resubmit",
                },
                path=audit_path,
            )
        raise

    return response


# --------------------------------------------------------------------------
# Broker response handling
# --------------------------------------------------------------------------


@dataclass
class BrokerResponse:
    order_id: str
    state: str
    ref_id: Optional[str] = None
    symbol: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def normalize_broker_response(raw: Any, ticket: SubmissionTicket) -> BrokerResponse:
    """Read a broker order response. Fails closed on anything unexpected."""
    if raw is None:
        raise SubmissionError(
            "the broker response is missing. Do not assume the order was not placed; "
            "reconcile before doing anything else."
        )
    if not isinstance(raw, dict):
        raise SubmissionError("broker response must be an object, got %s" % type(raw).__name__)

    body = raw
    if isinstance(raw.get("data"), dict):
        body = raw["data"]
        if isinstance(body.get("order"), dict):
            body = body["order"]

    order_id = body.get("id") or body.get("order_id")
    if not isinstance(order_id, str) or not order_id.strip():
        raise SubmissionError(
            "broker response has no order id; treat the submission as uncertain and reconcile"
        )

    state = str(body.get("state") or "").strip().lower()
    known = OPEN_STATES | FILLED_STATES | DEAD_STATES
    if state not in known:
        raise SubmissionError(
            "broker response has unrecognized state %r; refusing to interpret it" % body.get("state")
        )

    echoed = body.get("ref_id") or body.get("client_order_id")
    if echoed and str(echoed) != ticket.ref_id:
        raise SubmissionError(
            "broker echoed ref_id %r but the ticket's key is %r; this response may "
            "belong to a different order" % (echoed, ticket.ref_id)
        )

    return BrokerResponse(
        order_id=order_id.strip(),
        state=state,
        ref_id=str(echoed) if echoed else None,
        symbol=str(body.get("symbol") or ticket.asset),
        raw=body,
    )


def ingest_response(
    record: ExecutionRecord,
    response: BrokerResponse,
    audit_path: Optional[str] = None,
) -> ExecutionRecord:
    """Move SUBMISSION_UNCERTAIN -> SUBMITTED once an order id is known.

    This records that an order exists. It deliberately does NOT decide whether it
    filled — that requires asking the broker, which is :func:`reconcile_submission`.
    """
    if record.state != ExecutionState.SUBMISSION_UNCERTAIN:
        raise SubmissionError(
            "only a SUBMISSION_UNCERTAIN record may ingest a broker response; got %s"
            % record.state
        )
    if audit_path:
        write_audit(
            EVENT_SUBMITTED,
            decision_id=record.decision_id,
            detail={"broker_order_id": response.order_id, "broker_state": response.state},
            path=audit_path,
        )
    return transition(
        record, ExecutionState.SUBMITTED,
        reason="broker returned order id",
        audit_path=audit_path, broker_order_id=response.order_id,
    )


@dataclass
class FillReconciliation:
    found: bool
    state: Optional[str] = None
    filled_usd: Decimal = ZERO
    requested_usd: Decimal = ZERO
    broker_order_id: Optional[str] = None
    next_state: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "found": self.found,
            "broker_state": self.state,
            "filled_usd": money_str(self.filled_usd),
            "requested_usd": money_str(self.requested_usd),
            "broker_order_id": self.broker_order_id,
            "next_state": self.next_state,
            "notes": list(self.notes),
        }


def reconcile_submission(
    ticket: SubmissionTicket,
    record: ExecutionRecord,
    orders_payload: Any,
    asset_class_hint: str = "EQUITY",
) -> FillReconciliation:
    """Ask the broker what actually happened, instead of trusting the response.

    Matching is by ``ref_id`` first (the deterministic idempotency key), then by
    the recorded broker order id. **A not-found order is never treated as
    'nothing happened'** — the record stays uncertain and a human decides.
    """
    try:
        orders = extract_orders(orders_payload, asset_class_hint)
    except ReconciliationError as exc:
        raise SubmissionError("cannot reconcile the submission: %s" % exc)

    matches = []
    for order in orders:
        if order.ref_id and order.ref_id == ticket.ref_id:
            matches.append(order)
        elif record.broker_order_id and order.order_id == record.broker_order_id:
            matches.append(order)

    if not matches:
        return FillReconciliation(
            found=False,
            next_state=None,
            notes=[
                "No order matching ref_id %s or broker order id %s was found. This does "
                "NOT mean the order was not placed — the broker may not have indexed it "
                "yet. The record stays SUBMISSION_UNCERTAIN. Re-check before doing "
                "anything, and never resubmit on the strength of a not-found result."
                % (ticket.ref_id, record.broker_order_id)
            ],
        )

    if len(matches) > 1:
        return FillReconciliation(
            found=True,
            next_state=None,
            notes=[
                "%d orders matched this ticket. That should be impossible with "
                "ref_id deduplication and requires manual investigation before any "
                "further action." % len(matches)
            ],
        )

    order = matches[0]
    filled = order.notional_executed.quantize(CENTS)
    requested = order.notional_requested.quantize(CENTS)
    notes: List[str] = []

    if order.is_filled:
        next_state = ExecutionState.FILLED
    elif order.state == "partially_filled":
        next_state = ExecutionState.PARTIALLY_FILLED
        notes.append("partially filled: %s of %s" % (money_str(filled), money_str(requested)))
    elif order.is_open:
        next_state = None
        notes.append("order is still open (%s); reconcile again later" % order.state)
    elif order.state in ("rejected", "failed"):
        next_state = ExecutionState.REJECTED
        notes.append("broker rejected the order")
    else:
        next_state = ExecutionState.CANCELLED
        if filled > ZERO:
            notes.append(
                "cancelled after a partial fill of %s; that amount was still spent"
                % money_str(filled)
            )

    return FillReconciliation(
        found=True, state=order.state, filled_usd=filled, requested_usd=requested,
        broker_order_id=order.order_id, next_state=next_state, notes=notes,
    )
