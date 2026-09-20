"""Append-only decision log (JSON Lines) with secret redaction.

One JSON object per line in ``logs/decisions.jsonl``, one line per evaluation —
whether the decision was BUY or WAIT, and whether it passed validation or not.
Rejected proposals are logged too; that record is the point.

Nothing sensitive is ever written. Every record passes through
:func:`redact` first, which strips credential-shaped keys and account-number-
shaped values recursively.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from .models import DECISION_BUY, Proposal, ValidationResult, money_str
from .state import REPO_ROOT, BudgetState, iso_now

DEFAULT_LOG_PATH = os.path.join(REPO_ROOT, "logs", "decisions.jsonl")

EXECUTION_STATUS_DRY_RUN = "DRY_RUN_NOT_EXECUTED"

# Keys are matched two ways. Substrings catch compound names outright; tokens
# match only whole words, so "monthly_budget_authorized" is not mistaken for an
# authorization header just because it contains the letters "auth".
_SECRET_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "credential",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "id_token",
    "account_number",
    "account_id",
    "rhs_account",
    "rhc_account",
    "private_key",
    "device_id",
    "tax_id",
)

_SECRET_KEY_TOKENS = frozenset(
    {
        "auth",
        "oauth",
        "authorization",
        "token",
        "tokens",
        "cookie",
        "cookies",
        "session",
        "sessionid",
        "bearer",
        "jwt",
        "ssn",
        "mfa",
        "otp",
        "pin",
        "passphrase",
    }
)

_KEY_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")

_REDACTED = "[REDACTED]"


def _is_secret_key(key: str) -> bool:
    """True when a key name looks like it holds a credential or an identifier."""
    lowered = key.lower()
    if any(needle in lowered for needle in _SECRET_KEY_SUBSTRINGS):
        return True
    tokens = {part.lower() for part in _KEY_SPLIT_RE.split(key) if part}
    return bool(tokens & _SECRET_KEY_TOKENS)

# Long digit runs look like account numbers; mask all but the last four.
_ACCOUNT_NUMBER_RE = re.compile(r"\b\d{8,}\b")
# Bearer-ish opaque strings.
_BEARER_RE = re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]+")


def _redact_string(value: str) -> str:
    value = _BEARER_RE.sub("Bearer " + _REDACTED, value)
    return _ACCOUNT_NUMBER_RE.sub(lambda m: "••••" + m.group(0)[-4:], value)


def _could_carry_a_secret(value: Any) -> bool:
    """False only for values that cannot encode a credential or an identifier.

    Redaction by key name is a blunt instrument: ``_is_secret_key`` matches on
    word tokens, so a legitimate field like
    ``actionable_with_this_month_authorization`` matches on "authorization" and
    had its value replaced by "[REDACTED]" — which destroyed a boolean the
    guardrails require to be true or false, and made the logged record fail
    re-validation at approval time.

    A booleans-and-None exemption is safe because no credential, account number,
    PIN or token is a ``bool`` or ``None``. Integers are deliberately **not**
    exempt: an account number arriving as ``123456789`` rather than a string is
    exactly the case the key-name rule exists to catch.
    """
    return not (value is None or isinstance(value, bool))


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively strip secrets from a value destined for the log."""
    if _depth > 20:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_secret_key(key_str) and _could_carry_a_secret(item):
                cleaned[key_str] = _REDACTED
            else:
                cleaned[key_str] = redact(item, _depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_string(str(value))


def new_decision_id() -> str:
    """A fresh, unguessable decision id."""
    return "dec_" + uuid.uuid4().hex[:16]


#: Decision blocks carried into the log verbatim.
#:
#: The record used to be a hand-picked projection of the payload, and the fields
#: below were not in it. That made every logged BUY unapprovable: approval
#: re-validates the *record* (``scripts/approve_decision.py``), and the
#: guardrails require exactly these blocks on a BUY, so re-validation always
#: failed with MISSING_REQUIRED_FIELD however complete the original payload was.
#:
#: Persisting them relaxes nothing — no rule changed. It is what makes the record
#: re-checkable by the same guardrails that passed it in the first place.
CARRIED_VERBATIM = (
    # required on every BUY
    "why_not_wait",
    "margin_for_error",
    "monthly_optionality",
    "monthly_optionality_analysis",
    "portfolio_sprawl_assessment",
    "theme",
    "research_package",
    # required when opening a new position
    "new_position_justification",
    # the month-boundary block, and the acknowledgement that goes with it
    "events_considered",
    "authorization_expiry_acknowledged",
    # required on a WAIT — the canonical five-way names. The legacy aliases are
    # projected separately below, for records written before Stage 7.
    "wait_basis",
    "monthly_budget_remaining_usd",
    # the guardrails read these under their canonical names and warn when the
    # record reports only the projected monthly_budget_before / _after
    "monthly_budget_before_usd",
    "monthly_budget_after_usd",
    "best_existing_equity_candidate",
    "best_new_equity_candidate",
    "best_existing_crypto_candidate",
    "best_new_crypto_candidate",
    # descriptive fields the guardrails read when they are present
    "exchange",
    "fractional_eligible",
    "action",
    "side",
    "proposed_amount_usd",
)


def build_record(
    proposal: Proposal,
    result: ValidationResult,
    state_before: BudgetState,
    raw_decision: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the log record for one evaluation.

    ``monthly_budget_after`` reflects what the budget *would* be if the proposal
    were executed. Because version 1 never executes, the persisted budget state
    is unchanged — ``execution_status`` says so explicitly.
    """
    raw = raw_decision if raw_decision is not None else proposal.raw
    if not isinstance(raw, dict):
        raw = {}
    is_buy = proposal.decision == DECISION_BUY and result.valid

    budget_after = state_before.remaining_usd
    if proposal.decision == DECISION_BUY:
        budget_after = state_before.remaining_usd - proposal.proposed_amount_usd

    record: Dict[str, Any] = {
        "decision_id": proposal.decision_id or None,
        "timestamp": iso_now(),
        "schema_version": 1,
        "month": state_before.month,
        "decision": proposal.decision or None,
        "ticker": proposal.ticker,
        "security_name": proposal.security_name,
        "asset_class": proposal.asset_class,
        "asset_type": proposal.asset_type,
        "position_type": proposal.position_type,
        "classification": proposal.classification,
        "investment_horizon_months": proposal.investment_horizon_months,
        "proposed_amount": money_str(proposal.proposed_amount_usd),
        "monthly_budget_authorized": money_str(state_before.authorized_budget_usd),
        "monthly_budget_before": money_str(state_before.remaining_usd),
        "monthly_budget_after": money_str(budget_after),
        "current_price_usd": (
            money_str(proposal.current_price_usd)
            if proposal.current_price_usd is not None
            else None
        ),
        "quote_timestamp": raw.get("quote_timestamp"),
        "confidence": proposal.confidence,
        "thesis": raw.get("thesis"),
        "value_creation": raw.get("value_creation"),
        "valuation_reasoning": raw.get("valuation_reasoning"),
        "timing_reason": raw.get("timing_reason"),
        "alternatives_considered": raw.get("alternatives_considered"),
        "portfolio_exposure": raw.get("portfolio_exposure"),
        "risks": raw.get("risks"),
        "invalidation": raw.get("invalidation"),
        "cost_basis_analysis": raw.get("cost_basis_analysis"),
        "scorecard": raw.get("scorecard"),
        "candidate_pool": raw.get("candidate_pool"),
        "deep_research_shortlist": raw.get("deep_research_shortlist"),
        "best_existing_position_candidate": raw.get("best_existing_position_candidate"),
        "best_new_equity_candidate": raw.get("best_new_equity_candidate"),
        "best_crypto_candidate": raw.get("best_crypto_candidate"),
        "evidence": raw.get("evidence"),
        "validation_result": result.to_dict(),
        "execution_status": EXECUTION_STATUS_DRY_RUN,
        "would_have_purchased": bool(is_buy),
        "live_trading": False,
    }

    # Carried verbatim so the record can be re-validated later by the same
    # guardrails that passed it. An absent key stays absent rather than becoming
    # a null, so a WAIT does not acquire empty BUY blocks, or the reverse.
    for key in CARRIED_VERBATIM:
        if key in raw and key not in record:
            record[key] = raw[key]

    return redact(record)


def log_decision(record: Dict[str, Any], path: str = DEFAULT_LOG_PATH) -> Dict[str, Any]:
    """Append one record to the JSONL log. Returns the record as written."""
    safe = redact(record)
    safe.setdefault("execution_status", EXECUTION_STATUS_DRY_RUN)
    if safe.get("execution_status") != EXECUTION_STATUS_DRY_RUN:
        raise ValueError(
            "version 1 may only log %r decisions, got %r"
            % (EXECUTION_STATUS_DRY_RUN, safe.get("execution_status"))
        )

    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    line = json.dumps(safe, ensure_ascii=False, sort_keys=False)
    if "\n" in line:  # pragma: no cover - json.dumps escapes newlines
        raise ValueError("log record must serialize to a single line")

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return safe


def read_decisions(path: str = DEFAULT_LOG_PATH) -> List[Dict[str, Any]]:
    """Read every well-formed record. Malformed lines are skipped, not fatal."""
    records: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    records.append({"_unparseable_line": line[:200]})
    except FileNotFoundError:
        return []
    return records


def decisions_for_month(month: str, path: str = DEFAULT_LOG_PATH) -> List[Dict[str, Any]]:
    """Every decision recorded in calendar month ``month`` (``YYYY-MM``)."""
    out = []
    for record in read_decisions(path):
        if record.get("month") == month:
            out.append(record)
        elif isinstance(record.get("timestamp"), str) and record["timestamp"].startswith(month):
            out.append(record)
    return out


def summarize(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Small aggregate used by the status script."""
    records = list(records)
    buys = [r for r in records if r.get("decision") == "BUY"]
    valid_buys = [r for r in buys if (r.get("validation_result") or {}).get("valid")]
    return {
        "evaluations": len(records),
        "buy_decisions": len(buys),
        "valid_buy_decisions": len(valid_buys),
        "wait_decisions": len([r for r in records if r.get("decision") == "WAIT"]),
        "rejected": len([r for r in records if not (r.get("validation_result") or {}).get("valid")]),
    }
