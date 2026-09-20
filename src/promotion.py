"""Promotion — turning a scheduled recommendation into a normal PROPOSED decision.

## What promotion is, and is not

A scheduled run may recommend a purchase. It may not mint a `decision_id`, write
`logs/decisions.jsonl`, approve anything, or submit anything. So a recommendation
sits outside the decision pipeline entirely — useful to read, impossible to act
on. Promotion is the one deliberate, human-invoked step that carries it *into*
the pipeline, and it stops exactly where the pipeline already started: at a
**PROPOSED** decision with a real `decision_id` and fingerprint, awaiting the
unchanged human approval flow.

Promotion **is not approval**. It creates the thing a human then approves. The
approval script, its TTL, its verbatim challenge phrase and its audit log are
untouched by any of this.

## The re-checks, and why they are not a formality

The recommendation was produced at 10:30 by a process reading a tape that has
since moved. Everything it relied on is re-established from scratch:

* the recommendation is **still the newest** valid report, and is not stale;
* the digest's ACTION banner and the machine-readable payload **agree** — a
  mismatch means one of them was edited;
* the **monthly authorization** still has room, reconciled against the broker
  rather than the local ledger;
* **broker orders and pending activity** are re-read;
* **settled cash** covers it, cash-only — the account is `limited_margin`, and
  the difference between cash and buying power is margin;
* the **quote is refreshed** and the move since the recommendation is inside the
  existing slippage tolerance;
* **tradability** is re-checked, including crypto halt state and minimum order
  size;
* **every normal guardrail** runs again.

All of that already exists in :func:`src.execution.preflight`, so promotion
calls it rather than reimplementing it. Reimplementation is how two code paths
drift until one of them is wrong.

## The one subtlety: preflight always blocks, on purpose

`preflight()` refuses while the three execution switches are closed — which they
are, permanently. So promotion cannot require `result.ok`. What it does instead
is *partition* the blockers:

* :data:`EXPECTED_AT_PROMOTION` — the switches being shut, and no approval
  existing yet. Both are **required to be present**: their absence would mean
  execution had been enabled, or that something had already approved this.
* everything else — a real blocker, and promotion refuses.

So promotion succeeds only when *the sole reasons this could not execute are the
deliberately-closed switches and the not-yet-given approval*. That is a stronger
statement than filtering the safety checks out, and it verifies the switches are
still closed rather than ignoring them.

Pure functions over plain data. No I/O, no network, no subprocess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .execution import (
    MAX_CRYPTO_QUOTE_AGE_SECONDS,
    MAX_CRYPTO_SLIPPAGE_PCT,
    MAX_EQUITY_QUOTE_AGE_SECONDS,
    MAX_EQUITY_SLIPPAGE_PCT,
    BrokerSnapshot,
    PreflightResult,
)
from .models import CENTS, ZERO, MoneyError, money_str, parse_money

# --------------------------------------------------------------------------
# The blocker partition
# --------------------------------------------------------------------------

#: The three switches being shut. Every one of these MUST appear in a promotion
#: preflight — if one is missing, execution has been enabled somewhere and
#: promotion refuses rather than proceeding into an armed system.
REQUIRED_GATE_CODES = ("AGENT_DISABLED", "EXECUTION_MODE_DRY_RUN", "LIVE_TRADING_DISABLED")

#: No approval exists yet — that is the entire point of promotion.
EXPECTED_APPROVAL_CODES = ("NOT_APPROVED",)

EXPECTED_AT_PROMOTION = frozenset(REQUIRED_GATE_CODES) | frozenset(EXPECTED_APPROVAL_CODES)

#: A recommendation older than this is not promoted. Matches the approval TTL:
#: if an approval may not outlive a day, neither may the thing being promoted.
MAX_RECOMMENDATION_AGE_HOURS = 24

#: Schema of the machine-readable recommendation a scheduled run writes.
RECOMMENDATION_SCHEMA_VERSION = 1

#: A recommendation may not carry any of these. Minting an identifier with no
#: ledger entry behind it, or shipping an approval field, is exactly what the
#: scheduled run is forbidden to do.
FORBIDDEN_RECOMMENDATION_KEYS = (
    "decision_id",
    "approved",
    "approval",
    "approved_by",
    "user_approved",
    "execution_state",
    "fingerprint",
)

VALID_PLAN_TYPES = ("SINGLE_BUY", "SPLIT_BUY_PLAN")
MIN_PLAN_LEGS = 2
MAX_PLAN_LEGS = 5

#: The fields in which a leg may say what it is.
#:
#: ``INVESTMENT_POLICY.md`` §11 ("A BUY must contain") does not list ``decision``
#: at all, and the scheduled prompt's Step 13 sends the run to that list. A leg
#: that declares itself with ``action``/``side`` alone is therefore *conforming*,
#: and requiring ``decision`` specifically rejected a payload the contract asked
#: for. So BUY-ness is read from whichever of these the run actually wrote.
#:
#: The rule is "every verb present agrees", not "any verb says BUY": a leg
#: reading ``decision=WAIT, action=buy`` contradicts itself, and guessing which
#: half is the real one is exactly the kind of inference promotion must not make.
DECISION_VERB_KEYS = ("decision", "action", "side")


class PromotionError(Exception):
    """Raised when a recommendation cannot be read at all."""


@dataclass
class PromotionCheck:
    """Whether one leg may be promoted to a PROPOSED decision."""

    ok: bool = True
    blockers: List[Tuple[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)
    expected: List[str] = field(default_factory=list)

    @property
    def codes(self) -> List[str]:
        return [code for code, _ in self.blockers]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "blockers": [{"code": c, "message": m} for c, m in self.blockers],
            "warnings": list(self.warnings),
            "checks": list(self.checks),
            "expected_and_ignored": list(self.expected),
        }


def classify_preflight(result: PreflightResult) -> PromotionCheck:
    """Partition preflight blockers into expected-at-promotion and fatal.

    The gate codes are not merely tolerated — their **absence** is a blocker of
    its own. Promotion into a system whose switches have been opened is not a
    thing this script will do quietly.
    """
    check = PromotionCheck()
    check.checks.append("preflight_ran")
    check.checks.extend(result.checks)

    codes = set(result.codes)

    check.checks.append("execution_switches_still_closed")
    for gate in REQUIRED_GATE_CODES:
        if gate not in codes:
            check.blockers.append((
                "EXECUTION_ENABLED",
                "preflight did not report %s, which means that execution switch is "
                "OPEN. Promotion refuses to run against an armed execution path — "
                "close it before promoting anything." % gate,
            ))

    for code, message in result.blockers:
        if code in EXPECTED_AT_PROMOTION:
            check.expected.append(code)
        else:
            check.blockers.append((code, message))

    check.warnings.extend(result.warnings)
    check.ok = not check.blockers
    return check


# --------------------------------------------------------------------------
# The recommendation file
# --------------------------------------------------------------------------


def declared_verbs(leg: Dict[str, Any]) -> Dict[str, str]:
    """The verb fields this leg actually sets, upper-cased for comparison."""
    found: Dict[str, str] = {}
    for key in DECISION_VERB_KEYS:
        text = str(leg.get(key) or "").strip()
        if text:
            found[key] = text.upper()
    return found


def is_unambiguous_buy(leg: Dict[str, Any]) -> bool:
    """True when the leg says BUY and says nothing that contradicts it."""
    verbs = declared_verbs(leg)
    return bool(verbs) and all(verb == "BUY" for verb in verbs.values())


def buy_verb_problems(leg: Dict[str, Any], label: str) -> List[str]:
    """Why this leg is not a promotable BUY, if it is not one."""
    verbs = declared_verbs(leg)
    if not verbs:
        return ["%s does not say what it is: none of %s carries a value, so "
                "there is nothing to check against BUY"
                % (label, ", ".join(DECISION_VERB_KEYS))]
    disagreeing = sorted(key for key, verb in verbs.items() if verb != "BUY")
    if disagreeing:
        return ["%s is not a BUY: %s" % (label, ", ".join(
            "%s=%r" % (key, leg.get(key)) for key in disagreeing))]
    return []


def validate_recommendation(payload: Any) -> List[str]:
    """Structural problems with a scheduled run's recommendation payload."""
    problems: List[str] = []
    if not isinstance(payload, dict):
        return ["the recommendation is not a JSON object"]

    if payload.get("schema_version") != RECOMMENDATION_SCHEMA_VERSION:
        problems.append(
            "schema_version is %r, expected %d"
            % (payload.get("schema_version"), RECOMMENDATION_SCHEMA_VERSION))

    plan_type = payload.get("plan_type")
    if plan_type not in VALID_PLAN_TYPES:
        problems.append(
            "plan_type is %r; a recommendation worth promoting is one of %s"
            % (plan_type, list(VALID_PLAN_TYPES)))

    for key in ("generated_at", "source_report"):
        if not str(payload.get(key) or "").strip():
            problems.append("%s is missing" % key)

    legs = payload.get("legs")
    if not isinstance(legs, list) or not legs:
        problems.append("legs is missing or empty")
        return problems

    if plan_type == "SINGLE_BUY" and len(legs) != 1:
        problems.append("plan_type SINGLE_BUY but %d legs" % len(legs))
    if plan_type == "SPLIT_BUY_PLAN" and not (MIN_PLAN_LEGS <= len(legs) <= MAX_PLAN_LEGS):
        problems.append(
            "plan_type SPLIT_BUY_PLAN but %d leg(s); a plan has %d to %d"
            % (len(legs), MIN_PLAN_LEGS, MAX_PLAN_LEGS))

    for index, leg in enumerate(legs):
        label = "legs[%d]" % index
        if not isinstance(leg, dict):
            problems.append("%s is not an object" % label)
            continue
        for key in FORBIDDEN_RECOMMENDATION_KEYS:
            if key in leg:
                problems.append(
                    "%s carries %r. A scheduled run may not mint an identifier or "
                    "an approval field — promotion mints the decision_id, and only "
                    "scripts/approve_decision.py creates an approval."
                    % (label, key))
        problems.extend(buy_verb_problems(leg, label))
        if not str(leg.get("ticker") or "").strip():
            problems.append("%s has no ticker" % label)
        try:
            amount = parse_money(leg.get("proposed_amount_usd"), "proposed_amount_usd")
            if amount <= ZERO:
                problems.append("%s proposes a non-positive amount" % label)
        except MoneyError as exc:
            problems.append("%s: %s" % (label, exc))
    return problems


def recommendation_age_hours(payload: Dict[str, Any], now: datetime) -> Optional[float]:
    """How old the recommendation is, or None when it carries no usable stamp."""
    stamp = str(payload.get("generated_at") or "").strip()
    if not stamp:
        return None
    text = stamp.replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds() / 3600.0


def check_freshness(
    payload: Dict[str, Any],
    newest_report_rel: Optional[str],
    digest_points_at: Optional[str],
    now: datetime,
    max_age_hours: int = MAX_RECOMMENDATION_AGE_HOURS,
) -> PromotionCheck:
    """Is this still the newest valid recommendation, and recent enough?"""
    check = PromotionCheck()

    check.checks.append("recommendation_is_recent")
    age = recommendation_age_hours(payload, now)
    if age is None:
        check.blockers.append((
            "RECOMMENDATION_UNDATED",
            "the recommendation carries no readable generated_at; an undated "
            "recommendation cannot be shown to be current."))
    elif age > max_age_hours:
        check.blockers.append((
            "RECOMMENDATION_STALE",
            "the recommendation is %.1f hours old, past the %d-hour limit. Prices, "
            "the authorization and the broker's own record have all had time to "
            "move. Re-run the evaluation rather than promoting this."
            % (age, max_age_hours)))
    elif age < 0:
        check.blockers.append((
            "RECOMMENDATION_IN_THE_FUTURE",
            "the recommendation is stamped %.1f hours in the future; refusing to "
            "promote a payload whose clock cannot be trusted." % -age))

    source = str(payload.get("source_report") or "").strip()

    check.checks.append("recommendation_is_for_the_newest_report")
    if newest_report_rel and source and not source.endswith(newest_report_rel):
        check.blockers.append((
            "NOT_THE_NEWEST_REPORT",
            "the recommendation names %s but the newest audit record is %s. A "
            "newer evaluation has run since; promote from that one or re-run."
            % (source, newest_report_rel)))

    check.checks.append("digest_and_recommendation_name_the_same_report")
    if digest_points_at and source and not source.endswith(digest_points_at):
        check.blockers.append((
            "DIGEST_RECOMMENDATION_MISMATCH",
            "reports/latest.md points at %s but the recommendation names %s. One of "
            "them has been edited; refusing to guess which."
            % (digest_points_at, source)))

    check.ok = not check.blockers
    return check


def check_banner_agreement(banner: Any, payload: Dict[str, Any]) -> PromotionCheck:
    """The headline a human read must be the thing being promoted.

    The banner is the part of the digest most likely to be read alone. If it
    says `$10 SNDK` and the payload says `$25 NVDA`, the human approved a
    sentence that did not describe the decision.
    """
    check = PromotionCheck()
    check.checks.append("banner_matches_the_recommendation")

    if banner is None or getattr(banner, "kind", "") != "BUY":
        check.blockers.append((
            "NO_BUY_BANNER",
            "reports/latest.md does not announce a purchase in its ACTION line. "
            "There is nothing to promote."))
        check.ok = False
        return check

    legs = payload.get("legs") or []
    banner_legs = list(getattr(banner, "legs", []) or [])

    if len(banner_legs) != len(legs):
        check.blockers.append((
            "BANNER_LEG_COUNT_MISMATCH",
            "the ACTION line names %d leg(s) but the recommendation carries %d"
            % (len(banner_legs), len(legs))))
        check.ok = False
        return check

    for index, ((amount_text, symbol), leg) in enumerate(zip(banner_legs, legs)):
        label = "legs[%d]" % index
        expected_symbol = str(leg.get("ticker") or "").strip().upper()
        if symbol.upper() != expected_symbol:
            check.blockers.append((
                "BANNER_SYMBOL_MISMATCH",
                "%s: the ACTION line says %s, the recommendation says %s"
                % (label, symbol.upper(), expected_symbol)))
            continue
        try:
            shown = parse_money(amount_text.replace(",", ""), "banner amount")
            actual = parse_money(leg.get("proposed_amount_usd"), "proposed_amount_usd")
        except MoneyError as exc:
            check.blockers.append(("BANNER_AMOUNT_UNREADABLE", "%s: %s" % (label, exc)))
            continue
        if shown.quantize(CENTS) != actual.quantize(CENTS):
            check.blockers.append((
                "BANNER_AMOUNT_MISMATCH",
                "%s: the ACTION line says %s for %s, the recommendation says %s"
                % (label, money_str(shown), expected_symbol, money_str(actual))))

    check.ok = not check.blockers
    return check


# --------------------------------------------------------------------------
# The broker snapshot
# --------------------------------------------------------------------------


def _money(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return parse_money(value)
    except MoneyError:
        return None


def _stamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when


#: Per-asset fields a promotion snapshot carries under ``assets[<SYMBOL>]``.
#: Account-level facts (cash, orders, account type) are shared by every leg;
#: these are not, because a two-leg plan needs two quotes and two tradability
#: answers, and one snapshot silently reused for both would re-check the second
#: leg against the first leg's price.
PER_ASSET_SNAPSHOT_FIELDS = (
    "quote_price_usd",
    "quote_timestamp",
    "tradable",
    "fractional_tradable",
    "account_type_tradable",
    "crypto_pair_halted",
    "crypto_min_order_size",
)

#: Account-level fields that make a payload a *promotion* snapshot rather than
#: the capital gate's. The gate deliberately gathers neither.
PROMOTION_ACCOUNT_FIELDS = ("equity_orders", "crypto_orders")


def asset_block(raw: Any, symbol: Optional[str]) -> Dict[str, Any]:
    """The per-asset facts for ``symbol``, or the legacy top-level ones.

    A snapshot written before the per-asset shape existed carried one quote at
    the top level. That still loads, so an operator's hand-built single-leg
    snapshot keeps working; a plan with more than one leg needs the map.
    """
    if not isinstance(raw, dict):
        return {}
    assets = raw.get("assets")
    if isinstance(assets, dict) and symbol:
        for key, value in assets.items():
            if str(key).strip().upper() == str(symbol).strip().upper():
                return value if isinstance(value, dict) else {}
        return {}
    return {field: raw[field] for field in PER_ASSET_SNAPSHOT_FIELDS if field in raw}


def snapshot_problems(raw: Any) -> List[str]:
    """Why this payload is not a promotion snapshot.

    The specific accident worth naming: handing promotion the **capital-gate**
    snapshot. It is a valid JSON object with a real cash figure and a real
    timestamp, so it looks like an answer — but it is gathered by two account
    tools and carries no quote, no tradability and no order history, by design.
    Without this check the operator reads a wall of downstream blocker codes and
    has to work backwards to "you passed the wrong file".
    """
    if not isinstance(raw, dict):
        return ["the broker snapshot is not a JSON object"]
    problems: List[str] = []
    missing = [f for f in PROMOTION_ACCOUNT_FIELDS if raw.get(f) is None]
    assets = raw.get("assets")
    has_assets = isinstance(assets, dict) and bool(assets)
    has_legacy_quote = raw.get("quote_price_usd") is not None
    if missing:
        problems.append(
            "carries no %s. The capital-gate snapshot "
            "(state/broker_snapshot.json) gathers settled cash and nothing "
            "else, on purpose; promotion needs the broker's own order history "
            "too. Gather one with scripts/refresh_promotion_snapshot.sh."
            % " or ".join(missing))
    if not has_assets and not has_legacy_quote:
        problems.append(
            "carries no per-asset block. Promotion re-checks a refreshed quote "
            "and tradability for every proposed asset, which a cash-only "
            "snapshot cannot answer.")
    return problems


def snapshot_from_dict(
    raw: Any, now: datetime, symbol: Optional[str] = None
) -> BrokerSnapshot:
    """Build a :class:`BrokerSnapshot` for one asset from a gathered payload.

    Unlike the loader in ``scripts/execute_approved.py``, this one carries
    ``cash_usd``, ``unsettled_funds_usd`` and ``account_type`` — promotion is
    required to re-check **settled cash**, and preflight cannot do that from a
    snapshot that omits them. A missing field stays ``None`` and becomes a
    blocker; it is never defaulted into looking healthy.

    ``symbol`` selects the per-asset block. Passing none keeps the old
    single-asset behaviour for callers that have only one leg.
    """
    if not isinstance(raw, dict):
        raise PromotionError("the broker snapshot is not a JSON object")
    asset = asset_block(raw, symbol)
    return BrokerSnapshot(
        as_of=_stamp(raw.get("as_of")) or now,
        account_is_agentic=bool(raw.get("account_is_agentic")),
        account_masked=str(raw.get("account_masked") or "????"),
        equity_orders=raw.get("equity_orders"),
        crypto_orders=raw.get("crypto_orders"),
        buying_power_usd=_money(raw.get("buying_power_usd")),
        cash_usd=_money(raw.get("cash_usd")),
        unsettled_funds_usd=_money(raw.get("unsettled_funds_usd")),
        account_type=raw.get("account_type"),
        quote_price_usd=_money(asset.get("quote_price_usd")),
        quote_timestamp=_stamp(asset.get("quote_timestamp")),
        tradable=asset.get("tradable"),
        fractional_tradable=asset.get("fractional_tradable"),
        account_type_tradable=asset.get("account_type_tradable"),
        crypto_pair_halted=asset.get("crypto_pair_halted"),
        crypto_min_order_size=_money(asset.get("crypto_min_order_size")),
        read_errors=list(raw.get("read_errors") or []),
    )


def snapshot_coverage(snapshot: BrokerSnapshot, is_crypto: bool) -> List[str]:
    """Fields promotion needs that this snapshot does not carry.

    Reported as warnings *before* preflight runs, so the operator learns what to
    go and fetch rather than reading a wall of downstream blocker codes.
    """
    missing: List[str] = []
    if snapshot.quote_price_usd is None:
        missing.append("quote_price_usd (the refreshed quote)")
    if snapshot.quote_timestamp is None:
        missing.append("quote_timestamp (to age the quote)")
    if snapshot.cash_usd is None:
        missing.append("cash_usd (settled cash; buying power is not a substitute)")
    if snapshot.account_type is None:
        missing.append("account_type (to detect margin risk)")
    if snapshot.equity_orders is None and not is_crypto:
        missing.append("equity_orders (to reconcile this month's activity)")
    if snapshot.crypto_orders is None and is_crypto:
        missing.append("crypto_orders (to reconcile this month's activity)")
    if is_crypto:
        if snapshot.crypto_pair_halted is None:
            missing.append("crypto_pair_halted")
        if snapshot.crypto_min_order_size is None:
            missing.append("crypto_min_order_size")
    else:
        if snapshot.tradable is None:
            missing.append("tradable")
        if snapshot.fractional_tradable is None:
            missing.append("fractional_tradable (a $5-$25 order is fractional)")
    return missing


def slippage_tolerance(is_crypto: bool) -> Decimal:
    """The existing threshold. Promotion does not define its own."""
    return MAX_CRYPTO_SLIPPAGE_PCT if is_crypto else MAX_EQUITY_SLIPPAGE_PCT


def quote_age_limit(is_crypto: bool) -> int:
    return MAX_CRYPTO_QUOTE_AGE_SECONDS if is_crypto else MAX_EQUITY_QUOTE_AGE_SECONDS


def price_move_pct(recommended: Any, refreshed: Any) -> Optional[Decimal]:
    """Signed percentage move from the recommended price to the refreshed one."""
    try:
        was = parse_money(recommended)
        now_price = parse_money(refreshed)
    except MoneyError:
        return None
    if was <= ZERO:
        return None
    return ((now_price - was) / was * Decimal(100)).quantize(Decimal("0.01"))


def build_promoted_decision(
    leg: Dict[str, Any], decision_id: str, source_report: str, generated_at: str
) -> Dict[str, Any]:
    """The leg, as a normal decision payload with a real ``decision_id``.

    Everything else in the payload is the run's own work, carried across
    unchanged: promotion adds an identifier and provenance and edits nothing
    else. It deliberately does not touch amount, ticker, thesis or any field the
    fingerprint binds — a promotion that quietly rewrote the decision would make
    the approval meaningless.
    """
    decision = dict(leg)
    for key in FORBIDDEN_RECOMMENDATION_KEYS:
        decision.pop(key, None)
    # The recommendation schema lets a leg declare itself with any of the verb
    # fields; the decision schema the guardrails validate wants all three, in
    # their canonical casing. Promotion is precisely the seam between the two,
    # so it spells them out here rather than leaving the minted decision to be
    # rejected downstream as INVALID_DECISION. This is canonicalisation, not a
    # rewrite: it runs only when the leg is already unambiguously a BUY, and
    # `binding_view` lower-cases `action`/`side` before fingerprinting, so the
    # fingerprint is unchanged by it.
    if is_unambiguous_buy(decision):
        decision["decision"] = "BUY"
        decision["action"] = "buy"
        decision["side"] = "buy"
    decision["decision_id"] = decision_id
    decision["promoted_from"] = {
        "source": "SCHEDULED_EVALUATION",
        "source_report": source_report,
        "recommendation_generated_at": generated_at,
    }
    return decision


# --------------------------------------------------------------------------
# Verifying a promotion snapshot came from this gather
# --------------------------------------------------------------------------

#: A promotion snapshot older than this is not used. It is deliberately tight:
#: its whole purpose is a *refreshed* quote, and ``preflight`` will reject a
#: quote older than 300s (equity) or 60s (crypto) anyway. Catching it here names
#: the real problem — "the gather is stale" — rather than surfacing it as a
#: per-leg STALE_QUOTE the operator has to work backwards from.
MAX_PROMOTION_SNAPSHOT_AGE_SECONDS = 300


def promotion_refresh_problems(
    snapshot: Any,
    mtime: Optional[float],
    started_at: float,
    symbols: Sequence[str] = (),
    now: Optional[datetime] = None,
    max_age_seconds: float = MAX_PROMOTION_SNAPSHOT_AGE_SECONDS,
) -> List[str]:
    """Why a gather did NOT produce a usable promotion snapshot.

    Mirrors :func:`src.capital_gate.refresh_problems` deliberately, including
    the lesson behind it: "does a parseable snapshot exist" is the wrong
    question. A subprocess can exit 0 having written nothing, leaving the
    previous file on disk to be mistaken for a fresh reading. So the snapshot
    must also *post-date this invocation* and be fresh on its own ``as_of``.
    """
    problems: List[str] = []
    if snapshot is None:
        return ["no snapshot was written"]
    if not isinstance(snapshot, dict):
        return ["the snapshot is not a JSON object"]

    if mtime is None:
        problems.append("the snapshot file could not be stat'd")
    elif mtime < started_at:
        problems.append(
            "the snapshot predates this invocation, so it is a leftover rather "
            "than a refresh")

    stamp = _stamp(snapshot.get("as_of"))
    moment = now or datetime.now(timezone.utc)
    if stamp is None:
        problems.append("the snapshot carries no readable as_of")
    else:
        age = (moment - stamp).total_seconds()
        if age > max_age_seconds:
            problems.append(
                "the snapshot's as_of is %.0fs old, past the %.0fs limit"
                % (age, max_age_seconds))
        elif age < -60:
            problems.append("the snapshot's as_of is in the future")

    if not snapshot.get("account_is_agentic"):
        problems.append("the snapshot does not identify the agentic account")
    if _money(snapshot.get("cash_usd")) is None:
        problems.append("the snapshot carries no readable cash_usd")

    problems.extend(snapshot_problems(snapshot))

    for symbol in symbols:
        block = asset_block(snapshot, symbol)
        if not block:
            problems.append("the snapshot carries no block for %s" % symbol)
            continue
        if _money(block.get("quote_price_usd")) is None:
            problems.append("%s carries no readable quote_price_usd" % symbol)
        if _stamp(block.get("quote_timestamp")) is None:
            problems.append("%s carries no readable quote_timestamp" % symbol)
    return problems


def backfill_quote_timestamps(
    snapshot: Any, gathered_at: datetime
) -> List[str]:
    """Stamp any per-asset quote the gather left undated, conservatively.

    The gather reads a quote and is meant to record when. In practice the model
    driving it frequently omits ``quote_timestamp`` — the broker's quote payload
    does not always carry one — and an undated quote fails closed as
    ``STALE_QUOTE``, which is the right default and the wrong outcome here: the
    quote was demonstrably read during this invocation.

    So the *script* supplies what it actually observed, rather than asking the
    model to assert it. ``gathered_at`` is the moment the gather **started**, not
    its ``as_of``: the quote cannot have been read before the invocation began,
    so this can only make a quote look older than it is, never fresher. Erring
    toward stale keeps the quote-age check meaningful.

    Mutates ``snapshot`` in place and returns the symbols it stamped.
    """
    if not isinstance(snapshot, dict):
        return []
    assets = snapshot.get("assets")
    blocks: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(assets, dict):
        blocks = [(str(k), v) for k, v in assets.items() if isinstance(v, dict)]
    elif snapshot.get("quote_price_usd") is not None:
        blocks = [("", snapshot)]

    stamp = gathered_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    filled: List[str] = []
    for symbol, block in blocks:
        if _money(block.get("quote_price_usd")) is None:
            continue  # no quote to date; that stays a blocker
        if _stamp(block.get("quote_timestamp")) is None:
            block["quote_timestamp"] = stamp
            filled.append(symbol or "(top level)")
    return filled
