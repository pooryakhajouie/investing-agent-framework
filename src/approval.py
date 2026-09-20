"""Decision immutability and human approval.

Three things are kept strictly separate in this system:

    MODEL DECISION   ->   USER APPROVAL   ->   EXECUTION

A model produces a `PROPOSED` decision. **That is a recommendation, not
permission.** Only a separate, deliberate approval action produces an
`APPROVED` record, and only an approval record that still matches the decision
byte-for-byte can reach the executor.

> **Natural-language reasoning from Claude is not approval.** No amount of
> confidence, no phrase in a thesis, and no field the model writes into its own
> decision payload creates an approval. `src/guardrails.py` rejects any decision
> that carries approval-shaped fields, and this module never reads them.

Immutability is enforced with a SHA-256 fingerprint over a canonical
representation of the binding fields. Change the ticker, the amount, the asset
class, the action, the decision id, or the calendar month, and the fingerprint
changes, which invalidates the approval. An approved $10.00 purchase can never
quietly become $15.00.
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

from .models import CENTS, ZERO, MoneyError, money_str, parse_money, usd
from .state import REPO_ROOT, Config, atomic_write_json, current_month, iso_now, utc_now

APPROVALS_PATH = os.path.join(REPO_ROOT, "state", "approvals.json")

# Conservative default. A decision may specify a SHORTER window; never a longer one.
DEFAULT_APPROVAL_TTL_HOURS = 24
MAX_APPROVAL_TTL_HOURS = 24

# The binding fields. These and only these determine the fingerprint: changing
# any of them means the thing approved is not the thing about to be executed.
BINDING_FIELDS = (
    "decision_id",
    "action",
    "side",
    "asset",
    "asset_class",
    "asset_type",
    "position_type",
    "proposed_amount_usd",
    "month",
)

# Files whose content defines the policy and guardrail surface. If any changes,
# every outstanding approval is invalidated.
POLICY_SURFACE_FILES = (
    "config.json",
    "INVESTMENT_POLICY.md",
    "src/guardrails.py",
    "src/models.py",
)

#: Policy-surface files hashed by canonical *meaning* rather than raw bytes.
#:
#: Only config.json qualifies, and the reason is specific: it is the one policy
#: file the operator is required to edit mid-procedure (arming, then disarming),
#: and it is the one whose meaning is fully captured by a parsed data structure.
#: A whitespace-only rewrite — `json.dump(..., indent=2)` dropping blank lines,
#: say — changed the fingerprint and silently voided outstanding approvals, which
#: is a false alarm that trains operators to ignore real ones.
#:
#: The tradeoff, stated plainly: canonical hashing means a formatting change is
#: no longer tamper-evident in this file. That is acceptable *here* because
#: config.json has no semantics outside its parsed values — no comments that
#: change meaning, no ordering significance, no executable content. It is NOT
#: acceptable for the other three: in a .md policy or a .py module the bytes are
#: the meaning, a reordered paragraph or a moved line can change behaviour or
#: intent, and there is no parse that captures it. Those stay raw-byte hashed.
#:
#: Every semantic change to config.json — any value, any added or removed key —
#: still changes the fingerprint and still voids approvals.
CANONICAL_JSON_POLICY_FILES = frozenset({"config.json"})

# Namespace for deriving a stable broker idempotency key from a decision id.
REF_ID_NAMESPACE = uuid.UUID("6f2b1c94-0c5e-5f2a-9a7d-3c1e8b4f0a21")


class ApprovalError(Exception):
    """Raised when approval data cannot be trusted. Always fails closed."""


# --------------------------------------------------------------------------
# Canonical representation and fingerprint
# --------------------------------------------------------------------------


def _normalized_amount(value: Any) -> str:
    try:
        return money_str(parse_money(value, "proposed_amount_usd"))
    except MoneyError as exc:
        raise ApprovalError("cannot fingerprint an unparseable amount: %s" % exc)


def binding_view(decision: Dict[str, Any], month: Optional[str] = None) -> Dict[str, str]:
    """Extract exactly the fields that bind an approval to a decision."""
    if not isinstance(decision, dict):
        raise ApprovalError("decision must be an object")

    asset = decision.get("ticker") or decision.get("symbol") or ""
    view = {
        "decision_id": str(decision.get("decision_id") or "").strip(),
        "action": str(decision.get("action") or decision.get("decision") or "").strip().lower(),
        "side": str(decision.get("side") or "").strip().lower(),
        "asset": str(asset).strip().upper(),
        "asset_class": str(decision.get("asset_class") or "").strip().upper(),
        "asset_type": str(decision.get("asset_type") or "").strip().lower(),
        "position_type": str(decision.get("position_type") or "").strip().upper(),
        "proposed_amount_usd": _normalized_amount(
            decision.get("proposed_amount_usd", decision.get("proposed_amount", "0"))
        ),
        "month": str(month or decision.get("month") or current_month()).strip(),
    }
    if not view["decision_id"]:
        raise ApprovalError("decision_id is required to fingerprint a decision")
    return view


def canonical_payload(decision: Dict[str, Any], month: Optional[str] = None) -> str:
    """A stable, whitespace-free JSON string over the binding fields only."""
    view = binding_view(decision, month)
    ordered = {key: view[key] for key in BINDING_FIELDS}
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(decision: Dict[str, Any], month: Optional[str] = None) -> str:
    """SHA-256 of the canonical payload."""
    return hashlib.sha256(canonical_payload(decision, month).encode("utf-8")).hexdigest()


def canonical_json_bytes(text: str) -> bytes:
    """A formatting-independent encoding of a JSON document.

    Keys sorted, separators fixed, no indentation. Two documents with the same
    values produce identical bytes regardless of how they were written out.
    Raises rather than guessing if the text is not valid JSON — an unparseable
    policy file must fail closed, not silently fall back to raw bytes.
    """
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ApprovalError("policy surface file is not valid JSON: %s" % exc)
    return json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def policy_fingerprint(repo_root: str = REPO_ROOT) -> str:
    """SHA-256 over the policy and guardrail surface.

    An approval is only valid against the rules that were in force when it was
    given. Change the policy, the config values, the models, or the guardrails,
    and every outstanding approval becomes invalid.

    Files in :data:`CANONICAL_JSON_POLICY_FILES` are hashed by parsed meaning;
    everything else by raw bytes. See the note on that constant for why the
    distinction is drawn exactly there.
    """
    digest = hashlib.sha256()
    for relative in POLICY_SURFACE_FILES:
        path = os.path.join(repo_root, relative)
        digest.update(relative.encode("utf-8"))
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise ApprovalError("cannot fingerprint policy surface file %s: %s" % (relative, exc))
        if relative in CANONICAL_JSON_POLICY_FILES:
            try:
                raw = canonical_json_bytes(raw.decode("utf-8"))
            except (UnicodeDecodeError, ApprovalError) as exc:
                raise ApprovalError(
                    "cannot canonicalize policy surface file %s: %s. An unreadable "
                    "policy file fails closed rather than falling back to raw bytes."
                    % (relative, exc)
                )
        digest.update(raw)
    return digest.hexdigest()


def challenge_phrase(ticker: Any, amount: Any) -> str:
    """The exact phrase a human must type to approve one decision.

    Lives here rather than in the approval script so that anything which *shows*
    a human what they will have to type — ``scripts/promote_latest_recommendation.py``
    among them — cannot drift from what ``scripts/approve_decision.py`` actually
    demands. A phrase that looks right but is not accepted teaches the operator
    to copy-paste past the one deliberate friction point in the whole system.
    """
    return "APPROVE %s %s" % (
        str(ticker or "").strip().upper(),
        usd(parse_money(amount, "amount")),
    )


def broker_ref_id(decision_id: str) -> str:
    """A deterministic idempotency key for the broker, derived from decision_id.

    Both Robinhood order tools deduplicate on ``ref_id``. Deriving it from the
    decision id means a retry after an uncertain submission re-sends the SAME
    key, so the upstream collapses it rather than creating a second order.
    """
    if not decision_id or not str(decision_id).strip():
        raise ApprovalError("decision_id is required to derive a broker ref_id")
    return str(uuid.uuid5(REF_ID_NAMESPACE, str(decision_id).strip()))


# --------------------------------------------------------------------------
# Approval record
# --------------------------------------------------------------------------


@dataclass
class ApprovalRecord:
    decision_id: str
    decision_fingerprint: str
    policy_fingerprint: str
    asset: str
    asset_class: str
    asset_type: str
    action: str
    side: str
    max_amount_usd: Decimal
    month: str
    approved_at: str
    expires_at: str
    approved_by: str
    approval_id: str = field(default_factory=lambda: "apr_" + uuid.uuid4().hex[:16])
    schema_version: int = 1
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "approval_id": self.approval_id,
            "decision_id": self.decision_id,
            "decision_fingerprint": self.decision_fingerprint,
            "policy_fingerprint": self.policy_fingerprint,
            "asset": self.asset,
            "asset_class": self.asset_class,
            "asset_type": self.asset_type,
            "action": self.action,
            "side": self.side,
            "max_amount_usd": money_str(self.max_amount_usd),
            "month": self.month,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
            "approved_by": self.approved_by,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ApprovalRecord":
        if not isinstance(raw, dict):
            raise ApprovalError("approval record must be an object")
        required = (
            "approval_id", "decision_id", "decision_fingerprint", "policy_fingerprint",
            "asset", "asset_class", "asset_type", "action", "side", "max_amount_usd",
            "month", "approved_at", "expires_at", "approved_by",
        )
        missing = [k for k in required if not raw.get(k)]
        if missing:
            raise ApprovalError("approval record is missing %s" % ", ".join(missing))
        try:
            amount = parse_money(raw["max_amount_usd"], "max_amount_usd")
        except MoneyError as exc:
            raise ApprovalError(str(exc))
        return cls(
            decision_id=str(raw["decision_id"]),
            decision_fingerprint=str(raw["decision_fingerprint"]),
            policy_fingerprint=str(raw["policy_fingerprint"]),
            asset=str(raw["asset"]).upper(),
            asset_class=str(raw["asset_class"]).upper(),
            asset_type=str(raw["asset_type"]).lower(),
            action=str(raw["action"]).lower(),
            side=str(raw["side"]).lower(),
            max_amount_usd=amount.quantize(CENTS),
            month=str(raw["month"]),
            approved_at=str(raw["approved_at"]),
            expires_at=str(raw["expires_at"]),
            approved_by=str(raw["approved_by"]),
            approval_id=str(raw["approval_id"]),
            schema_version=int(raw.get("schema_version", 1)),
            note=str(raw.get("note") or ""),
        )


def _parse_iso(stamp: str, label: str) -> datetime:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise ApprovalError("%s %r is not an ISO-8601 UTC timestamp" % (label, stamp))


def create_approval(
    decision: Dict[str, Any],
    approved_by: str,
    now: Optional[datetime] = None,
    ttl_hours: Optional[int] = None,
    note: str = "",
) -> ApprovalRecord:
    """Build an approval for ONE exact decision.

    This is called only by ``scripts/approve_decision.py``. It is never reachable
    from an evaluation, and it deliberately takes no ``approved`` flag from the
    decision payload — the decision cannot approve itself.
    """
    now = now or utc_now()
    view = binding_view(decision)

    if view["action"] != "buy" or view["side"] != "buy":
        raise ApprovalError(
            "only a long BUY may be approved; got action=%r side=%r"
            % (view["action"], view["side"])
        )

    requested = decision.get("approval_ttl_hours", ttl_hours)
    hours = DEFAULT_APPROVAL_TTL_HOURS
    if requested is not None:
        try:
            hours = int(requested)
        except (TypeError, ValueError):
            raise ApprovalError("approval_ttl_hours must be an integer")
        if hours < 1:
            raise ApprovalError("approval_ttl_hours must be at least 1")
    hours = min(hours, MAX_APPROVAL_TTL_HOURS)

    expires = now + timedelta(hours=hours)
    stamp = lambda d: d.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    return ApprovalRecord(
        decision_id=view["decision_id"],
        decision_fingerprint=fingerprint(decision),
        policy_fingerprint=policy_fingerprint(),
        asset=view["asset"],
        asset_class=view["asset_class"],
        asset_type=view["asset_type"],
        action=view["action"],
        side=view["side"],
        max_amount_usd=parse_money(view["proposed_amount_usd"]).quantize(CENTS),
        month=view["month"],
        approved_at=stamp(now),
        expires_at=stamp(expires),
        approved_by=approved_by,
        note=note,
    )


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


@dataclass
class ApprovalVerdict:
    ok: bool
    blockers: List[Tuple[str, str]] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)

    @property
    def codes(self) -> List[str]:
        return [code for code, _ in self.blockers]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "blockers": [{"code": c, "message": m} for c, m in self.blockers],
            "checks": list(self.checks),
        }


def verify_approval(
    approval: Optional[ApprovalRecord],
    decision: Dict[str, Any],
    now: Optional[datetime] = None,
    repo_root: str = REPO_ROOT,
) -> ApprovalVerdict:
    """Confirm an approval still authorizes exactly this decision."""
    now = now or utc_now()
    blockers: List[Tuple[str, str]] = []
    checks: List[str] = []

    checks.append("approval_present")
    if approval is None:
        return ApprovalVerdict(
            False,
            [("NOT_APPROVED", "no approval record exists for this decision; a BUY "
                              "recommendation is not permission to execute")],
            checks,
        )

    checks.append("decision_id_matches")
    view = binding_view(decision)
    if approval.decision_id != view["decision_id"]:
        blockers.append(
            ("APPROVAL_DECISION_ID_MISMATCH",
             "approval is for decision %s, not %s" % (approval.decision_id, view["decision_id"]))
        )

    checks.append("decision_fingerprint_matches")
    current = fingerprint(decision)
    if approval.decision_fingerprint != current:
        blockers.append(
            ("DECISION_MODIFIED",
             "the decision payload changed since approval (approved %s..., now %s...); "
             "approval is void and the decision must be approved again"
             % (approval.decision_fingerprint[:12], current[:12]))
        )

    checks.append("policy_unchanged_since_approval")
    try:
        current_policy = policy_fingerprint(repo_root)
    except ApprovalError as exc:
        blockers.append(("POLICY_FINGERPRINT_UNAVAILABLE", str(exc)))
        current_policy = None
    if current_policy is not None and approval.policy_fingerprint != current_policy:
        blockers.append(
            ("POLICY_CHANGED",
             "the investment policy, config, models, or guardrails changed since "
             "approval; every outstanding approval is void")
        )

    checks.append("approval_not_expired")
    expires = _parse_iso(approval.expires_at, "expires_at")
    if now >= expires:
        blockers.append(
            ("APPROVAL_EXPIRED",
             "approval expired at %s (now %s); a new evaluation and a new approval are required"
             % (approval.expires_at, now.replace(microsecond=0).isoformat()))
        )

    checks.append("approval_month_is_current")
    month_now = current_month(now)
    if approval.month != month_now:
        blockers.append(
            ("APPROVAL_MONTH_ROLLED_OVER",
             "approval was for %s but the current month is %s; monthly authorization "
             "does not carry across months" % (approval.month, month_now))
        )

    checks.append("asset_and_action_match")
    for label, approved_value, decision_value in (
        ("asset", approval.asset, view["asset"]),
        ("asset_class", approval.asset_class, view["asset_class"]),
        ("asset_type", approval.asset_type, view["asset_type"]),
        ("action", approval.action, view["action"]),
        ("side", approval.side, view["side"]),
    ):
        if approved_value != decision_value:
            blockers.append(
                ("APPROVAL_FIELD_MISMATCH",
                 "approval authorizes %s=%r but the decision says %r"
                 % (label, approved_value, decision_value))
            )

    # A ceiling check, and deliberately the *second* line of defence on amount.
    # The binding control is the fingerprint: proposed_amount_usd is one of
    # BINDING_FIELDS, so any change to it — downward included — already failed
    # above as DECISION_MODIFIED. This check therefore only ever adds a second
    # code to an over-sized decision; it can never admit an under-sized one.
    # Documentation that described the approval as authorizing "at most" the
    # amount was reading this check in isolation and was wrong.
    checks.append("amount_within_approved_maximum")
    amount = parse_money(view["proposed_amount_usd"])
    if amount <= ZERO:
        blockers.append(("INVALID_AMOUNT", "approved amount must be greater than zero"))
    elif amount > approval.max_amount_usd:
        blockers.append(
            ("AMOUNT_EXCEEDS_APPROVAL",
             "decision proposes %s but approval caps the order at %s"
             % (money_str(amount), money_str(approval.max_amount_usd)))
        )

    checks.append("buy_only")
    if approval.action != "buy" or approval.side != "buy":
        blockers.append(
            ("NOT_A_BUY", "only a long BUY may ever be approved or executed")
        )

    return ApprovalVerdict(not blockers, blockers, checks)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def load_approvals(path: str = APPROVALS_PATH) -> Dict[str, ApprovalRecord]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ApprovalError("approvals file could not be read: %s" % exc)
    if not text.strip():
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ApprovalError(
            "approvals file at %s is not valid JSON (%s); refusing to continue" % (path, exc)
        )
    if not isinstance(raw, dict) or not isinstance(raw.get("approvals"), dict):
        raise ApprovalError("approvals file must be an object with an 'approvals' object")
    return {k: ApprovalRecord.from_dict(v) for k, v in raw["approvals"].items()}


def save_approvals(approvals: Dict[str, ApprovalRecord], path: str = APPROVALS_PATH) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "updated_at": iso_now(),
            "note": "Approvals are created only by scripts/approve_decision.py. "
                    "A model decision can never write here.",
            "approvals": {k: v.to_dict() for k, v in approvals.items()},
        },
    )


def get_approval(decision_id: str, path: str = APPROVALS_PATH) -> Optional[ApprovalRecord]:
    return load_approvals(path).get(decision_id)
