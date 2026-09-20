"""Execution records and the append-only audit log.

Two artifacts:

* ``state/executions.json`` — current state per decision id, written atomically.
* ``logs/execution_audit.jsonl`` — append-only, fsynced, one event per line.

Together they make it possible to reconstruct exactly why an order happened (or
why one did not). Every write goes through the same redaction used by the
decision log, so credentials, tokens and account numbers never reach disk.

**Crash safety.** The intended live sequence is write-ahead:

1. record ``SUBMISSION_UNCERTAIN`` with the deterministic ``ref_id``, fsync;
2. only then call the broker;
3. on a known response, move to ``SUBMITTED`` with the broker order id;
4. on any crash or ambiguity, the record is already ``SUBMISSION_UNCERTAIN``.

A process that restarts and finds ``SUBMISSION_UNCERTAIN`` must reconcile
against the broker. It must never resubmit. Because ``ref_id`` is derived from
``decision_id``, even a resubmission that somehow occurred would be deduplicated
upstream rather than creating a second order.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .decision_logger import redact
from .execution import ExecutionState, InvalidTransition, assert_transition
from .state import REPO_ROOT, atomic_write_json, iso_now

EXECUTIONS_PATH = os.path.join(REPO_ROOT, "state", "executions.json")
AUDIT_LOG_PATH = os.path.join(REPO_ROOT, "logs", "execution_audit.jsonl")

# Event types recorded in the audit log.
EVENT_DECISION_GENERATED = "DECISION_GENERATED"
EVENT_VALIDATION = "VALIDATION_RESULT"
EVENT_APPROVAL_GRANTED = "APPROVAL_GRANTED"
EVENT_APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
EVENT_APPROVAL_REVOKED = "APPROVAL_REVOKED"
EVENT_RECONCILIATION = "RECONCILIATION"
EVENT_PREFLIGHT = "PRE_EXECUTION_CHECKS"
EVENT_SUBMIT_INTENT = "SUBMIT_INTENT"
EVENT_SUBMITTED = "SUBMISSION_RESULT"
EVENT_FILL_RECONCILED = "FILL_RECONCILED"
EVENT_STATE_CHANGE = "STATE_CHANGE"
EVENT_FAILURE = "FAILURE"
EVENT_EXECUTION_REFUSED = "EXECUTION_REFUSED"


class ExecutionStoreError(Exception):
    """Raised when execution records cannot be trusted. Fails closed."""


@dataclass
class ExecutionRecord:
    decision_id: str
    state: str = ExecutionState.PROPOSED
    decision_fingerprint: str = ""
    approval_id: Optional[str] = None
    ref_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    month: str = ""
    asset: str = ""
    amount_usd: str = "0.00"
    created_at: str = field(default_factory=iso_now)
    updated_at: str = field(default_factory=iso_now)
    history: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "state": self.state,
            "decision_fingerprint": self.decision_fingerprint,
            "approval_id": self.approval_id,
            "ref_id": self.ref_id,
            "broker_order_id": self.broker_order_id,
            "month": self.month,
            "asset": self.asset,
            "amount_usd": self.amount_usd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ExecutionRecord":
        if not isinstance(raw, dict) or not raw.get("decision_id"):
            raise ExecutionStoreError("execution record must be an object with a decision_id")
        state = str(raw.get("state") or ExecutionState.PROPOSED)
        if state not in vars(ExecutionState).values():
            raise ExecutionStoreError("execution record has unknown state %r" % state)
        return cls(
            decision_id=str(raw["decision_id"]),
            state=state,
            decision_fingerprint=str(raw.get("decision_fingerprint") or ""),
            approval_id=raw.get("approval_id"),
            ref_id=raw.get("ref_id"),
            broker_order_id=raw.get("broker_order_id"),
            month=str(raw.get("month") or ""),
            asset=str(raw.get("asset") or ""),
            amount_usd=str(raw.get("amount_usd") or "0.00"),
            created_at=str(raw.get("created_at") or iso_now()),
            updated_at=str(raw.get("updated_at") or iso_now()),
            history=list(raw.get("history") or []),
        )


def load_executions(path: str = EXECUTIONS_PATH) -> Dict[str, ExecutionRecord]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ExecutionStoreError("executions file could not be read: %s" % exc)
    if not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExecutionStoreError(
            "executions file at %s is not valid JSON (%s); refusing to continue. An "
            "unreadable execution ledger must never be treated as 'nothing submitted'."
            % (path, exc)
        )
    if not isinstance(raw, dict) or not isinstance(raw.get("executions"), dict):
        raise ExecutionStoreError("executions file must contain an 'executions' object")
    return {k: ExecutionRecord.from_dict(v) for k, v in raw["executions"].items()}


def save_executions(records: Dict[str, ExecutionRecord], path: str = EXECUTIONS_PATH) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "updated_at": iso_now(),
            "executions": {k: v.to_dict() for k, v in records.items()},
        },
    )


def get_state(decision_id: str, path: str = EXECUTIONS_PATH) -> str:
    record = load_executions(path).get(decision_id)
    return record.state if record else ExecutionState.PROPOSED


# The lifecycle a *re*-approval must follow. An approval invalidated by a policy
# change does not simply get overwritten: the record moves
# ``APPROVED -> REAPPROVAL_REQUIRED`` first, and only then may the human
# approval script move it back to ``APPROVED``. There is deliberately no
# ``APPROVED -> APPROVED`` edge in :data:`~src.execution.VALID_TRANSITIONS`,
# because a silent self-edge would let a second approval overwrite a first with
# no trace in the record's history that the earlier one had been invalidated.
REAPPROVAL_PATH = (ExecutionState.REAPPROVAL_REQUIRED, ExecutionState.APPROVED)


def plan_transitions_to_approved(current_state: str) -> Tuple[str, ...]:
    """The state sequence needed to legally reach ``APPROVED``, or raise.

    Nothing is mutated and nothing is persisted. The point is that a caller can
    establish that the *whole* path is legal **before** it writes an approval
    record, which is what stops ``state/approvals.json`` from gaining an
    approval that the execution ledger then refuses to honour.

    The defect this exists to prevent: ``scripts/approve_decision.py`` used to
    ``save_approvals()`` and only afterwards attempt
    ``transition(record, APPROVED)``. Re-approving an already-``APPROVED``
    decision — exactly what a policy-fingerprint change forces — raised
    :class:`InvalidTransition` *after* the approval had been persisted, leaving
    the approval store and the ledger disagreeing about whether the decision was
    approved, and requiring a human to hand-edit JSON to recover.
    """
    if current_state == ExecutionState.APPROVED:
        path: Tuple[str, ...] = REAPPROVAL_PATH
    else:
        path = (ExecutionState.APPROVED,)
    cursor = current_state
    for target in path:
        assert_transition(cursor, target)
        cursor = target
    return path


def transition(
    record: ExecutionRecord,
    target: str,
    reason: str = "",
    audit_path: Optional[str] = AUDIT_LOG_PATH,
    **fields: Any,
) -> ExecutionRecord:
    """Move a record to ``target``, enforcing the state machine."""
    assert_transition(record.state, target)
    # A record may only *become* APPROVED in the company of a minted approval.
    # Approvals are minted by ``src.approval.create_approval``, which is reached
    # only from ``scripts/approve_decision.py`` behind a TTY check and a verbatim
    # challenge phrase. This makes "only the human approval path may produce an
    # APPROVED record" a property of the store rather than a convention.
    if target == ExecutionState.APPROVED:
        approval_id = fields.get("approval_id", record.approval_id)
        if not approval_id:
            raise InvalidTransition(
                "refusing to mark %s APPROVED without an approval_id: an APPROVED "
                "record must be accompanied by an approval minted by "
                "scripts/approve_decision.py. A decision cannot approve itself."
                % record.decision_id
            )
    previous = record.state
    record.state = target
    record.updated_at = iso_now()
    for key, value in fields.items():
        if hasattr(record, key):
            setattr(record, key, value)
    record.history.append(
        {"from": previous, "to": target, "at": record.updated_at, "reason": reason}
    )
    if audit_path:
        write_audit(
            EVENT_STATE_CHANGE,
            decision_id=record.decision_id,
            detail={"from": previous, "to": target, "reason": reason},
            path=audit_path,
        )
    return record


def write_audit(
    event: str,
    decision_id: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    path: str = AUDIT_LOG_PATH,
) -> Dict[str, Any]:
    """Append one redacted event to the audit log and fsync it.

    Fsync matters: the write-ahead SUBMIT_INTENT record is only useful if it
    survives the crash it exists to protect against.
    """
    entry = redact(
        {
            "schema_version": 1,
            "timestamp": iso_now(),
            "event": event,
            "decision_id": decision_id,
            "detail": detail or {},
        }
    )
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False, sort_keys=False)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry


def read_audit(path: str = AUDIT_LOG_PATH) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"_unparseable_line": line[:200]})
    except FileNotFoundError:
        return []
    return events
