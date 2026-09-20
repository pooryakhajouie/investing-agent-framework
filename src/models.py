"""Data models and money handling.

Money is handled with :class:`decimal.Decimal` throughout. Binary floating point
is never used for a budget comparison. Floats supplied by callers are accepted
only via their shortest round-trip repr, and are flagged as a warning.

Version 2 adds a third asset class — direct Robinhood-supported cryptocurrency —
alongside U.S. common stocks and non-leveraged U.S.-listed ETFs. Crypto is not
permitted by removing a guardrail; it is permitted by an explicit, positive
validation path (see :mod:`src.guardrails`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


class MoneyError(ValueError):
    """Raised when a value cannot be interpreted as an exact USD amount."""


def parse_money(value: Any, field_name: str = "amount") -> Decimal:
    """Parse ``value`` into an exact :class:`Decimal` USD amount.

    Accepts Decimal, int, and str (``"$1,234.56"`` is tolerated). Floats are
    converted through ``repr`` so ``0.1`` becomes ``Decimal("0.1")`` rather than
    the binary-expansion noise ``Decimal(0.1)`` would produce; callers should
    still prefer strings.
    """
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool):
        raise MoneyError("%s: booleans are not valid money values" % field_name)
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, float):
        parsed = Decimal(repr(value))
    elif isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "").replace("_", "")
        if not cleaned:
            raise MoneyError("%s: empty string is not a valid money value" % field_name)
        try:
            parsed = Decimal(cleaned)
        except InvalidOperation:
            raise MoneyError("%s: %r is not a valid money value" % (field_name, value))
    else:
        raise MoneyError(
            "%s: unsupported type %s for a money value" % (field_name, type(value).__name__)
        )

    if not parsed.is_finite():
        raise MoneyError("%s: %r is not a finite money value" % (field_name, value))
    return parsed


def is_whole_cents(amount: Decimal) -> bool:
    """True when ``amount`` has no fraction smaller than one cent."""
    scaled = amount * 100
    return scaled == scaled.to_integral_value()


def money_str(amount: Decimal) -> str:
    """Render an amount as a plain two-decimal string, e.g. ``"25.00"``."""
    return str(amount.quantize(CENTS, rounding=ROUND_HALF_UP))


def usd(amount: Decimal) -> str:
    """Render an amount for humans, e.g. ``"$25.00"``."""
    return "$" + money_str(amount)


# --------------------------------------------------------------------------
# Decision vocabulary
# --------------------------------------------------------------------------

DECISION_BUY = "BUY"
DECISION_WAIT = "WAIT"
VALID_DECISIONS = (DECISION_BUY, DECISION_WAIT)

CONFIDENCE_LEVELS = ("LOW", "MEDIUM", "HIGH")

# --- asset classes (the coarse bucket the $25 competes across) ------------
ASSET_CLASS_EQUITY = "EQUITY"
ASSET_CLASS_ETF = "ETF"
ASSET_CLASS_CRYPTO = "CRYPTO"
VALID_ASSET_CLASSES = (ASSET_CLASS_EQUITY, ASSET_CLASS_ETF, ASSET_CLASS_CRYPTO)

# --- asset types (the specific instrument kind) ---------------------------
ASSET_US_COMMON_STOCK = "us_common_stock"
ASSET_US_ETF = "us_etf"
ASSET_CRYPTO = "crypto"
ALLOWED_ASSET_TYPES = (ASSET_US_COMMON_STOCK, ASSET_US_ETF, ASSET_CRYPTO)

# An asset_class must agree with its asset_type.
ASSET_CLASS_FOR_TYPE = {
    ASSET_US_COMMON_STOCK: ASSET_CLASS_EQUITY,
    ASSET_US_ETF: ASSET_CLASS_ETF,
    ASSET_CRYPTO: ASSET_CLASS_CRYPTO,
}

# --- position type --------------------------------------------------------
POSITION_EXISTING = "EXISTING_POSITION"
POSITION_NEW = "NEW_POSITION"
VALID_POSITION_TYPES = (POSITION_EXISTING, POSITION_NEW)

# --- cost-basis direction -------------------------------------------------
AVG_RAISES = "RAISES"
AVG_LOWERS = "LOWERS"
AVG_NEUTRAL = "NEUTRAL"
VALID_AVG_DIRECTIONS = (AVG_RAISES, AVG_LOWERS, AVG_NEUTRAL)

# --- candidate classifications (reasoning labels, never trade signals) ----
CLASSIFICATIONS_GENERAL = (
    "HIGH_CONVICTION_CANDIDATE",
    "PROMISING",
    "WATCH",
    "ATTRACTIVE_ON_PULLBACK",
    "TOO_EXPENSIVE",
    "TOO_EXTENDED",
    "THESIS_UNCLEAR",
    "FUNDAMENTALS_WEAK",
    "SPECULATIVE",
    "REJECTED",
    "RESEARCH_INCOMPLETE",
)
CLASSIFICATIONS_EXISTING = (
    "ADD_CANDIDATE",
    "HOLD_NO_ADD",
    "THESIS_WEAKENING",
    "OVERCONCENTRATED",
)
CLASSIFICATIONS_MOMENTUM = (
    "EARLY_STRUCTURAL_OPPORTUNITY",
    "FUNDAMENTALLY_SUPPORTED_MOMENTUM",
    "SPECULATIVE_HYPE",
)
VALID_CLASSIFICATIONS = (
    CLASSIFICATIONS_GENERAL + CLASSIFICATIONS_EXISTING + CLASSIFICATIONS_MOMENTUM
)

# Only these classifications may accompany an actual BUY. A candidate the model
# itself labelled TOO_EXPENSIVE -- or RESEARCH_INCOMPLETE -- may not
# simultaneously be the thing it buys.
BUYABLE_CLASSIFICATIONS = (
    "HIGH_CONVICTION_CANDIDATE",
    "PROMISING",
    "ADD_CANDIDATE",
    "EARLY_STRUCTURAL_OPPORTUNITY",
    "FUNDAMENTALLY_SUPPORTED_MOMENTUM",
)

# --- scorecard dimensions (see DISCOVERY_POLICY.md) ----------------------
SCORECARD_DIMENSIONS = (
    "long_term_opportunity",
    "business_or_network_quality",
    "fundamental_strength",
    "growth_runway",
    "competitive_advantage",
    "valuation",
    "catalyst_quality",
    "portfolio_fit",
    "downside_risk",
    "uncertainty",
    "entry_attractiveness",
)

# --- candidate provenance -------------------------------------------------
PROVENANCE_PREFIXES = (
    "CURRENT_HOLDING",
    "MY_WATCHLIST",
    "MY_SCAN",
    "THEME_DISCOVERY",
    "SUPPLY_CHAIN_DISCOVERY",
    "ROBINHOOD_POPULAR",
    "OPEN_DISCOVERY",
)

# --------------------------------------------------------------------------
# Prohibitions
# --------------------------------------------------------------------------

# Asset types that remain explicitly forbidden, mapped to their violation code.
# NOTE: "crypto" is deliberately NOT here any more — it has its own positive
# validation path in guardrails._check_crypto_rules.
FORBIDDEN_ASSET_TYPES = {
    "option": "OPTIONS_FORBIDDEN",
    "options": "OPTIONS_FORBIDDEN",
    "option_contract": "OPTIONS_FORBIDDEN",
    "future": "FUTURES_FORBIDDEN",
    "futures": "FUTURES_FORBIDDEN",
    "forex": "FUTURES_FORBIDDEN",
    "event_contract": "FUTURES_FORBIDDEN",
    "prediction_market": "FUTURES_FORBIDDEN",
    "leveraged_etf": "LEVERAGED_PRODUCT_FORBIDDEN",
    "inverse_etf": "INVERSE_PRODUCT_FORBIDDEN",
    "leveraged_crypto": "LEVERAGED_PRODUCT_FORBIDDEN",
    "crypto_futures": "FUTURES_FORBIDDEN",
    "otc": "OTC_FORBIDDEN",
    "otc_stock": "OTC_FORBIDDEN",
    "pink_sheet": "OTC_FORBIDDEN",
}

# Order sides that are not a plain long purchase.
FORBIDDEN_SIDES = {
    "sell": "SELL_FORBIDDEN",
    "sell_to_open": "SELL_FORBIDDEN",
    "sell_to_close": "SELL_FORBIDDEN",
    "sell_short": "SHORT_FORBIDDEN",
    "short": "SHORT_FORBIDDEN",
    "short_sell": "SHORT_FORBIDDEN",
    "buy_to_cover": "SHORT_FORBIDDEN",
}

# Top-level actions that are not an investment decision at all.
FORBIDDEN_ACTIONS = {
    "sell": "SELL_FORBIDDEN",
    "transfer": "TRANSFER_FORBIDDEN",
    "withdraw": "TRANSFER_FORBIDDEN",
    "withdrawal": "TRANSFER_FORBIDDEN",
    "deposit": "TRANSFER_FORBIDDEN",
    "ach_transfer": "TRANSFER_FORBIDDEN",
    "settings_change": "SETTINGS_CHANGE_FORBIDDEN",
    "account_upgrade": "SETTINGS_CHANGE_FORBIDDEN",
}

MAJOR_US_EXCHANGES = {
    "NYSE",
    "NASDAQ",
    "NYSEARCA",
    "ARCA",
    "AMEX",
    "NYSEAMERICAN",
    "BATS",
    "CBOE",
    "CBOEBZX",
    "BZX",
    "IEX",
}

TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
CRYPTO_PAIR_RE = re.compile(r"^[A-Z0-9]{2,10}-USD$")


# --------------------------------------------------------------------------
# Proposal
# --------------------------------------------------------------------------


@dataclass
class Proposal:
    """A normalized decision produced by the model, ready for validation.

    Only fields the *validator* needs live here as typed attributes. Narrative
    fields (thesis, risks, evidence, ...) are carried through in ``raw`` and are
    checked for presence and internal consistency, not parsed.
    """

    decision_id: str = ""
    decision: str = ""
    action: str = ""
    asset_class: Optional[str] = None
    position_type: Optional[str] = None
    ticker: Optional[str] = None
    security_name: Optional[str] = None
    asset_type: Optional[str] = None
    exchange: Optional[str] = None
    side: str = "buy"
    proposed_amount_usd: Decimal = ZERO
    current_price_usd: Optional[Decimal] = None
    market_cap_usd: Optional[Decimal] = None
    fractional_eligible: Optional[bool] = None
    investment_horizon_months: Optional[int] = None
    classification: Optional[str] = None
    uses_margin: bool = False
    uses_options: bool = False
    is_short: bool = False
    is_leveraged: Optional[bool] = None
    is_inverse: Optional[bool] = None
    is_otc: Optional[bool] = None
    is_transfer: bool = False
    confidence: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_crypto(self) -> bool:
        return (
            self.asset_class == ASSET_CLASS_CRYPTO
            or self.asset_type == ASSET_CRYPTO
        )


@dataclass
class Violation:
    """A hard rule breach. Any violation makes a proposal invalid."""

    code: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass
class ValidationResult:
    """Outcome of deterministic validation.

    ``valid`` means "this proposal does not break any hard rule".
    ``executable`` means "this proposal may be sent to a broker" — which in
    version 1/2 is always False, because live trading is disabled.
    """

    valid: bool
    executable: bool = False
    violations: List[Violation] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks_run: List[str] = field(default_factory=list)
    execution_status: str = "DRY_RUN_NOT_EXECUTED"

    @property
    def violation_codes(self) -> List[str]:
        return [v.code for v in self.violations]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "executable": self.executable,
            "violations": [v.to_dict() for v in self.violations],
            "warnings": list(self.warnings),
            "checks_run": list(self.checks_run),
            "execution_status": self.execution_status,
        }


# --------------------------------------------------------------------------
# Version 3 additions
# --------------------------------------------------------------------------

# Robinhood's floor for a dollar-based fractional equity order.
MIN_EQUITY_ORDER_USD = Decimal("1.00")

# A BUY that would consume more than this share of the month's authorization
# before mid-month must carry an explicit monthly-optionality justification.
OPTIONALITY_SPEND_THRESHOLD = Decimal("0.50")
MID_MONTH_DAY = 15


# --- research completeness ------------------------------------------------
# Every component must be present. A component that could not be obtained must
# say so explicitly, as a string beginning with NOT_AVAILABLE.
NOT_AVAILABLE_PREFIX = "NOT_AVAILABLE"

RESEARCH_COMPONENTS_EQUITY = (
    "current_quote_and_valuation",
    "recent_quarterly_results",
    "multi_quarter_trends",
    "balance_sheet",
    "cash_flow",
    "earnings_history",
    "material_news",
    "primary_filings",
    "competitive_position",
    "major_risks",
    "upcoming_events",
    "bull_case",
    "bear_case",
)

# Components that cannot be waived: without these there is no analysis at all.
RESEARCH_COMPONENTS_EQUITY_MANDATORY = (
    "current_quote_and_valuation",
    "recent_quarterly_results",
    "multi_quarter_trends",
    "balance_sheet",
    "major_risks",
    "bull_case",
    "bear_case",
)

RESEARCH_COMPONENTS_ETF = (
    "current_quote_and_valuation",
    "index_and_methodology",
    "expense_ratio",
    "holdings_concentration",
    "overlap_with_existing_holdings",
    "liquidity_and_spread",
    "fund_size_and_age",
    "major_risks",
    "bull_case",
    "bear_case",
)
RESEARCH_COMPONENTS_ETF_MANDATORY = (
    "current_quote_and_valuation",
    "index_and_methodology",
    "expense_ratio",
    "overlap_with_existing_holdings",
    "major_risks",
    "bull_case",
    "bear_case",
)

RESEARCH_COMPONENTS_CRYPTO = (
    "current_quote_and_market_cap",
    "supply_and_issuance",
    "network_usage_and_adoption",
    "ecosystem_and_development",
    "security_and_decentralization",
    "regulatory_risk",
    "competitive_networks",
    "long_term_price_and_drawdown_history",
    "major_risks",
    "bull_case",
    "bear_case",
)
RESEARCH_COMPONENTS_CRYPTO_MANDATORY = (
    "current_quote_and_market_cap",
    "supply_and_issuance",
    "network_usage_and_adoption",
    "long_term_price_and_drawdown_history",
    "major_risks",
    "bull_case",
    "bear_case",
)

RESEARCH_COMPONENTS_FOR_ASSET_TYPE = {
    ASSET_US_COMMON_STOCK: (RESEARCH_COMPONENTS_EQUITY, RESEARCH_COMPONENTS_EQUITY_MANDATORY),
    ASSET_US_ETF: (RESEARCH_COMPONENTS_ETF, RESEARCH_COMPONENTS_ETF_MANDATORY),
    ASSET_CRYPTO: (RESEARCH_COMPONENTS_CRYPTO, RESEARCH_COMPONENTS_CRYPTO_MANDATORY),
}


# --- primary sources ------------------------------------------------------
# Evidence drawn from these tools counts as primary/official financial data.
# Analyst opinion may inform a thesis but can never satisfy this requirement.
PRIMARY_SOURCE_TOOLS = frozenset(
    {
        "get_sec_filing",
        "get_sec_filing_index",
        "get_sec_filing_facts",
        "get_sec_filing_facts_catalog",
        "get_financials",
        "get_equity_fundamentals",
        "get_earnings_results",
    }
)
PRIMARY_SOURCE_TYPES = frozenset(
    {"SEC_FILING", "COMPANY_IR", "EARNINGS_RELEASE", "OFFICIAL_FINANCIALS"}
)
# Minimum distinct primary-source evidence entries for a new equity position.
MIN_PRIMARY_SOURCES_NEW_EQUITY = 2

# --- crypto sources -------------------------------------------------------
# A cryptoasset files nothing with the SEC and publishes no income statement,
# so the equity source hierarchy above cannot be met and demanding it would
# make every crypto candidate permanently unbuyable for the wrong reason.
# These are crypto's equivalents: the protocol's own published record, the
# chain itself, the governance process, the regulator, and reputable data
# providers. Read-only external research is how they are obtained.
CRYPTO_PRIMARY_SOURCE_TYPES = frozenset(
    {
        "PROTOCOL_DOCUMENTATION",   # a whitepaper, spec, or improvement proposal
        "PROJECT_OFFICIAL",         # the foundation's or core team's own publication
        "CORE_DEV_REPOSITORY",      # the reference implementation and its release notes
        "ONCHAIN_DATA",             # supply, issuance, fees, addresses, read from the chain
        "GOVERNANCE_RECORD",        # an executed on-chain or foundation governance decision
        "REGULATORY_PUBLICATION",   # a regulator's own filing, order, or guidance
        "RESEARCH_DATA_PROVIDER",   # a reputable aggregator, named and dated
    }
)
MIN_PRIMARY_SOURCES_NEW_CRYPTO = 2

# What Robinhood is authoritative for. Nothing external may override these,
# because they describe this account rather than the world.
ROBINHOOD_AUTHORITATIVE_SUBJECTS = (
    "holdings",
    "cost_basis",
    "pair_eligibility",
    "tradability",
    "orders",
    "execution",
)

# ...and, correspondingly, the excuses that no longer justify a research gap.
# Robinhood is a broker, not a research database. It publishes no network
# statistics, issuance schedule, protocol roadmap or regulatory analysis, and it
# was never going to. "The connected tools do not expose it" is therefore a
# statement about the tool surface, not about whether the fact is obtainable —
# and a component that a protocol publishes openly must be researched with
# read-only external sources instead of waived.
ROBINHOOD_SCOPE_EXCUSE_PATTERNS = (
    r"robinhood\s*,?\s+(?:[\w,'-]+\s+){0,4}?(?:does\s+not|doesn'?t|do\s+not|don'?t|cannot|can'?t|has\s+no|have\s+no|lacks?|provides?\s+no|exposes?\s+no|returns?\s+no|offers?\s+no)",
    r"(?:not|never|un)?(?:available|exposed|provided|returned|surfaced)\s+(?:by|from|through|via)\s+(?:the\s+)?(?:robinhood|mcp)",
    r"no\s+robinhood\s+(?:tool|endpoint|api|field|data)",
    r"(?:the\s+)?(?:connected|available|permitted|allowed)\s+(?:read[- ]only\s+)?(?:mcp\s+)?tools?\s+(?:do\s+not|don'?t|does\s+not|doesn'?t|cannot|can'?t|provide|expose|offer|return)",
    r"(?:the\s+)?mcp\s+(?:server|tool\s*set|tools?)\s+(?:does\s+not|do\s+not|doesn'?t|don'?t|cannot|can'?t|has\s+no|lacks?)",
    r"no\s+(?:connected|available|permitted)\s+tool\s+(?:exposes?|provides?|returns?|offers?)",
    r"(?:outside|beyond)\s+(?:the\s+)?(?:scope\s+of\s+)?(?:the\s+)?(?:robinhood|connected)\s+tool",
)


# --------------------------------------------------------------------------
# Calendar-boundary event actionability
# --------------------------------------------------------------------------
#
# The $25 does not roll over, which makes the month's last tradable moment a
# hard edge. An event landing after it teaches something about *next* month's
# capital, not this month's, and a decision that treats it as this month's
# information is simply wrong about the calendar.
#
# `src/market_calendar.py` computes the edge. These are the payload fields
# through which a decision states its own view of it, so the two can be
# compared rather than trusted.
REQUIRED_EVENT_KEYS = ("label", "date", "actionable_with_this_month_authorization")

# The field in which a decision acknowledges that waiting through a
# next-month-only event lets this month's authorization expire unused. That is
# a permitted choice — WAIT is a first-class outcome and an expired
# authorization is not a failure — but it must be a stated choice, never an
# accident dressed up as patience.
AUTHORIZATION_EXPIRY_FIELD = "authorization_expiry_acknowledged"

# Where a decision lists the dated events it weighed.
EVENTS_FIELD = "events_considered"


# --- theme taxonomy -------------------------------------------------------
# subtheme -> broad theme. A position addresses a SUBTHEME. Claiming it fills a
# whole broad theme is a category error the validator refuses.
THEME_TAXONOMY = {
    # healthcare
    "robotic_assisted_surgery": "healthcare",
    "medical_devices": "healthcare",
    "pharmaceuticals": "healthcare",
    "biotechnology": "healthcare",
    "healthcare_services": "healthcare",
    "life_science_tools": "healthcare",
    # robotics & automation (industrial/physical, NOT medical)
    "industrial_robotics": "robotics_automation",
    "humanoid_robotics": "robotics_automation",
    "warehouse_automation": "robotics_automation",
    "motion_control": "robotics_automation",
    "machine_vision": "robotics_automation",
    "factory_automation": "robotics_automation",
    # autonomous systems
    "autonomous_vehicles": "autonomous_systems",
    "drones_and_uas": "autonomous_systems",
    # storage & memory
    "nand_flash_storage": "storage_memory",
    "hdd_nearline_storage": "storage_memory",
    "dram_and_hbm_memory": "storage_memory",
    "enterprise_storage_systems": "storage_memory",
    "storage_controllers": "storage_memory",
    # semiconductors
    "ai_accelerators": "semiconductors",
    "semiconductor_equipment": "semiconductors",
    "custom_silicon_asic": "semiconductors",
    "analog_and_power_semis": "semiconductors",
    "foundry": "semiconductors",
    # networking
    "optical_components": "networking",
    "optical_transport": "networking",
    "datacenter_switching": "networking",
    "telecom_equipment": "networking",
    # datacenter physical infrastructure
    "datacenter_power_and_cooling": "datacenter_infrastructure",
    "datacenter_reits": "datacenter_infrastructure",
    # energy & grid
    "power_generation": "energy_infrastructure",
    "grid_equipment": "energy_infrastructure",
    "grid_construction_services": "energy_infrastructure",
    "utilities": "energy_infrastructure",
    "nuclear": "energy_infrastructure",
    "oil_and_gas": "energy_infrastructure",
    # software & internet
    "cloud_platforms": "software_internet",
    "digital_advertising": "software_internet",
    "enterprise_software": "software_internet",
    "cybersecurity": "software_internet",
    "ip_licensing": "software_internet",
    "fintech_software": "software_internet",
    # consumer
    "ecommerce": "consumer",
    "streaming_media": "consumer",
    "consumer_staples": "consumer",
    "consumer_hardware": "consumer",
    "electric_vehicles": "consumer",
    "travel_and_leisure": "consumer",
    # financials
    "banks": "financials",
    "payments": "financials",
    "exchanges_and_brokers": "financials",
    "insurance": "financials",
    # industrials & materials
    "aerospace_defense": "industrials",
    "industrial_machinery": "industrials",
    "engineering_and_construction": "industrials",
    "rare_earths_and_critical_materials": "materials",
    "precious_metals": "materials",
    # diversified index exposure
    "broad_us_equity": "diversified_index",
    "us_large_cap_growth": "diversified_index",
    "total_world_equity": "diversified_index",
    "international_equity": "diversified_index",
    # crypto
    "store_of_value_crypto": "crypto",
    "smart_contract_platform": "crypto",
    "crypto_infrastructure": "crypto",
    "payments_crypto": "crypto",
    "attention_driven_crypto": "crypto",
}

BROAD_THEMES = frozenset(THEME_TAXONOMY.values())


def subthemes_for(broad_theme: str) -> List[str]:
    """Every subtheme that rolls up to ``broad_theme``."""
    return sorted(k for k, v in THEME_TAXONOMY.items() if v == broad_theme)


# --- structured reasoning blocks a BUY must carry -------------------------
REQUIRED_OPTIONALITY_KEYS = (
    "days_remaining_in_month",
    "known_events_this_month",
    "candidates_being_watched",
    "probability_ranking_changes",
    "dry_powder_benefit",
    "partial_deployment_considered",
)
REQUIRED_NEW_POSITION_KEYS = (
    "incremental_expected_return",
    "diversification_benefit",
    "overlap_with_existing",
    "diversification_is_economic_not_cosmetic",
    "best_existing_alternative",
    "why_new_position_beats_adding_to_existing",
    "initial_size_significance",
)
REQUIRED_SPRAWL_KEYS = (
    "position_count",
    "smallest_positions",
    "economic_significance_of_this_add",
    "overlap_analysis",
    "concentration_alternative",
)
REQUIRED_THEME_KEYS = ("primary", "subtheme", "scope_caveat")

# Phrases that, on their own, are NOT a sufficient reason to buy rather than wait.
INSUFFICIENT_TIMING_PATTERNS = (
    r"no\s+new\s+information",
    r"nothing\s+to\s+wait\s+for",
    r"absence\s+of\s+a\s+reason\s+to\s+wait",
    r"no\s+(pending|upcoming|near[- ]term)\s+catalyst",
    r"waiting\s+(would\s+)?buys?\s+no\s+information",
)


# --------------------------------------------------------------------------
# Stage 7 — allocation plans
# --------------------------------------------------------------------------
#
# An evaluation now returns a *plan* rather than a bare decision. A plan is a
# thin container: every BUY leg inside it is an ordinary, complete decision
# payload that goes through the unchanged single-decision validator, keeps its
# own decision_id, and later earns its own fingerprint, approval, submission
# ticket and reconciliation. The plan adds only the cross-leg arithmetic and
# the cross-leg reasoning that a single decision cannot express.
#
# Splitting is never required. One high-conviction purchase, or WAIT, remains
# a first-class and frequently better answer.

PLAN_WAIT = "WAIT"
PLAN_SINGLE_BUY = "SINGLE_BUY"
PLAN_SPLIT_BUY = "SPLIT_BUY_PLAN"
VALID_PLAN_TYPES = (PLAN_WAIT, PLAN_SINGLE_BUY, PLAN_SPLIT_BUY)

# A split must be a genuine allocation decision, not a scatter.
MIN_SPLIT_LEGS = 2
MAX_SPLIT_LEGS = 5

# How many legs each plan type may carry.
LEG_COUNT_FOR_PLAN = {
    PLAN_WAIT: (0, 0),
    PLAN_SINGLE_BUY: (1, 1),
    PLAN_SPLIT_BUY: (MIN_SPLIT_LEGS, MAX_SPLIT_LEGS),
}

# Every leg of a split must say why this dollar is better spent here than on
# the sibling legs it was weighed against. A leg that cannot answer that has
# not earned its slice.
REQUIRED_LEG_ALLOCATION_KEYS = (
    "why_better_than_other_legs",
    "why_this_amount",
    "legs_compared_against",
)

# A split creates several positions at once, which is exactly when sprawl is
# most likely and least noticed.
REQUIRED_PLAN_SPRAWL_KEYS = (
    "positions_after_this_plan",
    "why_not_concentrate_into_one",
    "smallest_leg_significance",
)


# --- the five competing uses of the month's capital -----------------------
#
# Equities, ETFs and direct Robinhood-supported crypto compete for ONE $25
# authorization. An evaluation must show the competition it actually ran, and
# crypto is a full competitor rather than an afterthought appended at the end.
#
# A bucket that genuinely does not apply -- there is no existing crypto
# position to add to, say -- must say so explicitly with a NOT_APPLICABLE
# prefix and a reason. Silence is not the same as inapplicable.
NOT_APPLICABLE_PREFIX = "NOT_APPLICABLE"

CAPITAL_USE_ADD_EQUITY = "add_to_existing_equity"
CAPITAL_USE_NEW_EQUITY = "open_new_equity_or_etf"
CAPITAL_USE_ADD_CRYPTO = "add_to_existing_crypto"
CAPITAL_USE_NEW_CRYPTO = "open_new_crypto"
CAPITAL_USE_WAIT = "preserve_budget_as_cash"

CAPITAL_USES = (
    CAPITAL_USE_ADD_EQUITY,
    CAPITAL_USE_NEW_EQUITY,
    CAPITAL_USE_ADD_CRYPTO,
    CAPITAL_USE_NEW_CRYPTO,
    CAPITAL_USE_WAIT,
)

# The four non-WAIT buckets a WAIT decision must name a strongest candidate in.
# WAIT itself is the fifth use and is the decision being made, so it needs no
# separate candidate. Each entry is (canonical_key, legacy_aliases) -- the
# aliases keep decisions logged before Stage 7 valid.
WAIT_CANDIDATE_BUCKETS = (
    ("best_existing_equity_candidate", ("best_existing_position_candidate",)),
    ("best_new_equity_candidate", ()),
    ("best_existing_crypto_candidate", ("best_crypto_candidate",)),
    ("best_new_crypto_candidate", ("best_crypto_candidate",)),
)

# The two buckets whose research is most often waved away rather than done,
# because Robinhood publishes none of the inputs a crypto thesis needs.
CRYPTO_WAIT_BUCKETS = ("best_existing_crypto_candidate", "best_new_crypto_candidate")
