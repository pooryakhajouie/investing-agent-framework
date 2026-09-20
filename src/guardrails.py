"""Deterministic safety validation — the authoritative layer.

Nothing in this module asks the model for an opinion. It takes a proposed
decision (a plain dict, as produced by an AI evaluation), the loaded config, and
the current budget state, and it answers one question: *does this break a hard
rule?*

If a hard rule is broken, the proposal is invalid. The model's confidence,
reasoning, or insistence is irrelevant. There is deliberately **no override
parameter** anywhere in this module.

Version 2 adds crypto. Crypto was NOT enabled by deleting the old
``CRYPTO_FORBIDDEN`` rule and walking away; it was replaced by
:func:`_check_crypto_rules`, an explicit allow-list check against a snapshot of
Robinhood's own supported currency pairs. A crypto symbol that is not in that
snapshot, not tradable, halted, or below its minimum order size is rejected.

Live trading remains disabled: ``executable`` is always False and
:func:`assert_execution_allowed` always raises.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    ALLOWED_ASSET_TYPES,
    ASSET_CLASS_CRYPTO,
    ASSET_CLASS_EQUITY,
    ASSET_CLASS_ETF,
    ASSET_CLASS_FOR_TYPE,
    ASSET_CRYPTO,
    ASSET_US_COMMON_STOCK,
    ASSET_US_ETF,
    AVG_LOWERS,
    AVG_NEUTRAL,
    AVG_RAISES,
    BROAD_THEMES,
    BUYABLE_CLASSIFICATIONS,
    CENTS,
    CONFIDENCE_LEVELS,
    CRYPTO_PAIR_RE,
    DECISION_BUY,
    DECISION_WAIT,
    FORBIDDEN_ACTIONS,
    FORBIDDEN_ASSET_TYPES,
    FORBIDDEN_SIDES,
    INSUFFICIENT_TIMING_PATTERNS,
    MAJOR_US_EXCHANGES,
    MID_MONTH_DAY,
    MIN_EQUITY_ORDER_USD,
    MIN_PRIMARY_SOURCES_NEW_CRYPTO,
    MIN_PRIMARY_SOURCES_NEW_EQUITY,
    CRYPTO_WAIT_BUCKETS,
    NOT_APPLICABLE_PREFIX,
    NOT_AVAILABLE_PREFIX,
    OPTIONALITY_SPEND_THRESHOLD,
    POSITION_EXISTING,
    POSITION_NEW,
    PRIMARY_SOURCE_TOOLS,
    PRIMARY_SOURCE_TYPES,
    AUTHORIZATION_EXPIRY_FIELD,
    CRYPTO_PRIMARY_SOURCE_TYPES,
    EVENTS_FIELD,
    REQUIRED_EVENT_KEYS,
    ROBINHOOD_SCOPE_EXCUSE_PATTERNS,
    REQUIRED_NEW_POSITION_KEYS,
    REQUIRED_OPTIONALITY_KEYS,
    REQUIRED_SPRAWL_KEYS,
    REQUIRED_THEME_KEYS,
    WAIT_CANDIDATE_BUCKETS,
    RESEARCH_COMPONENTS_FOR_ASSET_TYPE,
    THEME_TAXONOMY,
    TICKER_RE,
    VALID_ASSET_CLASSES,
    VALID_AVG_DIRECTIONS,
    VALID_CLASSIFICATIONS,
    VALID_DECISIONS,
    VALID_POSITION_TYPES,
    ZERO,
    MoneyError,
    Proposal,
    ValidationResult,
    Violation,
    is_whole_cents,
    money_str,
    parse_money,
)
from .models import subthemes_for
from .lessons import (
    LESSON_MISUSE_FIELDS,
    overreach_match,
    references_lesson,
)
from .market_calendar import (
    ACTIONABLE_NEXT_MONTH_ONLY,
    VALID_EVENT_ASSET_CLASSES,
    describe_boundary,
    event_actionability,
    exchange_date,
    final_opportunity,
    is_trading_day,
    parse_event_date,
    project_date,
)
from .state import (
    CRYPTO_UNIVERSE_MAX_AGE_DAYS,
    BudgetState,
    Config,
    CryptoUniverse,
    CryptoUniverseError,
    load_crypto_universe,
    utc_now,
)

# --------------------------------------------------------------------------
# Thresholds. These are guardrail constants, not budget configuration. The one
# canonical dollar-budget source remains config.json -> monthly_budget_usd, and
# the horizon floor is config.json -> min_investment_horizon_months.
# --------------------------------------------------------------------------

MIN_SHARE_PRICE_USD = Decimal("1.00")  # below this is a penny stock: hard reject
LOW_PRICE_WARN_USD = Decimal("5.00")  # below this: warn
SMALL_CAP_WARN_USD = Decimal("300000000")  # below this market cap: warn

# Well-known leveraged / inverse exchange-traded products. Not exhaustive; the
# name-pattern scan below is the general net, and an unrecognized product still
# has to pass the asset-type and narrative checks.
KNOWN_LEVERAGED_INVERSE = {
    "AGQ", "BITX", "BNKU", "BOIL", "BULZ", "CHAU", "CONL", "CURE", "CWEB",
    "DDM", "DOG", "DPST", "DRN", "DRV", "DUST", "DXD", "EDC", "EDZ", "ERX",
    "ERY", "EURL", "FAS", "FAZ", "FNGD", "FNGU", "GDXD", "GDXU", "HIBL",
    "HIBS", "INDL", "JDST", "JNUG", "KOLD", "KORU", "LABD", "LABU", "MEXX",
    "MIDU", "MSTU", "MSTX", "MSTZ", "MVV", "NAIL", "NUGT", "NVDL", "NVDX",
    "PILL", "PSQ", "QID", "QLD", "RETL", "RWM", "SAA", "SCO", "SDOW", "SDS",
    "SH", "SOXL", "SOXS", "SPXL", "SPXS", "SPXU", "SQQQ", "SRTY", "SSO",
    "SVIX", "SVXY", "TECL", "TECS", "TMF", "TMV", "TNA", "TQQQ", "TSLL",
    "TSLQ", "TSLS", "TWM", "TZA", "UCO", "UDOW", "UPRO", "URTY", "UTSL",
    "UVIX", "UVXY", "UWM", "VIXY", "WEBL", "WEBS", "YANG", "YINN", "ZSL",
    # 2x/-1x single-crypto ETPs
    "BITI", "ETHU", "ETHD", "XXRP", "SOLT",
}

# Crypto-tracking ETPs. Direct crypto is now an allowed asset class, so these are
# no longer forbidden — but holding one alongside direct coins is duplicated
# exposure, and the thesis has to say so. Warning, not violation.
KNOWN_CRYPTO_ETPS = {
    "IBIT", "FBTC", "GBTC", "BITB", "ARKB", "BTCO", "HODL", "BRRR", "EZBC",
    "BTCW", "ETHA", "FETH", "ETHE", "ETHW", "CETH", "QETH", "BITO", "BTF",
}

# Single-commodity and volatility ETPs: allowed, but not obviously a durable
# long-term holding. Warning only.
KNOWN_SINGLE_COMMODITY_ETPS = {
    "GLD", "IAU", "SGOL", "SLV", "SIVR", "PPLT", "PALL", "USO", "UNG", "CPER",
    "VXX", "VIXM",
}

# Crypto assets whose primary driver is attention rather than utility. Allowed
# by the catalog, but the thesis must confront it. Warning only.
MEME_CRYPTO_CODES = {
    "DOGE", "SHIB", "PEPE", "BONK", "WIF", "FLOKI", "TRUMP", "MEW", "POPCAT",
    "PNUT", "MOODENG", "CASHCAT", "PENGU",
}

_LEVERAGE_NAME_PATTERNS = [
    re.compile(r"(?<![a-z0-9])[-+]?\d+(\.\d+)?\s*x(?![a-z])", re.I),  # "3X", "-1x", "2.5 X"
    re.compile(r"\bultra(pro|short)?\b", re.I),
    re.compile(r"\bleveraged\b", re.I),
    re.compile(r"\bgeared\b", re.I),
    re.compile(r"\bdaily\s+(bull|bear)\b", re.I),
]
_INVERSE_NAME_PATTERNS = [
    re.compile(r"\binverse\b", re.I),
    re.compile(r"\bultrashort\b", re.I),
    re.compile(r"\bbear\b", re.I),
    re.compile(r"\bshort\s+(qqq|s&p|dow|russell|term\s+bear)\b", re.I),
    re.compile(r"(?<![a-z0-9])-\d+(\.\d+)?\s*x(?![a-z])", re.I),
]
_OPTION_NAME_PATTERNS = [
    re.compile(r"\b(call|put)s?\b", re.I),
    re.compile(r"\b(covered\s+call|cash\s+secured\s+put|spread|straddle|strangle|iron\s+condor)\b", re.I),
]

# Narrative fields each decision type must carry, non-empty.
REQUIRED_BUY_NARRATIVE = (
    "thesis",
    "value_creation",
    "valuation_reasoning",
    "timing_reason",
    "why_not_wait",
    "margin_for_error",
    "alternatives_considered",
    "portfolio_exposure",
    "risks",
    "invalidation",
    "evidence",
)
REQUIRED_WAIT_NARRATIVE = (
    "thesis",
    "timing_reason",
    "alternatives_considered",
    "evidence",
)

# A WAIT must say WHICH kind of reason it rests on. Waiting for information is
# only one of these -- valuation and risk/reward stand on their own.
VALID_WAIT_BASES = (
    "VALUATION",
    "EXPECTED_RETURN_TOO_LOW",
    "RISK_REWARD_MEDIOCRE",
    "UNCERTAINTY_TOO_HIGH",
    "PRICE_EXTENDED",
    "BETTER_CANDIDATES_EXPECTED",
    "THRESHOLD_NOT_MET",
    "PRESERVING_MONTHLY_OPTIONALITY",
    "RESEARCH_INCOMPLETE",
    "AWAITING_INFORMATION",
)
# Bases that are self-sufficient: a WAIT resting on any of these needs no
# pending event to justify it.
STANDALONE_WAIT_BASES = frozenset(VALID_WAIT_BASES) - {"AWAITING_INFORMATION"}
# Keys a cost-basis analysis must carry when adding to an existing position.
REQUIRED_COST_BASIS_KEYS = (
    "average_cost_usd",
    "raises_or_lowers_average",
    "economic_meaning",
)



_INSUFFICIENT_TIMING_RES = [re.compile(p, re.I) for p in INSUFFICIENT_TIMING_PATTERNS]


# Keys that would represent a decision approving itself. Their mere presence in
# a decision payload is a violation.
SELF_APPROVAL_FIELDS = (
    "approved",
    "approval",
    "approved_by",
    "approval_id",
    "user_approved",
    "execution_state",
    "authorized",
    "execute",
)


class LiveTradingDisabled(RuntimeError):
    """Raised by :func:`assert_execution_allowed`. Always."""


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def _as_bool(value: Any) -> Tuple[bool, bool]:
    """Coerce a truthiness flag. Returns ``(value, was_present_and_understood)``."""
    if isinstance(value, bool):
        return value, True
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "y", "1"):
            return True, True
        if lowered in ("false", "no", "n", "0", ""):
            return False, True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value), True
    return False, False


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _nonempty_narrative(value: Any) -> bool:
    """True when a narrative field carries real content."""
    if isinstance(value, str):
        return len(value.strip()) >= 3
    if isinstance(value, (list, tuple)):
        return len(value) > 0 and any(_nonempty_narrative(item) for item in value)
    if isinstance(value, dict):
        return len(value) > 0
    return False


def normalize(raw: Dict[str, Any]) -> Tuple[Proposal, List[Violation]]:
    """Turn a raw decision dict into a :class:`Proposal`.

    Parse problems become violations rather than exceptions, so that malformed
    model output is *rejected* rather than crashing the validator.
    """
    violations: List[Violation] = []
    proposal = Proposal(raw=dict(raw) if isinstance(raw, dict) else {})

    if not isinstance(raw, dict):
        violations.append(
            Violation("MALFORMED_PROPOSAL", "decision must be a JSON object, got %s" % type(raw).__name__)
        )
        return proposal, violations

    proposal.decision_id = _text(raw.get("decision_id")) or ""
    proposal.decision = (_text(raw.get("decision")) or "").upper()
    proposal.action = (_text(raw.get("action")) or "").lower()
    proposal.asset_class = (_text(raw.get("asset_class")) or "").upper() or None
    proposal.position_type = (_text(raw.get("position_type")) or "").upper() or None
    proposal.classification = (_text(raw.get("classification")) or "").upper() or None
    # A crypto proposal may name its asset in `ticker` or in `symbol`.
    proposal.ticker = (
        (_text(raw.get("ticker")) or _text(raw.get("symbol")) or "").upper() or None
    )
    proposal.security_name = _text(raw.get("security_name"))
    proposal.asset_type = (_text(raw.get("asset_type")) or "").lower() or None
    proposal.exchange = (_text(raw.get("exchange")) or "").upper().replace(" ", "") or None
    proposal.side = (_text(raw.get("side")) or _text(raw.get("order_side")) or "buy").lower()
    proposal.confidence = (_text(raw.get("confidence")) or "").upper() or None

    horizon = raw.get("investment_horizon_months")
    if horizon is not None and horizon != "":
        try:
            proposal.investment_horizon_months = int(horizon)
        except (TypeError, ValueError):
            violations.append(
                Violation(
                    "MALFORMED_NUMBER",
                    "investment_horizon_months must be an integer, got %r" % (horizon,),
                )
            )

    amount_raw = raw.get("proposed_amount_usd", raw.get("proposed_amount"))
    if amount_raw is None or amount_raw == "":
        proposal.proposed_amount_usd = ZERO
    else:
        try:
            proposal.proposed_amount_usd = parse_money(amount_raw, "proposed_amount_usd")
        except MoneyError as exc:
            violations.append(Violation("MALFORMED_AMOUNT", str(exc)))
            proposal.proposed_amount_usd = ZERO
        if isinstance(amount_raw, float):
            proposal.raw.setdefault("_notes", []).append(
                "proposed_amount_usd arrived as a float; prefer a decimal string"
            )

    for field_name, attr in (
        ("current_price_usd", "current_price_usd"),
        ("market_cap_usd", "market_cap_usd"),
    ):
        value = raw.get(field_name)
        if value is None or value == "":
            continue
        try:
            setattr(proposal, attr, parse_money(value, field_name))
        except MoneyError as exc:
            violations.append(Violation("MALFORMED_NUMBER", str(exc)))

    for field_name, attr in (
        ("uses_margin", "uses_margin"),
        ("uses_options", "uses_options"),
        ("is_short", "is_short"),
        ("is_transfer", "is_transfer"),
    ):
        value, understood = _as_bool(raw.get(field_name, False))
        if raw.get(field_name) is not None and not understood:
            violations.append(
                Violation("MALFORMED_FLAG", "%s must be a boolean, got %r" % (field_name, raw[field_name]))
            )
        setattr(proposal, attr, value)

    for field_name, attr in (
        ("is_leveraged", "is_leveraged"),
        ("is_inverse", "is_inverse"),
        ("is_otc", "is_otc"),
        ("fractional_eligible", "fractional_eligible"),
    ):
        if field_name not in raw or raw[field_name] is None:
            continue
        value, understood = _as_bool(raw[field_name])
        if not understood:
            violations.append(
                Violation("MALFORMED_FLAG", "%s must be a boolean, got %r" % (field_name, raw[field_name]))
            )
            continue
        setattr(proposal, attr, value)

    # Legacy field: v1 decisions carried an is_crypto boolean. Honour it as a
    # hint toward the crypto asset class rather than as a rejection trigger.
    legacy_crypto, understood = _as_bool(raw.get("is_crypto", False))
    if legacy_crypto and understood and not proposal.asset_class:
        proposal.asset_class = ASSET_CLASS_CRYPTO
        if not proposal.asset_type:
            proposal.asset_type = ASSET_CRYPTO

    return proposal, violations


# --------------------------------------------------------------------------
# Prohibitions that apply to every decision, regardless of asset class
# --------------------------------------------------------------------------


def _check_prohibited_classes(
    proposal: Proposal, config: Config, violations: List[Violation], checks: List[str]
) -> None:
    checks.append("prohibited_asset_classes")

    haystack = " ".join(
        part
        for part in (
            proposal.security_name or "",
            proposal.asset_type or "",
            str(proposal.raw.get("order_type", "")),
            str(proposal.raw.get("instrument_type", "")),
        )
        if part
    )
    ticker = proposal.ticker or ""
    is_crypto = proposal.is_crypto

    # --- top-level action ---
    if proposal.action and proposal.action in FORBIDDEN_ACTIONS:
        violations.append(
            Violation(
                FORBIDDEN_ACTIONS[proposal.action],
                "action %r is forbidden by policy" % proposal.action,
            )
        )

    # --- side ---
    if proposal.side in FORBIDDEN_SIDES:
        violations.append(
            Violation(
                FORBIDDEN_SIDES[proposal.side],
                "order side %r is forbidden; only a long 'buy' is permitted" % proposal.side,
            )
        )

    # --- selling ---
    if not config.allow_selling and (
        proposal.decision == "SELL" or proposal.raw.get("is_sell") is True
    ):
        violations.append(Violation("SELL_FORBIDDEN", "selling existing investments is forbidden"))

    # --- options ---
    if proposal.uses_options and not config.allow_options:
        violations.append(Violation("OPTIONS_FORBIDDEN", "options are forbidden by policy"))
    if not is_crypto:
        # A crypto NAME can legitimately contain option-ish words; the option
        # scan only applies to securities.
        for pattern in _OPTION_NAME_PATTERNS:
            if haystack and pattern.search(haystack) and not config.allow_options:
                violations.append(
                    Violation(
                        "OPTIONS_FORBIDDEN",
                        "instrument description %r looks like an options position" % haystack.strip(),
                    )
                )
                break
    if proposal.raw.get("strike_price") or proposal.raw.get("expiration_date"):
        violations.append(
            Violation("OPTIONS_FORBIDDEN", "a strike price or expiration date implies an option contract")
        )

    # --- margin ---
    if proposal.uses_margin and not config.allow_margin:
        violations.append(Violation("MARGIN_FORBIDDEN", "margin / borrowing is forbidden by policy"))
    if str(proposal.raw.get("account_usage", "")).lower() in ("margin", "borrow", "leverage"):
        violations.append(Violation("MARGIN_FORBIDDEN", "margin account usage is forbidden by policy"))

    # --- shorting ---
    if proposal.is_short and not config.allow_shorting:
        violations.append(Violation("SHORT_FORBIDDEN", "short selling is forbidden by policy"))

    # --- transfers ---
    if proposal.is_transfer and not config.allow_transfers:
        violations.append(Violation("TRANSFER_FORBIDDEN", "money transfers are forbidden by policy"))

    # --- leveraged / inverse (equities only; crypto leverage is caught by the
    #     asset-type table and by the currency-pair allow-list) ---
    if not config.allow_leveraged_etfs:
        if proposal.is_leveraged is True:
            violations.append(
                Violation("LEVERAGED_PRODUCT_FORBIDDEN", "leveraged products are forbidden by policy")
            )
        elif ticker and not is_crypto and ticker in KNOWN_LEVERAGED_INVERSE:
            violations.append(
                Violation(
                    "LEVERAGED_PRODUCT_FORBIDDEN",
                    "%s is a known leveraged/inverse exchange-traded product" % ticker,
                )
            )
        elif not is_crypto:
            for pattern in _LEVERAGE_NAME_PATTERNS:
                if haystack and pattern.search(haystack):
                    violations.append(
                        Violation(
                            "LEVERAGED_PRODUCT_FORBIDDEN",
                            "security name %r matches a leveraged-product pattern (%s)"
                            % (haystack.strip(), pattern.pattern),
                        )
                    )
                    break

    if not config.allow_inverse_etfs:
        if proposal.is_inverse is True:
            violations.append(
                Violation("INVERSE_PRODUCT_FORBIDDEN", "inverse products are forbidden by policy")
            )
        elif not is_crypto:
            for pattern in _INVERSE_NAME_PATTERNS:
                if haystack and pattern.search(haystack):
                    violations.append(
                        Violation(
                            "INVERSE_PRODUCT_FORBIDDEN",
                            "security name %r matches an inverse-product pattern (%s)"
                            % (haystack.strip(), pattern.pattern),
                        )
                    )
                    break

    # --- OTC ---
    if proposal.is_otc is True:
        violations.append(Violation("OTC_FORBIDDEN", "OTC securities are forbidden by policy"))

    # --- forbidden asset types ---
    if proposal.asset_type and proposal.asset_type in FORBIDDEN_ASSET_TYPES:
        code = FORBIDDEN_ASSET_TYPES[proposal.asset_type]
        allowed_by_config = {
            "OPTIONS_FORBIDDEN": config.allow_options,
            "LEVERAGED_PRODUCT_FORBIDDEN": config.allow_leveraged_etfs,
            "INVERSE_PRODUCT_FORBIDDEN": config.allow_inverse_etfs,
        }.get(code, False)
        if not allowed_by_config:
            violations.append(
                Violation(code, "asset_type %r is forbidden by policy" % proposal.asset_type)
            )


# --------------------------------------------------------------------------
# Equity-specific rules
# --------------------------------------------------------------------------


def _check_equity_rules(
    proposal: Proposal,
    violations: List[Violation],
    warnings: List[str],
    checks: List[str],
) -> None:
    checks.append("equity_ticker_wellformed")
    ticker = proposal.ticker
    if not ticker:
        violations.append(Violation("MISSING_TICKER", "an equity BUY must name a ticker"))
    elif not TICKER_RE.match(ticker):
        violations.append(
            Violation("MALFORMED_TICKER", "%r is not a well-formed U.S. equity ticker" % ticker)
        )

    checks.append("major_us_exchange")
    if proposal.exchange and proposal.exchange not in MAJOR_US_EXCHANGES:
        violations.append(
            Violation(
                "OTC_FORBIDDEN",
                "exchange %r is not a recognized major U.S. exchange" % proposal.exchange,
            )
        )

    checks.append("penny_stock_rejected")
    price = proposal.current_price_usd
    amount = proposal.proposed_amount_usd
    if price is not None:
        if price <= ZERO:
            violations.append(
                Violation("MALFORMED_NUMBER", "current_price_usd must be greater than zero")
            )
        elif price < MIN_SHARE_PRICE_USD:
            violations.append(
                Violation(
                    "PENNY_STOCK_FORBIDDEN",
                    "share price %s is below the %s penny-stock floor"
                    % (money_str(price), money_str(MIN_SHARE_PRICE_USD)),
                )
            )
        elif price < LOW_PRICE_WARN_USD:
            warnings.append(
                "share price %s is under %s; the thesis must justify a low-priced security"
                % (money_str(price), money_str(LOW_PRICE_WARN_USD))
            )
    else:
        warnings.append("no current_price_usd supplied; the penny-stock price check could not run")

    if proposal.market_cap_usd is not None and proposal.market_cap_usd < SMALL_CAP_WARN_USD:
        warnings.append(
            "market cap %s is under %s; small-cap speculation is discouraged"
            % (money_str(proposal.market_cap_usd), money_str(SMALL_CAP_WARN_USD))
        )

    checks.append("fractional_feasibility")
    if (
        price is not None
        and price > ZERO
        and amount > ZERO
        and amount < price
        and proposal.fractional_eligible is False
    ):
        violations.append(
            Violation(
                "FRACTIONAL_NOT_ELIGIBLE",
                "%s of %s buys less than one share at %s, and the security is not "
                "fractional-eligible" % (money_str(amount), ticker, money_str(price)),
            )
        )
    if proposal.fractional_eligible is None and price is not None and amount < price:
        warnings.append(
            "fractional_eligible was not supplied; confirm fractional eligibility with "
            "get_equity_tradability before this could ever be executed"
        )

    if ticker and ticker in KNOWN_CRYPTO_ETPS:
        warnings.append(
            "%s is a crypto-tracking ETP; direct crypto is now an allowed asset class, "
            "so confirm this is not duplicated exposure to coins already held" % ticker
        )
    if ticker and ticker in KNOWN_SINGLE_COMMODITY_ETPS:
        warnings.append(
            "%s is a single-commodity or volatility ETP, which produces no cash flow; "
            "the thesis must justify it as a long-term holding" % ticker
        )
    if proposal.asset_type == ASSET_US_ETF and not proposal.security_name:
        warnings.append(
            "no security_name supplied for an ETF; the leveraged/inverse name scan is weaker without it"
        )


# --------------------------------------------------------------------------
# Crypto-specific rules  (the positive allow-list path)
# --------------------------------------------------------------------------


def _check_crypto_rules(
    proposal: Proposal,
    config: Config,
    universe: Optional[CryptoUniverse],
    universe_error: Optional[str],
    violations: List[Violation],
    warnings: List[str],
    checks: List[str],
) -> None:
    checks.append("crypto_allowed_by_config")
    if not config.allow_crypto:
        violations.append(
            Violation("CRYPTO_FORBIDDEN", "cryptocurrency is forbidden by the current config")
        )
        return

    checks.append("crypto_symbol_wellformed")
    symbol = proposal.ticker
    if not symbol:
        violations.append(Violation("MISSING_TICKER", "a crypto BUY must name a currency pair"))
        return
    if not CRYPTO_PAIR_RE.match(symbol):
        violations.append(
            Violation(
                "MALFORMED_CRYPTO_PAIR",
                "%r is not a Robinhood USD currency pair; use the hyphenated form, e.g. 'BTC-USD'"
                % symbol,
            )
        )
        return

    checks.append("crypto_universe_available")
    if universe is None:
        violations.append(
            Violation(
                "CRYPTO_UNIVERSE_UNAVAILABLE",
                "the Robinhood currency-pair snapshot could not be loaded, so no crypto "
                "proposal can be validated: %s" % (universe_error or "unknown error"),
            )
        )
        return

    age = universe.age_days()
    if age is not None and age > CRYPTO_UNIVERSE_MAX_AGE_DAYS:
        warnings.append(
            "the crypto currency-pair snapshot is %.0f days old (limit %d); refresh it with "
            "get_currency_pairs before relying on tradability"
            % (age, CRYPTO_UNIVERSE_MAX_AGE_DAYS)
        )

    checks.append("crypto_pair_supported_by_robinhood")
    pair = universe.get(symbol)
    if pair is None:
        violations.append(
            Violation(
                "CRYPTO_PAIR_NOT_SUPPORTED",
                "%s is not in Robinhood's supported currency-pair list; it cannot be bought "
                "in this account" % symbol,
            )
        )
        return

    checks.append("crypto_pair_tradable")
    if pair.tradability != "tradable":
        violations.append(
            Violation(
                "CRYPTO_PAIR_NOT_TRADABLE",
                "%s has Robinhood tradability %r" % (symbol, pair.tradability),
            )
        )
    if pair.individual_tradability != "tradable":
        violations.append(
            Violation(
                "CRYPTO_PAIR_NOT_TRADABLE",
                "%s is not tradable in an individual brokerage account (%r)"
                % (symbol, pair.individual_tradability),
            )
        )
    if pair.display_only:
        violations.append(
            Violation("CRYPTO_PAIR_NOT_TRADABLE", "%s is display-only on Robinhood" % symbol)
        )

    checks.append("crypto_pair_not_halted")
    if pair.halted:
        violations.append(
            Violation(
                "CRYPTO_PAIR_HALTED",
                "%s is currently halted on Robinhood%s"
                % (
                    symbol,
                    " (regions: %s)" % ", ".join(pair.halted_regions) if pair.halted_regions else "",
                ),
            )
        )
    elif pair.halted_regions:
        warnings.append(
            "%s has a regional trading halt in %s" % (symbol, ", ".join(pair.halted_regions))
        )

    checks.append("crypto_min_order_size")
    price = proposal.current_price_usd
    amount = proposal.proposed_amount_usd
    if price is None:
        warnings.append(
            "no current_price_usd supplied for %s; the minimum-order-size check could not run"
            % symbol
        )
    elif price <= ZERO:
        violations.append(
            Violation("MALFORMED_NUMBER", "current_price_usd must be greater than zero")
        )
    elif pair.min_order_size is not None and amount > ZERO:
        try:
            units = amount / price
        except (InvalidOperation, ZeroDivisionError):  # pragma: no cover - guarded above
            units = ZERO
        if units < pair.min_order_size:
            violations.append(
                Violation(
                    "CRYPTO_BELOW_MIN_ORDER_SIZE",
                    "%s of %s buys %s units at %s, below Robinhood's minimum order size of %s"
                    % (
                        money_str(amount),
                        symbol,
                        units.normalize(),
                        money_str(price),
                        pair.min_order_size.normalize(),
                    ),
                )
            )

    # Fields that only make sense for a listed security must not appear.
    checks.append("crypto_has_no_equity_fields")
    if proposal.exchange:
        violations.append(
            Violation(
                "CRYPTO_FIELD_MISMATCH",
                "a crypto proposal must not carry an 'exchange' (got %r)" % proposal.exchange,
            )
        )

    code = (pair.code or symbol.split("-")[0]).upper()
    if code in MEME_CRYPTO_CODES:
        warnings.append(
            "%s is an attention-driven / meme asset; the thesis must justify it on durable "
            "relevance, not popularity or price per token" % symbol
        )


# --------------------------------------------------------------------------
# Rules common to every BUY
# --------------------------------------------------------------------------


def _check_buy_common(
    proposal: Proposal,
    config: Config,
    state: BudgetState,
    now: datetime,
    violations: List[Violation],
    warnings: List[str],
    checks: List[str],
) -> None:
    # --- explicit BUY representation ---
    checks.append("explicit_buy_representation")
    if proposal.side != "buy":
        violations.append(
            Violation("NOT_EXPLICIT_BUY", "a BUY decision must carry side='buy', got %r" % proposal.side)
        )
    if proposal.action and proposal.action not in ("buy", "purchase", ""):
        violations.append(
            Violation("NOT_EXPLICIT_BUY", "a BUY decision must carry action='buy', got %r" % proposal.action)
        )

    # --- amount ---
    checks.append("amount_positive_and_whole_cents")
    amount = proposal.proposed_amount_usd
    if amount == ZERO:
        violations.append(
            Violation("ZERO_AMOUNT", "a BUY decision must propose an amount greater than zero")
        )
    elif amount < ZERO:
        violations.append(
            Violation("NEGATIVE_AMOUNT", "proposed amount %s is negative" % money_str(amount))
        )
    elif not is_whole_cents(amount):
        violations.append(
            Violation("SUB_CENT_AMOUNT", "proposed amount %s is not a whole number of cents" % amount)
        )

    # --- budget (shared across ALL asset classes) ---
    checks.append("monthly_budget_not_exceeded")
    checks.append("cumulative_monthly_budget_not_exceeded")
    if amount > ZERO:
        if amount > config.monthly_budget_usd:
            violations.append(
                Violation(
                    "EXCEEDS_MONTHLY_BUDGET",
                    "proposed %s exceeds the authorized monthly budget of %s"
                    % (money_str(amount), money_str(config.monthly_budget_usd)),
                )
            )
        if amount > state.remaining_usd:
            violations.append(
                Violation(
                    "EXCEEDS_REMAINING_BUDGET",
                    "proposed %s exceeds the %s remaining in the %s authorization"
                    % (money_str(amount), money_str(state.remaining_usd), state.month),
                )
            )
        cumulative = state.committed_usd + amount
        if cumulative > state.authorized_budget_usd:
            violations.append(
                Violation(
                    "EXCEEDS_CUMULATIVE_MONTHLY_BUDGET",
                    "this purchase would bring %s purchases to %s, over the authorized %s"
                    % (state.month, money_str(cumulative), money_str(state.authorized_budget_usd)),
                )
            )

    # --- asset class / type agreement ---
    checks.append("asset_class_and_type_agree")
    if not proposal.asset_type:
        violations.append(Violation("MISSING_ASSET_TYPE", "a BUY decision must state an asset_type"))
    elif proposal.asset_type not in ALLOWED_ASSET_TYPES:
        if proposal.asset_type not in FORBIDDEN_ASSET_TYPES:
            violations.append(
                Violation(
                    "ASSET_TYPE_NOT_ALLOWED",
                    "asset_type %r is not in the allow-list %s"
                    % (proposal.asset_type, list(ALLOWED_ASSET_TYPES)),
                )
            )
    if not proposal.asset_class:
        violations.append(
            Violation("MISSING_ASSET_CLASS", "a BUY decision must state an asset_class")
        )
    elif proposal.asset_class not in VALID_ASSET_CLASSES:
        violations.append(
            Violation(
                "INVALID_ASSET_CLASS",
                "asset_class must be one of %s, got %r"
                % (list(VALID_ASSET_CLASSES), proposal.asset_class),
            )
        )
    elif proposal.asset_type in ASSET_CLASS_FOR_TYPE:
        expected = ASSET_CLASS_FOR_TYPE[proposal.asset_type]
        if proposal.asset_class != expected:
            violations.append(
                Violation(
                    "ASSET_CLASS_MISMATCH",
                    "asset_type %r implies asset_class %r, but %r was declared"
                    % (proposal.asset_type, expected, proposal.asset_class),
                )
            )

    # --- position type ---
    checks.append("position_type_declared")
    if not proposal.position_type:
        violations.append(
            Violation(
                "MISSING_POSITION_TYPE",
                "a BUY decision must declare position_type (%s or %s)"
                % (POSITION_EXISTING, POSITION_NEW),
            )
        )
    elif proposal.position_type not in VALID_POSITION_TYPES:
        violations.append(
            Violation(
                "INVALID_POSITION_TYPE",
                "position_type must be one of %s, got %r"
                % (list(VALID_POSITION_TYPES), proposal.position_type),
            )
        )

    # --- investment horizon ---
    checks.append("long_term_horizon")
    horizon = proposal.investment_horizon_months
    if horizon is None:
        violations.append(
            Violation(
                "MISSING_REQUIRED_FIELD",
                "a BUY decision must state investment_horizon_months",
            )
        )
    elif horizon < config.min_investment_horizon_months:
        violations.append(
            Violation(
                "HORIZON_TOO_SHORT",
                "investment_horizon_months of %d is below the %d-month minimum; this agent "
                "is a long-term investor, not a trader"
                % (horizon, config.min_investment_horizon_months),
            )
        )

    # --- classification ---
    checks.append("classification_supports_a_buy")
    if not proposal.classification:
        violations.append(
            Violation("MISSING_REQUIRED_FIELD", "a BUY decision must state a classification")
        )
    elif proposal.classification not in VALID_CLASSIFICATIONS:
        violations.append(
            Violation(
                "INVALID_CLASSIFICATION",
                "classification %r is not one of %s"
                % (proposal.classification, list(VALID_CLASSIFICATIONS)),
            )
        )
    elif proposal.classification not in BUYABLE_CLASSIFICATIONS:
        violations.append(
            Violation(
                "CLASSIFICATION_CONTRADICTS_BUY",
                "classification %r does not support a purchase; a BUY must be one of %s"
                % (proposal.classification, list(BUYABLE_CLASSIFICATIONS)),
            )
        )

    # --- budget arithmetic the model reported must match reality ---
    checks.append("reported_budget_arithmetic")
    _check_reported_budget(proposal, state, violations, warnings)

    # --- narrative completeness ---
    checks.append("required_buy_fields_present")
    for name in REQUIRED_BUY_NARRATIVE:
        if not _nonempty_narrative(proposal.raw.get(name)):
            violations.append(
                Violation("MISSING_REQUIRED_FIELD", "a BUY decision must include a non-empty %r" % name)
            )

    # --- version 3: capital is a month-long option, not a today-only budget ---
    _check_broker_minimum(proposal, violations, checks)
    _check_monthly_optionality(proposal, config, state, now, violations, warnings, checks)
    _check_timing_is_not_merely_absence_of_information(proposal, warnings, checks)
    _check_portfolio_sprawl(proposal, violations, checks)
    _check_theme_precision(proposal, violations, checks)

    # --- cost-basis analysis when adding to an existing position ---
    if proposal.position_type == POSITION_EXISTING:
        _check_cost_basis(proposal, violations, warnings, checks)

    # --- a new ticker must clear a real hurdle ---
    if proposal.position_type == POSITION_NEW:
        _check_new_position_hurdle(proposal, violations, warnings, checks)
        _check_research_completeness(proposal, violations, warnings, checks)
        _check_primary_sources(proposal, violations, warnings, checks)


def _check_cost_basis(
    proposal: Proposal,
    violations: List[Violation],
    warnings: List[str],
    checks: List[str],
) -> None:
    """Adding to a holding requires an honest cost-basis analysis.

    The direction the average moves is arithmetic, not opinion, so it is checked
    against the actual numbers. This is the deterministic guard against
    "averaging down" reasoning that has quietly inverted the arithmetic.
    """
    checks.append("cost_basis_analysis_present_and_consistent")
    analysis = proposal.raw.get("cost_basis_analysis")
    if not isinstance(analysis, dict) or not analysis:
        violations.append(
            Violation(
                "MISSING_COST_BASIS_ANALYSIS",
                "adding to an existing position requires a cost_basis_analysis object",
            )
        )
        return

    for key in REQUIRED_COST_BASIS_KEYS:
        value = analysis.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            violations.append(
                Violation(
                    "MISSING_COST_BASIS_ANALYSIS",
                    "cost_basis_analysis must include a non-empty %r" % key,
                )
            )

    direction = (_text(analysis.get("raises_or_lowers_average")) or "").upper()
    if direction and direction not in VALID_AVG_DIRECTIONS:
        violations.append(
            Violation(
                "INVALID_COST_BASIS_DIRECTION",
                "raises_or_lowers_average must be one of %s, got %r"
                % (list(VALID_AVG_DIRECTIONS), direction),
            )
        )
        return

    avg_raw = analysis.get("average_cost_usd")
    price = proposal.current_price_usd
    if avg_raw in (None, "") or price is None:
        warnings.append(
            "cost-basis direction could not be checked arithmetically without both "
            "average_cost_usd and current_price_usd"
        )
        return
    try:
        average = parse_money(avg_raw, "cost_basis_analysis.average_cost_usd")
    except MoneyError as exc:
        violations.append(Violation("MALFORMED_NUMBER", str(exc)))
        return
    if average <= ZERO:
        violations.append(
            Violation("MALFORMED_NUMBER", "cost_basis_analysis.average_cost_usd must be positive")
        )
        return

    expected = AVG_NEUTRAL
    if price > average:
        expected = AVG_RAISES
    elif price < average:
        expected = AVG_LOWERS
    if direction and direction != expected:
        violations.append(
            Violation(
                "COST_BASIS_DIRECTION_WRONG",
                "buying at %s against an average cost of %s %s the average, but the decision "
                "claims it %s it"
                % (money_str(price), money_str(average), expected, direction),
            )
        )


# --------------------------------------------------------------------------
# Version 3 checks: optionality, hurdle, research gate, sources, sprawl, theme
# --------------------------------------------------------------------------


def _dict_block(proposal: Proposal, name: str) -> Optional[Dict[str, Any]]:
    value = proposal.raw.get(name)
    return value if isinstance(value, dict) and value else None


def _missing_keys(block: Dict[str, Any], keys: Tuple[str, ...]) -> List[str]:
    missing = []
    for key in keys:
        value = block.get(key)
        if value is None:
            missing.append(key)
        elif isinstance(value, str) and not value.strip():
            missing.append(key)
        elif isinstance(value, (list, tuple, dict)) and len(value) == 0:
            missing.append(key)
    return missing


def _check_broker_minimum(
    proposal: Proposal, violations: List[Violation], checks: List[str]
) -> None:
    """Partial deployment is allowed, but not below what the broker will accept."""
    checks.append("meets_broker_minimum_order")
    amount = proposal.proposed_amount_usd
    if amount <= ZERO or proposal.is_crypto:
        return  # crypto has its own per-pair minimum check
    if amount < MIN_EQUITY_ORDER_USD:
        violations.append(
            Violation(
                "BELOW_BROKER_MINIMUM",
                "%s is below the %s minimum for a dollar-based fractional equity order"
                % (money_str(amount), money_str(MIN_EQUITY_ORDER_USD)),
            )
        )


def _check_monthly_optionality(
    proposal: Proposal,
    config: Config,
    state: BudgetState,
    now: datetime,
    violations: List[Violation],
    warnings: List[str],
    checks: List[str],
) -> None:
    """The $25 is a month-long option, not a today-only budget.

    Spending today forfeits the ability to act on anything that happens later in
    the same calendar month. Every BUY must show it considered that. A BUY that
    would push cumulative spending past half the authorization before mid-month
    must additionally justify surrendering that flexibility in writing.
    """
    checks.append("monthly_optionality_considered")

    block = _dict_block(proposal, "monthly_optionality")
    if block is None:
        violations.append(
            Violation(
                "MISSING_OPTIONALITY_ANALYSIS",
                "every BUY must carry a 'monthly_optionality' object: spending today "
                "forfeits the ability to act later in the same month",
            )
        )
    else:
        missing = _missing_keys(block, REQUIRED_OPTIONALITY_KEYS)
        if missing:
            violations.append(
                Violation(
                    "MISSING_OPTIONALITY_ANALYSIS",
                    "monthly_optionality is missing non-empty %s" % ", ".join(missing),
                )
            )
        reported_days = block.get("days_remaining_in_month")
        if isinstance(reported_days, int) and not isinstance(reported_days, bool):
            actual = days_remaining_in_month(now)
            if reported_days != actual:
                violations.append(
                    Violation(
                        "OPTIONALITY_MISREPORTED",
                        "monthly_optionality.days_remaining_in_month says %d, but %d days "
                        "remain in %s" % (reported_days, actual, state.month),
                    )
                )

        # Calendar days are not opportunities. Equities trade only in sessions,
        # so at month end the number that matters is how many sessions are left
        # -- and the last one of them is the edge past which this month's
        # authorization cannot reach. Optional to report, checked when reported.
        reported_sessions = block.get("tradable_sessions_remaining")
        if isinstance(reported_sessions, int) and not isinstance(reported_sessions, bool):
            actual_sessions = tradable_sessions_remaining(now)
            if reported_sessions != actual_sessions:
                violations.append(
                    Violation(
                        "OPTIONALITY_MISREPORTED",
                        "monthly_optionality.tradable_sessions_remaining says %d, but %d "
                        "US equity sessions remain in %s (the last is %s)"
                        % (
                            reported_sessions,
                            actual_sessions,
                            state.month,
                            final_opportunity(now.year, now.month, "EQUITY").strftime(
                                "%Y-%m-%d %H:%M"
                            ),
                        ),
                    )
                )

    # --- the mid-month majority-spend gate ---
    checks.append("majority_spend_before_mid_month_justified")
    amount = proposal.proposed_amount_usd
    if amount <= ZERO:
        return
    cumulative = state.committed_usd + amount
    threshold = (state.authorized_budget_usd * OPTIONALITY_SPEND_THRESHOLD).quantize(CENTS)
    if cumulative > threshold and now.day < MID_MONTH_DAY:
        justification = proposal.raw.get("monthly_optionality_analysis")
        if not _nonempty_narrative(justification) or (
            isinstance(justification, str) and len(justification.strip()) < 120
        ):
            violations.append(
                Violation(
                    "MISSING_OPTIONALITY_ANALYSIS",
                    "this purchase would bring %s spending to %s, above the %s "
                    "half-authorization mark, on day %d of the month. A substantive "
                    "'monthly_optionality_analysis' is required explaining why "
                    "surrendering the rest of the month's flexibility is justified."
                    % (
                        state.month,
                        money_str(cumulative),
                        money_str(threshold),
                        now.day,
                    ),
                )
            )
        else:
            warnings.append(
                "committing %s of the %s authorization on day %d of the month; the "
                "optionality analysis was supplied and must genuinely justify it"
                % (money_str(cumulative), money_str(state.authorized_budget_usd), now.day)
            )


def days_remaining_in_month(now: Optional[datetime] = None) -> int:
    """Calendar days left in the authorization month, including today.

    Read on the **project** clock, not UTC. The monthly authorization is the
    owner's calendar question; answering it in UTC made the count roll over
    while neither the owner's date nor the market's had, which invalidated an
    otherwise-sound decision for reporting a number that was correct when it
    was written.
    """
    today = project_date(now)
    last_day = calendar.monthrange(today.year, today.month)[1]
    return last_day - today.day + 1


def tradable_sessions_remaining(now: Optional[datetime] = None) -> int:
    """US equity sessions left in the month, counting today if it is one.

    The honest denominator for month-end optionality. Twelve calendar days can
    be eight sessions, and only the last of them can spend this month's $25.

    Read on the **exchange** clock: how many NYSE sessions remain is a question
    about the exchange's own date, not UTC's and not the owner's.
    """
    today = exchange_date(now)
    last_day = calendar.monthrange(today.year, today.month)[1]
    return sum(
        1
        for day in range(today.day, last_day + 1)
        if is_trading_day(date(today.year, today.month, day))
    )


def _check_timing_is_not_merely_absence_of_information(
    proposal: Proposal, warnings: List[str], checks: List[str]
) -> None:
    """'Nothing to wait for' is not a reason to buy."""
    checks.append("timing_not_merely_absence_of_information")
    blob = " ".join(
        str(proposal.raw.get(key, ""))
        for key in ("timing_reason", "why_not_wait")
        if isinstance(proposal.raw.get(key), str)
    )
    for pattern in _INSUFFICIENT_TIMING_RES:
        if pattern.search(blob):
            warnings.append(
                "the timing argument appeals to the absence of upcoming information "
                "(matched %r). That is never sufficient on its own: a good business can "
                "still be a bad investment at today's price. The buy must rest on "
                "expected return from the current entry price." % pattern.pattern
            )
            break


# --------------------------------------------------------------------------
# The calendar boundary: an event after the month's last tradable moment
# --------------------------------------------------------------------------
#
# The $25 does not carry into the next month, so "later this month" has a hard
# edge. A company reporting after the close on the last trading day of the
# month lands inside the month on a calendar and outside it in every way that
# matters: the first session anyone could respond in belongs to the next month,
# funded by the next month's authorization.
#
# Waiting through such an event is a perfectly good decision. What is not
# acceptable is describing the event as information this month's budget can act
# on, or letting the authorization lapse without saying that is what is
# happening. WAIT is a first-class outcome; an unnoticed expiry is not.


def _wait_bases(proposal: Proposal) -> List[str]:
    """The declared ``wait_basis`` values, normalized."""
    raw = proposal.raw.get("wait_basis")
    if isinstance(raw, str):
        items = [raw]
    elif isinstance(raw, (list, tuple)):
        items = [b for b in raw if isinstance(b, str)]
    else:
        items = []
    return [b.strip().upper() for b in items]


def _event_after_close(event: Dict[str, Any]) -> bool:
    """Whether an event's information arrives after that day's session ends.

    Accepts either ``occurs_after_close: true`` or the more descriptive
    ``session_timing: "AFTER_CLOSE"`` (vs ``BEFORE_OPEN`` / ``INTRADAY``).
    """
    if event.get("occurs_after_close") is True:
        return True
    timing = _text(event.get("session_timing")) or ""
    return timing.strip().upper() == "AFTER_CLOSE"


def _check_event_actionability(
    proposal: Proposal,
    now: datetime,
    violations: List[Violation],
    warnings: List[str],
    checks: List[str],
) -> None:
    """Every dated event must be classified against the month's last opportunity."""
    checks.append("event_actionability_respects_the_month_boundary")

    events = proposal.raw.get(EVENTS_FIELD)
    bases = _wait_bases(proposal)

    if events is None:
        # Waiting *for information* is the one basis that depends entirely on
        # when the information arrives, so it has to name the events.
        if proposal.decision == DECISION_WAIT and "AWAITING_INFORMATION" in bases:
            violations.append(
                Violation(
                    "MISSING_EVENT_ACTIONABILITY",
                    "a WAIT resting on AWAITING_INFORMATION must list %r: each event "
                    "needs a 'label', a 'date', and "
                    "'actionable_with_this_month_authorization'. An event after the "
                    "month's final tradable opportunity is next month's information, "
                    "and this month's %s cannot be spent on it."
                    % (EVENTS_FIELD, "authorization"),
                )
            )
        return

    if not isinstance(events, (list, tuple)):
        violations.append(
            Violation(
                "MALFORMED_EVENT_LIST",
                "%r must be a list of event objects, got %s"
                % (EVENTS_FIELD, type(events).__name__),
            )
        )
        return

    next_month_only: List[str] = []

    for index, event in enumerate(events):
        if not isinstance(event, dict):
            violations.append(
                Violation(
                    "MALFORMED_EVENT_LIST",
                    "%s[%d] must be an object with a label and a date" % (EVENTS_FIELD, index),
                )
            )
            continue

        missing = _missing_keys(event, REQUIRED_EVENT_KEYS)
        if missing:
            violations.append(
                Violation(
                    "MISSING_EVENT_ACTIONABILITY",
                    "%s[%d] is missing non-empty %s"
                    % (EVENTS_FIELD, index, ", ".join(missing)),
                )
            )

        label = _text(event.get("label")) or "%s[%d]" % (EVENTS_FIELD, index)

        event_date, date_error = parse_event_date(_text(event.get("date")) or "")
        if event_date is None:
            violations.append(
                Violation("MALFORMED_EVENT_DATE", "%s: %s" % (label, date_error))
            )
            continue

        asset_class = (_text(event.get("asset_class")) or "EQUITY").strip().upper()
        if asset_class not in VALID_EVENT_ASSET_CLASSES:
            violations.append(
                Violation(
                    "INVALID_EVENT_ASSET_CLASS",
                    "%s declares asset_class %r; an event must be classified against a "
                    "market that has a closing bell or one that does not, so it must be "
                    "one of %s" % (label, asset_class, list(VALID_EVENT_ASSET_CLASSES)),
                )
            )
            continue

        after_close = _event_after_close(event)
        verdict = event_actionability(event_date, asset_class, after_close, reference=now)
        boundary = describe_boundary(event_date, asset_class, after_close, reference=now)

        declared = event.get("actionable_with_this_month_authorization")
        if isinstance(declared, bool):
            expected = verdict != ACTIONABLE_NEXT_MONTH_ONLY
            if declared != expected:
                violations.append(
                    Violation(
                        "EVENT_ACTIONABILITY_MISREPORTED",
                        "%s (%s%s, %s) is declared %s with this month's authorization, "
                        "but %s"
                        % (
                            label,
                            event_date.isoformat(),
                            " after the close" if after_close else "",
                            asset_class,
                            "actionable" if declared else "not actionable",
                            boundary["explanation"],
                        ),
                    )
                )
        elif declared is not None:
            violations.append(
                Violation(
                    "MISSING_EVENT_ACTIONABILITY",
                    "%s: 'actionable_with_this_month_authorization' must be true or "
                    "false, got %r" % (label, declared),
                )
            )

        if verdict == ACTIONABLE_NEXT_MONTH_ONLY:
            next_month_only.append(
                "%s on %s%s (first tradable %s)"
                % (
                    label,
                    event_date.isoformat(),
                    " after the close" if after_close else "",
                    boundary["first_actionable_session"],
                )
            )

    if not next_month_only:
        return

    # Something being waited on lands past the edge. Say so out loud.
    checks.append("authorization_expiry_stated_when_waiting_past_the_month")
    acknowledgement = proposal.raw.get(AUTHORIZATION_EXPIRY_FIELD)
    if not _nonempty_narrative(acknowledgement):
        violations.append(
            Violation(
                "UNACKNOWLEDGED_AUTHORIZATION_EXPIRY",
                "these events fall after the final tradable opportunity of %s: %s. "
                "Their information cannot be acted on with this month's authorization, "
                "which does not roll over. Waiting through them may be the right call, "
                "but it means allowing this month's authorization to expire unused — "
                "state that explicitly in %r."
                % (
                    "%04d-%02d" % (now.year, now.month),
                    "; ".join(next_month_only),
                    AUTHORIZATION_EXPIRY_FIELD,
                )
            )
        )
    else:
        warnings.append(
            "waiting past the month's final tradable opportunity for: %s. The %s "
            "authorization expires unused; the acknowledgement was supplied and must "
            "say so plainly."
            % ("; ".join(next_month_only), "%04d-%02d" % (now.year, now.month))
        )


def _check_lessons_are_advisory(
    proposal: Proposal,
    raw_decision: Dict[str, Any],
    violations: List[Violation],
    checks: List[str],
) -> None:
    """A lesson informs a decision. It never authorises one.

    Historical lessons under ``research/lessons/`` are advisory, exactly like a
    watchlist entry (``DISCOVERY_POLICY.md`` §2) or a research note. Three
    things they may never do, and this rejects each:

    * **authorise** — approval is a human act, and a pattern from the past is
      not one (``CLAUDE.md`` §0a);
    * **satisfy a research requirement** — a lesson is a conclusion about past
      decisions, not evidence about this asset;
    * **relax a guardrail** — the validator has no override parameter, and a
      precedent is not one either.

    Learning from history is the point of the lessons layer. Using history as
    permission is the failure mode it must not enable.
    """
    checks.append("lessons_are_advisory_only")

    if isinstance(raw_decision, dict):
        for key in LESSON_MISUSE_FIELDS:
            if key in raw_decision:
                violations.append(
                    Violation(
                        "LESSON_MISUSED_AS_AUTHORIZATION",
                        "the decision payload carries %r. A historical lesson is "
                        "advisory: it cannot authorise a purchase, satisfy a "
                        "research requirement, or relax a guardrail. Cite it as "
                        "reasoning under 'thesis' instead." % key,
                    )
                )
                break

    # Overreaching language anywhere in the decision's own narrative.
    for name in ("thesis", "timing_reason", "why_not_wait", "margin_for_error",
                 "valuation_reasoning", "monthly_optionality_analysis"):
        matched = overreach_match(proposal.raw.get(name))
        if matched:
            violations.append(
                Violation(
                    "LESSON_MISUSED_AS_AUTHORIZATION",
                    "%r claims a lesson or precedent authorises, approves, waives "
                    "or overrides something (matched %r). Lessons are advisory "
                    "only; 'it worked before' is not a thesis." % (name, matched),
                )
            )
            break

    # A research component may not be satisfied by pointing at a lesson.
    checks.append("research_not_satisfied_by_a_lesson")
    block = _dict_block(proposal, "research_package")
    if block:
        for key, value in sorted(block.items()):
            if references_lesson(value):
                violations.append(
                    Violation(
                        "LESSON_CANNOT_SATISFY_RESEARCH",
                        "research_package.%s is answered by pointing at a lesson. "
                        "A lesson is a conclusion about past decisions, not "
                        "evidence about this asset — research the component."
                        % key,
                    )
                )
                break


def _check_new_position_hurdle(
    proposal: Proposal, violations: List[Violation], warnings: List[str], checks: List[str]
) -> None:
    """A new ticker is not inherently preferable to more of a good existing one."""
    checks.append("new_position_hurdle")
    block = _dict_block(proposal, "new_position_justification")
    if block is None:
        violations.append(
            Violation(
                "MISSING_NEW_POSITION_JUSTIFICATION",
                "opening a NEW_POSITION requires a 'new_position_justification' object "
                "comparing it directly against adding the same capital to the strongest "
                "existing holdings",
            )
        )
        return

    missing = _missing_keys(block, REQUIRED_NEW_POSITION_KEYS)
    if missing:
        violations.append(
            Violation(
                "MISSING_NEW_POSITION_JUSTIFICATION",
                "new_position_justification is missing non-empty %s" % ", ".join(missing),
            )
        )

    alternative = block.get("best_existing_alternative")
    symbol = None
    if isinstance(alternative, dict):
        symbol = _text(alternative.get("symbol"))
    elif isinstance(alternative, str):
        symbol = _text(alternative)
    if not symbol:
        violations.append(
            Violation(
                "MISSING_NEW_POSITION_JUSTIFICATION",
                "new_position_justification.best_existing_alternative must name the "
                "specific existing holding this new position was compared against",
            )
        )
    elif proposal.ticker and symbol.upper() == proposal.ticker:
        violations.append(
            Violation(
                "MISSING_NEW_POSITION_JUSTIFICATION",
                "best_existing_alternative names the proposed asset itself (%s); it must "
                "name a DIFFERENT existing holding" % symbol,
            )
        )

    # Filling an empty sector is a research signal, not a reason to buy.
    gap_text = " ".join(
        str(block.get(key, ""))
        for key in ("diversification_benefit", "diversification_is_economic_not_cosmetic")
    ).lower()
    if gap_text and re.search(r"fills?\s+(an?\s+)?(empty|missing|0%|zero)\s+", gap_text):
        warnings.append(
            "the diversification argument leans on filling an empty sector or theme. "
            "Missing exposure is a research signal, not a reason to buy; expected "
            "long-term risk-adjusted return remains the objective."
        )


def _check_research_completeness(
    proposal: Proposal, violations: List[Violation], warnings: List[str], checks: List[str]
) -> None:
    """No final BUY on a new position without the minimum research package."""
    checks.append("research_completeness_gate")
    spec = RESEARCH_COMPONENTS_FOR_ASSET_TYPE.get(proposal.asset_type or "")
    if spec is None:
        return  # asset-type validity is handled elsewhere
    components, mandatory = spec

    block = _dict_block(proposal, "research_package")
    if block is None:
        violations.append(
            Violation(
                "RESEARCH_INCOMPLETE_FOR_NEW_POSITION",
                "opening a NEW_POSITION requires a 'research_package' covering %s. "
                "Classify the candidate RESEARCH_INCOMPLETE instead of buying it."
                % ", ".join(components),
            )
        )
        return

    absent: List[str] = []
    declared_unavailable: List[str] = []
    for key in components:
        value = block.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            absent.append(key)
        elif isinstance(value, str) and value.strip().upper().startswith(NOT_AVAILABLE_PREFIX):
            declared_unavailable.append(key)

    if absent:
        violations.append(
            Violation(
                "RESEARCH_INCOMPLETE_FOR_NEW_POSITION",
                "research_package omits %s entirely. Every component must either carry "
                "findings or say '%s: <reason>'. Silence is not the same as unavailable."
                % (", ".join(absent), NOT_AVAILABLE_PREFIX),
            )
        )

    blocked = [k for k in declared_unavailable if k in mandatory]
    if blocked:
        violations.append(
            Violation(
                "RESEARCH_INCOMPLETE_FOR_NEW_POSITION",
                "research_package declares %s unavailable, and those components cannot be "
                "waived for a new position. Classify RESEARCH_INCOMPLETE rather than "
                "manufacturing confidence from incomplete research." % ", ".join(blocked)
            )
        )

    optional_gaps = [k for k in declared_unavailable if k not in mandatory]
    if optional_gaps:
        warnings.append(
            "research_package could not obtain %s; the thesis must not lean on anything "
            "those components would have shown" % ", ".join(optional_gaps)
        )

    if proposal.is_crypto and declared_unavailable:
        _check_research_gap_is_a_real_gap(
            block, declared_unavailable, violations, checks
        )


# Robinhood is authoritative for what this account owns, what it paid, which
# pairs it may trade, and what it has ordered. It is a broker: it publishes no
# issuance schedule, no active-address count, no protocol roadmap and no
# regulatory analysis, and it never claimed to.
#
# So "the connected tools do not expose it" describes the tool surface, not the
# world. A crypto network's supply schedule, usage, development activity,
# security model and regulatory position are published openly by the protocol
# itself, read directly off the chain, and tracked by reputable providers.
# Waiving a component for that reason turns a research task into a permanent
# excuse, and every crypto candidate becomes RESEARCH_INCOMPLETE forever for a
# reason that has nothing to do with the asset.
#
# A genuine gap still exists and is still waivable -- read-only web research
# unavailable in a given run, a source that could not be reached, a figure no
# reputable provider publishes. State *that* obstacle. These patterns match
# only the broker-scope excuse, so an honest account of a real obstacle passes.
_ROBINHOOD_SCOPE_EXCUSE_RES = [
    re.compile(pattern, re.I) for pattern in ROBINHOOD_SCOPE_EXCUSE_PATTERNS
]


def _scope_excuse(text: Any) -> Optional[str]:
    """The broker-scope excuse pattern this text matches, if any."""
    blob = _text(text)
    if not blob:
        return None
    for pattern in _ROBINHOOD_SCOPE_EXCUSE_RES:
        if pattern.search(blob):
            return pattern.pattern
    return None


def _check_research_gap_is_a_real_gap(
    block: Dict[str, Any],
    declared_unavailable: List[str],
    violations: List[Violation],
    checks: List[str],
) -> None:
    """A waived crypto component must name a real obstacle, not the tool list."""
    checks.append("crypto_research_gaps_are_real_gaps")
    for key in declared_unavailable:
        matched = _scope_excuse(block.get(key))
        if matched:
            violations.append(
                Violation(
                    "RESEARCH_GAP_NOT_JUSTIFIED",
                    "research_package.%s is waived because Robinhood or the connected "
                    "tool set does not expose it (matched %r). That is not a reason: "
                    "Robinhood is authoritative for holdings, cost basis, pair "
                    "eligibility, tradability and orders, and for nothing else. "
                    "Network usage, issuance and supply, protocol development, "
                    "ecosystem, security and decentralization, regulatory "
                    "developments, institutional adoption and competing networks are "
                    "published by primary and official sources and must be researched "
                    "with read-only external sources. Waive it only for an obstacle "
                    "that actually exists, and say which one."
                    % (key, matched),
                )
            )


def _check_primary_sources(
    proposal: Proposal, violations: List[Violation], warnings: List[str], checks: List[str]
) -> None:
    """Analyst opinion may inform a thesis; it may never substitute for the record.

    What counts as "the record" differs by asset class, and pretending otherwise
    is a category error in both directions. A company files with the SEC and
    publishes financial statements. A cryptoasset does neither: its record is
    the protocol's own specification, the chain itself, the reference
    implementation, the governance process, the regulator's own publications,
    and reputable data providers. Requiring an SEC filing from a blockchain
    would make every crypto candidate unbuyable for a reason that says nothing
    about the asset -- so crypto is held to an equally strict standard drawn
    from sources that actually exist for it.
    """
    checks.append("primary_sources_present")
    evidence = proposal.raw.get("evidence")
    if not isinstance(evidence, (list, tuple)):
        return  # presence is enforced by the narrative check

    if proposal.is_crypto:
        accepted_tools = frozenset()
        accepted_types = CRYPTO_PRIMARY_SOURCE_TYPES
        minimum = MIN_PRIMARY_SOURCES_NEW_CRYPTO
        soft_types = {"ANALYST_OPINION", "SECONDARY_REPORTING", "SOCIAL_SENTIMENT"}
        soft_tools = {"get_equity_news"}
        description = "source_type in %s" % sorted(accepted_types)
    else:
        accepted_tools = PRIMARY_SOURCE_TOOLS
        accepted_types = PRIMARY_SOURCE_TYPES
        minimum = MIN_PRIMARY_SOURCES_NEW_EQUITY
        soft_types = {"ANALYST_OPINION"}
        soft_tools = {"get_equity_news"}
        description = "%s, or source_type in %s" % (
            ", ".join(sorted(accepted_tools)),
            sorted(accepted_types),
        )

    primary = 0
    soft = 0
    for item in evidence:
        if isinstance(item, dict):
            tool = (_text(item.get("tool")) or "").lower()
            source_type = (_text(item.get("source_type")) or "").upper()
            if tool in accepted_tools or source_type in accepted_types:
                primary += 1
            if source_type in soft_types or tool in soft_tools:
                soft += 1

    if primary < minimum:
        violations.append(
            Violation(
                "INSUFFICIENT_PRIMARY_SOURCES",
                "a new %s position requires at least %d evidence entries from primary "
                "or official sources (%s); found %d. %s"
                % (
                    "crypto" if proposal.is_crypto else "equity/ETF",
                    minimum,
                    description,
                    primary,
                    "Read-only external research is the way to obtain these for a "
                    "cryptoasset; Robinhood does not publish them and is not expected "
                    "to."
                    if proposal.is_crypto
                    else "Analyst opinion never counts.",
                ),
            )
        )
    if soft and soft >= primary:
        warnings.append(
            "evidence contains %d analyst/secondary entries against %d primary-source "
            "entries; opinion and reporting must not carry the thesis" % (soft, primary)
        )


def _check_portfolio_sprawl(
    proposal: Proposal, violations: List[Violation], checks: List[str]
) -> None:
    """Another ticker is not automatically progress."""
    checks.append("portfolio_sprawl_assessed")
    block = _dict_block(proposal, "portfolio_sprawl_assessment")
    if block is None:
        violations.append(
            Violation(
                "MISSING_SPRAWL_ASSESSMENT",
                "every BUY must carry a 'portfolio_sprawl_assessment': position count, "
                "smallest positions, the economic significance of this addition, overlap, "
                "and whether concentrating into an existing holding would be better",
            )
        )
        return
    missing = _missing_keys(block, REQUIRED_SPRAWL_KEYS)
    if missing:
        violations.append(
            Violation(
                "MISSING_SPRAWL_ASSESSMENT",
                "portfolio_sprawl_assessment is missing non-empty %s" % ", ".join(missing),
            )
        )


def _check_theme_precision(
    proposal: Proposal, violations: List[Violation], checks: List[str]
) -> None:
    """A position addresses a SUBTHEME. It does not fill a whole broad theme."""
    checks.append("theme_precision")
    block = _dict_block(proposal, "theme")
    if block is None:
        violations.append(
            Violation(
                "MISSING_REQUIRED_FIELD",
                "every BUY must carry a 'theme' object with %s" % ", ".join(REQUIRED_THEME_KEYS),
            )
        )
        return

    missing = _missing_keys(block, REQUIRED_THEME_KEYS)
    if missing:
        violations.append(
            Violation(
                "MISSING_REQUIRED_FIELD",
                "theme is missing non-empty %s" % ", ".join(missing),
            )
        )

    subtheme = (_text(block.get("subtheme")) or "").lower()
    primary = (_text(block.get("primary")) or "").lower()
    if subtheme and subtheme not in THEME_TAXONOMY:
        violations.append(
            Violation(
                "INVALID_THEME",
                "subtheme %r is not in the taxonomy. Use a specific subtheme, e.g. "
                "'robotic_assisted_surgery' rather than 'robotics'." % subtheme,
            )
        )
        return
    if subtheme and primary:
        expected = THEME_TAXONOMY[subtheme]
        if primary != expected:
            violations.append(
                Violation(
                    "THEME_MISMATCH",
                    "subtheme %r rolls up to broad theme %r, but %r was declared. "
                    "Robotic-assisted surgery is healthcare, not industrial robotics."
                    % (subtheme, expected, primary),
                )
            )
    if primary and primary in BROAD_THEMES and subtheme:
        siblings = [t for t in subthemes_for(primary) if t != subtheme]
        if siblings and not _nonempty_narrative(block.get("scope_caveat")):
            violations.append(
                Violation(
                    "MISSING_REQUIRED_FIELD",
                    "theme.scope_caveat must state what this position does NOT cover; "
                    "%r leaves %s unaddressed" % (subtheme, ", ".join(siblings[:4])),
                )
            )


def _check_wait_basis(
    proposal: Proposal, violations: List[Violation], checks: List[str]
) -> None:
    """A WAIT must name the KIND of reason it rests on."""
    checks.append("wait_basis_declared")
    raw_basis = proposal.raw.get("wait_basis")
    if isinstance(raw_basis, str):
        bases = [raw_basis]
    elif isinstance(raw_basis, (list, tuple)):
        bases = [b for b in raw_basis if isinstance(b, str)]
    else:
        bases = []

    if not bases:
        violations.append(
            Violation(
                "MISSING_REQUIRED_FIELD",
                "a WAIT must declare 'wait_basis' (one or more of %s). Waiting for "
                "information is only one valid basis; valuation and risk/reward stand "
                "on their own." % list(VALID_WAIT_BASES),
            )
        )
        return

    unknown = [b for b in bases if b.strip().upper() not in VALID_WAIT_BASES]
    if unknown:
        violations.append(
            Violation(
                "INVALID_WAIT_BASIS",
                "wait_basis values %s are not in %s" % (unknown, list(VALID_WAIT_BASES)),
            )
        )


def _check_reported_budget(
    proposal: Proposal,
    state: BudgetState,
    violations: List[Violation],
    warnings: List[str],
) -> None:
    """The model must not misreport the budget math in its own decision object."""
    before_raw = proposal.raw.get("monthly_budget_before_usd")
    after_raw = proposal.raw.get("monthly_budget_after_usd")

    if before_raw is None:
        warnings.append("monthly_budget_before_usd was not reported")
    else:
        try:
            before = parse_money(before_raw, "monthly_budget_before_usd")
        except MoneyError as exc:
            violations.append(Violation("MALFORMED_NUMBER", str(exc)))
            return
        if before != state.remaining_usd:
            violations.append(
                Violation(
                    "BUDGET_MISREPORTED",
                    "reported monthly_budget_before_usd %s does not match the actual "
                    "remaining authorization %s"
                    % (money_str(before), money_str(state.remaining_usd)),
                )
            )

    if after_raw is None:
        warnings.append("monthly_budget_after_usd was not reported")
        return
    try:
        after = parse_money(after_raw, "monthly_budget_after_usd")
    except MoneyError as exc:
        violations.append(Violation("MALFORMED_NUMBER", str(exc)))
        return
    expected = state.remaining_usd - proposal.proposed_amount_usd
    if after != expected:
        violations.append(
            Violation(
                "BUDGET_MISREPORTED",
                "reported monthly_budget_after_usd %s does not match %s - %s = %s"
                % (
                    money_str(after),
                    money_str(state.remaining_usd),
                    money_str(proposal.proposed_amount_usd),
                    money_str(expected),
                ),
            )
        )
    if after < ZERO:
        violations.append(
            Violation("EXCEEDS_REMAINING_BUDGET", "remaining authorization would go negative")
        )


def _bucket_value(raw: Dict[str, Any], name: str, aliases: Tuple[str, ...]) -> Any:
    """A WAIT candidate bucket, accepting its pre-Stage-7 alias."""
    for key in (name,) + tuple(aliases):
        value = raw.get(key)
        if value is not None:
            return value
    return None


def _check_wait_capital_use_comparison(
    proposal: Proposal, violations: List[Violation], checks: List[str]
) -> None:
    """A WAIT must show the five-way competition it actually ran.

    Equities, ETFs and direct crypto compete for one authorization, so a WAIT
    has to name the strongest candidate for adding to an existing equity, for a
    new equity/ETF, for adding to an existing crypto position, and for a new
    crypto position. Preserving the budget is the fifth use and is the decision
    itself.

    A bucket that genuinely does not apply must say ``NOT_APPLICABLE: <reason>``.
    Silence is not the same as inapplicable — the same rule the research
    package already uses for unobtainable components.
    """
    checks.append("wait_compares_all_five_capital_uses")
    for name, aliases in WAIT_CANDIDATE_BUCKETS:
        value = _bucket_value(proposal.raw, name, aliases)

        if value is None or not _nonempty_narrative(value):
            violations.append(
                Violation(
                    "MISSING_CAPITAL_USE_COMPARISON",
                    "a WAIT decision must include a non-empty %r. The $25 is one "
                    "authorization shared by equities, ETFs and crypto, so WAIT must "
                    "name the strongest candidate in every competing bucket and say "
                    "why it fell short. If the bucket genuinely does not apply, say "
                    "%r with a reason." % (name, NOT_APPLICABLE_PREFIX + ": ..."),
                )
            )
            continue

        text = _text(value)
        if text is not None and text.upper().startswith(NOT_APPLICABLE_PREFIX):
            reason = text[len(NOT_APPLICABLE_PREFIX):].lstrip(": ").strip()
            if len(reason) < 10:
                violations.append(
                    Violation(
                        "MISSING_CAPITAL_USE_COMPARISON",
                        "%r is marked %s but gives no reason. State why this use of "
                        "capital does not apply this month."
                        % (name, NOT_APPLICABLE_PREFIX),
                    )
                )
            continue

        if isinstance(value, dict) and not _nonempty_narrative(value.get("why_not")):
            violations.append(
                Violation(
                    "MISSING_CAPITAL_USE_COMPARISON",
                    "%r names a candidate but does not say why it fell short; every "
                    "bucket needs a 'why_not'." % name,
                )
            )


def _check_crypto_bucket_research_excuses(
    proposal: Proposal, violations: List[Violation], checks: List[str]
) -> None:
    """A crypto bucket may not be dismissed as unresearchable because of the broker.

    ``RESEARCH_INCOMPLETE`` is an honest and often correct classification. What
    it must not become is a standing excuse: Robinhood was never going to
    publish a network's issuance schedule or a protocol's development activity,
    so "the connected tools do not expose it" would disqualify every crypto
    candidate forever, in every month, on grounds that describe the tool surface
    rather than the asset.

    This fires only where a bucket both claims the research is missing *and*
    blames the broker for it. A bucket that names a real obstacle -- no
    read-only web research available in this run, a source that could not be
    reached, a figure nobody reputable publishes -- passes unchanged.
    """
    checks.append("crypto_buckets_not_dismissed_on_broker_scope")
    for name in CRYPTO_WAIT_BUCKETS:
        value = _bucket_value(proposal.raw, name, ("best_crypto_candidate",))
        if value is None:
            continue
        if isinstance(value, dict):
            blob = " ".join(str(v) for v in value.values() if isinstance(v, str))
        else:
            blob = _text(value) or ""
        if not blob.strip():
            continue

        claims_gap = (
            "RESEARCH_INCOMPLETE" in blob.upper()
            or blob.upper().startswith(NOT_APPLICABLE_PREFIX)
        )
        if not claims_gap:
            continue
        matched = _scope_excuse(blob)
        if matched:
            violations.append(
                Violation(
                    "RESEARCH_GAP_NOT_JUSTIFIED",
                    "%s is written off as unresearchable because Robinhood or the "
                    "connected tool set does not expose the inputs (matched %r). "
                    "Robinhood is authoritative for holdings, cost basis, pair "
                    "eligibility, tradability and orders -- not for network usage, "
                    "issuance, protocol development, ecosystem, security, regulatory "
                    "developments, institutional adoption or competing networks. Those "
                    "come from primary and official external sources, read-only, and "
                    "must be researched rather than waived. If a real obstacle blocked "
                    "the research, name that obstacle instead." % (name, matched),
                )
            )


def _check_wait_rules(
    proposal: Proposal,
    state: BudgetState,
    violations: List[Violation],
    warnings: List[str],
    checks: List[str],
) -> None:
    checks.append("wait_proposes_no_purchase")
    if proposal.proposed_amount_usd != ZERO:
        violations.append(
            Violation(
                "WAIT_WITH_AMOUNT",
                "a WAIT decision must not propose a dollar amount (got %s)"
                % money_str(proposal.proposed_amount_usd),
            )
        )

    _check_wait_basis(proposal, violations, checks)

    checks.append("required_wait_fields_present")
    for name in REQUIRED_WAIT_NARRATIVE:
        if not _nonempty_narrative(proposal.raw.get(name)):
            violations.append(
                Violation(
                    "MISSING_REQUIRED_FIELD",
                    "a WAIT decision must include a non-empty %r" % name,
                )
            )

    _check_wait_capital_use_comparison(proposal, violations, checks)
    _check_crypto_bucket_research_excuses(proposal, violations, checks)

    checks.append("wait_reports_correct_budget")
    remaining_raw = proposal.raw.get(
        "monthly_budget_remaining_usd", proposal.raw.get("monthly_budget_before_usd")
    )
    if remaining_raw is None:
        violations.append(
            Violation(
                "MISSING_REQUIRED_FIELD",
                "a WAIT decision must report monthly_budget_remaining_usd",
            )
        )
    else:
        try:
            remaining = parse_money(remaining_raw, "monthly_budget_remaining_usd")
        except MoneyError as exc:
            violations.append(Violation("MALFORMED_NUMBER", str(exc)))
        else:
            if remaining != state.remaining_usd:
                violations.append(
                    Violation(
                        "BUDGET_MISREPORTED",
                        "reported remaining authorization %s does not match the actual %s"
                        % (money_str(remaining), money_str(state.remaining_usd)),
                    )
                )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def validate(
    raw_decision: Dict[str, Any],
    config: Config,
    state: BudgetState,
    crypto_universe: Optional[CryptoUniverse] = None,
    now: Optional[datetime] = None,
) -> ValidationResult:
    """Validate a proposed decision against every hard rule.

    ``crypto_universe`` is loaded from disk on demand when a crypto proposal
    needs it. ``now`` drives the mid-month optionality gate. Pass both
    explicitly in tests.

    Returns a :class:`ValidationResult`. ``executable`` is always False.
    """
    now = now or utc_now()
    violations: List[Violation] = []
    warnings: List[str] = []
    checks: List[str] = ["proposal_wellformed"]

    proposal, parse_violations = normalize(raw_decision)
    violations.extend(parse_violations)

    # --- layering: arming execution is not an investment question ---------
    #
    # This function judges ONE thing: is this a sound investment decision under
    # the policy? It used to also emit LIVE_TRADING_NOT_PERMITTED whenever
    # config.live_trading was true, on the theory that the repository was
    # dry-run only. That made the documented live path impossible, because the
    # policy fingerprint forces arm-then-approve while approve_decision.py
    # re-validates through here — so arming was guaranteed to invalidate the
    # very decision the operator was about to approve.
    #
    # The switches are still enforced, in the layer that owns them:
    # src.execution.execution_gate_blockers() refuses execution unless
    # execution_mode is APPROVAL_REQUIRED, agent_enabled is true AND
    # live_trading is true, and src.execution.preflight() calls it on every
    # path toward a submission ticket. Nothing here can authorize anything:
    # ``executable`` below is unconditionally False.
    #
    # An armed repository is worth saying out loud, so it warns. A warning does
    # not invalidate a decision that is otherwise sound.
    checks.append("execution_arming_is_not_an_investment_concern")
    if config.live_trading or config.agent_enabled or config.execution_mode != "DRY_RUN":
        warnings.append(
            "execution is armed (execution_mode=%s, agent_enabled=%r, live_trading=%r in "
            "%s). That has no bearing on whether this decision is a sound investment, "
            "which is all this validator judges. Execution readiness is decided by "
            "src.execution.preflight(), and broker handoff additionally requires a human "
            "approval and a fresh single-use submission ticket."
            % (config.execution_mode, config.agent_enabled, config.live_trading,
               config.path)
        )

    # --- a decision can never approve itself -----------------------------
    checks.append("no_self_approval_fields")
    if isinstance(raw_decision, dict):
        for key in SELF_APPROVAL_FIELDS:
            if key in raw_decision:
                violations.append(
                    Violation(
                        "SELF_APPROVAL_ATTEMPTED",
                        "the decision payload carries %r. Model reasoning is never "
                        "approval: approvals live only in state/approvals.json and are "
                        "created only by scripts/approve_decision.py." % key,
                    )
                )
                break

    # --- decision id -----------------------------------------------------
    checks.append("decision_id_present")
    if not proposal.decision_id:
        violations.append(
            Violation("MISSING_DECISION_ID", "every decision must carry a non-empty decision_id")
        )
    else:
        checks.append("decision_id_not_replayed")
        if state.has_acted(proposal.decision_id):
            violations.append(
                Violation(
                    "DUPLICATE_DECISION_ID",
                    "decision_id %r has already been acted upon; a decision may never be "
                    "executed twice, in any asset class" % proposal.decision_id,
                )
            )

    # --- decision type ---------------------------------------------------
    checks.append("decision_is_buy_or_wait")
    if proposal.decision not in VALID_DECISIONS:
        violations.append(
            Violation(
                "INVALID_DECISION",
                "decision must be exactly %s or %s, got %r"
                % (DECISION_BUY, DECISION_WAIT, proposal.decision or None),
            )
        )

    # --- confidence ------------------------------------------------------
    checks.append("confidence_stated")
    if proposal.confidence is None:
        warnings.append("no confidence level stated")
    elif proposal.confidence not in CONFIDENCE_LEVELS:
        violations.append(
            Violation(
                "INVALID_CONFIDENCE",
                "confidence must be one of %s, got %r" % (list(CONFIDENCE_LEVELS), proposal.confidence),
            )
        )

    # --- prohibitions apply to every decision ----------------------------
    _check_prohibited_classes(proposal, config, violations, checks)

    # --- historical lessons are advisory, in every decision --------------
    _check_lessons_are_advisory(proposal, raw_decision, violations, checks)

    # --- the calendar boundary applies to every decision -----------------
    # A BUY may cite an event as a reason not to wait, and a WAIT may cite one
    # as a reason to wait. Either way, an event past the month's last tradable
    # moment is next month's information and must not be presented otherwise.
    _check_event_actionability(proposal, now, violations, warnings, checks)

    # --- type-specific ---------------------------------------------------
    if proposal.decision == DECISION_BUY:
        _check_buy_common(proposal, config, state, now, violations, warnings, checks)

        if proposal.is_crypto:
            universe_error = None
            if crypto_universe is None:
                try:
                    crypto_universe = load_crypto_universe()
                except CryptoUniverseError as exc:
                    universe_error = str(exc)
            _check_crypto_rules(
                proposal, config, crypto_universe, universe_error, violations, warnings, checks
            )
        elif proposal.asset_type in (ASSET_US_COMMON_STOCK, ASSET_US_ETF) or proposal.asset_class in (
            ASSET_CLASS_EQUITY,
            ASSET_CLASS_ETF,
        ):
            _check_equity_rules(proposal, violations, warnings, checks)
        else:
            # Neither a recognized security nor crypto: the asset-type check
            # above has already rejected it; still require a ticker.
            checks.append("ticker_present")
            if not proposal.ticker:
                violations.append(Violation("MISSING_TICKER", "a BUY decision must name an asset"))

    elif proposal.decision == DECISION_WAIT:
        _check_wait_rules(proposal, state, violations, warnings, checks)

    # De-duplicate violations while preserving order.
    seen = set()
    unique: List[Violation] = []
    for violation in violations:
        key = (violation.code, violation.message)
        if key not in seen:
            seen.add(key)
            unique.append(violation)

    return ValidationResult(
        valid=not unique,
        executable=False,  # never, under any circumstances
        violations=unique,
        warnings=warnings,
        checks_run=checks,
        execution_status="DRY_RUN_NOT_EXECUTED",
    )


def assert_execution_allowed(config: Config) -> None:
    """The gate a future live version would call before submitting an order.

    This always raises. There is intentionally no broker client in this
    repository for it to guard.
    """
    raise LiveTradingDisabled(
        "This version is dry-run only: no order submission path exists. "
        "live_trading=%r in %s, and enabling it is not sufficient — a live "
        "execution module would have to be written and reviewed first."
        % (config.live_trading, config.path)
    )
