"""What is worth interrupting a human for, and how often.

A notification that fires every weekday morning is not a notification; it is
weather. Within a week it is muted, and the one that mattered is muted with it.
So this module is mostly about *silence*:

* a `WAIT` is silent. It is the most common and most correct outcome, and there
  is nothing for the owner to do about it;
* an exhausted authorization is silent. The month worked as designed;
* an unchanged condition is silent after it has been reported once, until it
  either materially changes or a reminder interval elapses.

What does fire: an actionable purchase recommendation, a failure in the
machinery that was supposed to produce one, and a funding shortfall the owner is
the only one who can fix.

The suppression key is a **fingerprint of the thing being said**, not of the
event that said it. Two funding reminders naming the same shortfall are the same
notification however many days apart they were generated; a funding reminder
whose shortfall changed is a new one, because the owner's decision changes.

Pure logic plus a small JSON ledger. Rendering a macOS notification is I/O and
belongs to the caller — ``scripts/notify.py`` — so this module stays importable
under the AST rule that bans network and subprocess from ``src/``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .models import ZERO, money_str, parse_money, usd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTIFICATION_STATE_PATH = os.path.join(REPO_ROOT, "state", "notifications.json")

# --------------------------------------------------------------------------
# Kinds
# --------------------------------------------------------------------------

NOTIFY_ACTIONABLE_BUY = "ACTIONABLE_BUY"
NOTIFY_FUNDING_REQUIRED = "FUNDING_REQUIRED"
NOTIFY_MONTH_END_FUNDING = "MONTH_END_FUNDING"
NOTIFY_PROMOTION_FAILED = "PROMOTION_FAILED"
NOTIFY_RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
NOTIFY_PROPOSAL_INVALIDATED = "PROPOSAL_INVALIDATED"

NOTIFICATION_KINDS = (
    NOTIFY_ACTIONABLE_BUY,
    NOTIFY_FUNDING_REQUIRED,
    NOTIFY_MONTH_END_FUNDING,
    NOTIFY_PROMOTION_FAILED,
    NOTIFY_RECONCILIATION_FAILED,
    NOTIFY_PROPOSAL_INVALIDATED,
)

#: How long an unchanged condition stays suppressed before it is repeated.
#: Deliberately long: a funding shortfall the owner has already seen is not news
#: tomorrow, and a daily repeat is how a real alert gets trained into noise.
DEFAULT_REMINDER_HOURS = 72

#: An actionable BUY is never suppressed by the reminder interval — a different
#: recommendation is always news — but an *identical* one from a re-run on the
#: same day is not.
REMINDER_HOURS: Dict[str, int] = {
    NOTIFY_ACTIONABLE_BUY: 12,
    NOTIFY_FUNDING_REQUIRED: DEFAULT_REMINDER_HOURS,
    NOTIFY_MONTH_END_FUNDING: 24 * 20,   # at most once per month, in practice
    NOTIFY_PROMOTION_FAILED: 12,
    NOTIFY_RECONCILIATION_FAILED: 6,     # this one is urgent; repeat sooner
    NOTIFY_PROPOSAL_INVALIDATED: 12,
}


class NotificationError(Exception):
    """Raised when the notification ledger cannot be trusted."""


@dataclass
class Notification:
    """One thing worth saying, and the identity that decides whether to repeat."""

    kind: str
    title: str
    body: str
    fingerprint: str = ""
    urgent: bool = False
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "fingerprint": self.fingerprint,
            "urgent": self.urgent,
            "details": dict(self.details),
        }


def _fingerprint(kind: str, parts: Any) -> str:
    payload = json.dumps(
        {"kind": kind, "parts": parts}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# Builders — each returns None when there is nothing worth saying
# --------------------------------------------------------------------------

def actionable_buy(
    decision: str,
    legs: Any,
    remaining_authorization_usd: Any,
    confidence: str = "",
    approval_required: bool = True,
) -> Optional[Notification]:
    """The one notification this whole system exists to produce.

    ``legs`` is a sequence of mappings carrying at least ``ticker`` and
    ``proposed_amount_usd``. A WAIT returns None — silence is the correct
    output for the most common outcome.
    """
    if decision not in ("SINGLE_BUY", "SPLIT_BUY_PLAN"):
        return None
    rows = []
    total = ZERO
    for leg in legs or []:
        ticker = str(
            (leg.get("ticker") if isinstance(leg, dict) else getattr(leg, "ticker", ""))
            or "?").upper()
        raw = (leg.get("proposed_amount_usd") if isinstance(leg, dict)
               else getattr(leg, "proposed_amount_usd", "0"))
        try:
            amount = parse_money(raw, "proposed_amount_usd")
        except Exception:  # noqa: BLE001 - a malformed leg must still notify
            amount = ZERO
        total += amount
        rows.append((ticker, amount))
    if not rows:
        return None

    summary = " + ".join("%s %s" % (usd(a), t) for t, a in rows)
    if not approval_required:  # pragma: no cover - defensive; never true today
        raise NotificationError(
            "a scheduled recommendation is never pre-approved; refusing to say so")

    body_lines = [
        "%s" % summary,
        "Confidence: %s" % (confidence or "not stated"),
        "Remaining authorization: %s" % usd(
            parse_money(remaining_authorization_usd, "remaining_authorization_usd")),
        "HUMAN APPROVAL REQUIRED — nothing is approved or submitted.",
    ]
    return Notification(
        kind=NOTIFY_ACTIONABLE_BUY,
        title="Buy recommendation: %s" % summary,
        body="\n".join(body_lines),
        fingerprint=_fingerprint(NOTIFY_ACTIONABLE_BUY,
                                 [[t, money_str(a)] for t, a in rows]),
        urgent=True,
        details={
            "decision": decision,
            "legs": [{"ticker": t, "amount_usd": money_str(a)} for t, a in rows],
            "total_usd": money_str(total),
            "confidence": confidence,
        },
    )


def funding_required(gate: Any) -> Optional[Notification]:
    """Authorization remains but the account cannot fund it."""
    outcome = gate.outcome if hasattr(gate, "outcome") else gate.get("outcome")
    if outcome != "FUNDING_REQUIRED":
        return None
    remaining = gate.remaining_authorization_usd
    cash = gate.settled_cash_usd if gate.settled_cash_usd is not None else ZERO
    shortfall = gate.shortfall_usd
    return Notification(
        kind=NOTIFY_FUNDING_REQUIRED,
        title="Funding needed: %s unusable" % usd(remaining),
        body=(
            "%s of %s authorization remains, but settled cash is %s.\n"
            "Deposit at least %s to use it. Execution is cash-only."
            % (usd(remaining), gate.month, usd(cash), usd(shortfall))
        ),
        # Keyed on the shortfall, so a changed deposit requirement is news and
        # an unchanged one is not.
        fingerprint=_fingerprint(
            NOTIFY_FUNDING_REQUIRED,
            {"month": gate.month, "shortfall": money_str(shortfall)}),
        urgent=False,
        details=gate.to_dict() if hasattr(gate, "to_dict") else dict(gate),
    )


def month_end_funding(
    month: str, next_month_budget_usd: Any, settled_cash_usd: Any
) -> Optional[Notification]:
    """One reminder near the month's end, so next month starts usable.

    The budget figure is passed in from ``config.json`` rather than assumed;
    nothing here knows or hard-codes a dollar amount.
    """
    budget = parse_money(next_month_budget_usd, "next_month_budget_usd")
    cash = parse_money(settled_cash_usd, "settled_cash_usd")
    if cash >= budget:
        return None
    shortfall = (budget - cash).quantize(budget)
    return Notification(
        kind=NOTIFY_MONTH_END_FUNDING,
        title="Fund the Agentic account for next month",
        body=(
            "%s is ending. Next month's authorization is %s and the account holds "
            "%s in settled cash.\nDeposit %s to start the month able to act.\n"
            "This does not change the monthly budget."
            % (month, usd(budget), usd(cash), usd(shortfall))
        ),
        fingerprint=_fingerprint(
            NOTIFY_MONTH_END_FUNDING, {"month": month, "shortfall": money_str(shortfall)}),
        urgent=False,
        details={
            "month": month,
            "next_month_budget_usd": money_str(budget),
            "settled_cash_usd": money_str(cash),
            "shortfall_usd": money_str(shortfall),
        },
    )


def failure(kind: str, summary: str, detail: str = "",
            details: Optional[Dict[str, Any]] = None) -> Notification:
    """Something that was supposed to work did not."""
    if kind not in NOTIFICATION_KINDS:
        raise NotificationError("unknown notification kind %r" % kind)
    return Notification(
        kind=kind,
        title=summary,
        body=detail or summary,
        fingerprint=_fingerprint(kind, {"summary": summary, "detail": detail}),
        urgent=kind == NOTIFY_RECONCILIATION_FAILED,
        details=dict(details or {}),
    )


# --------------------------------------------------------------------------
# Suppression ledger
# --------------------------------------------------------------------------

def load_state(path: str = NOTIFICATION_STATE_PATH) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise NotificationError("notification ledger unreadable: %s" % exc)
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except ValueError as exc:
        # A corrupt ledger must not silence a real alert. Fail *open* here —
        # the opposite of everywhere else in this repository, and deliberately
        # so: the cost of a duplicate notification is noise, the cost of a
        # swallowed one is a missed purchase or an unreconciled order.
        raise NotificationError("notification ledger is not valid JSON: %s" % exc)
    if not isinstance(payload, dict):
        raise NotificationError("notification ledger must be an object")
    return payload.get("sent", {}) if "sent" in payload else payload


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def should_send(
    notification: Notification,
    sent: Dict[str, Any],
    now: Optional[datetime] = None,
    reminder_hours: Optional[Dict[str, int]] = None,
) -> bool:
    """True unless this exact thing was said recently enough to still stand."""
    now = now or datetime.now(timezone.utc)
    table = reminder_hours or REMINDER_HOURS
    previous = (sent or {}).get(notification.kind)
    if not isinstance(previous, dict):
        return True
    if previous.get("fingerprint") != notification.fingerprint:
        return True          # the thing being said changed: always news
    last = _parse_iso(previous.get("at"))
    if last is None:
        return True
    window = timedelta(hours=table.get(notification.kind, DEFAULT_REMINDER_HOURS))
    return now - last >= window


def record_sent(
    notification: Notification,
    sent: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    updated = dict(sent or {})
    updated[notification.kind] = {
        "fingerprint": notification.fingerprint,
        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "title": notification.title,
    }
    return updated


def save_state(sent: Dict[str, Any], path: str = NOTIFICATION_STATE_PATH) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"schema_version": 1, "sent": sent}, handle, indent=2,
                  ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
