"""Configuration and monthly budget state.

Design rules enforced here:

* ``config.json`` is the single canonical source of the monthly budget. No other
  module hard-codes a dollar amount.
* Corrupt state **fails closed**: it raises :class:`CorruptStateError` with an
  explanation and never silently rewrites the file.
* Month rollover resets *spending*, never *authorization*. A new month is always
  authorized for exactly ``monthly_budget_usd`` — unused prior-month budget is
  discarded.
* Writes are atomic (temp file in the same directory, ``fsync``, ``os.replace``).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .models import CENTS, ZERO, MoneyError, is_whole_cents, money_str, parse_money

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
DEFAULT_BUDGET_PATH = os.path.join(REPO_ROOT, "state", "budget.json")
DEFAULT_LAST_EVAL_PATH = os.path.join(REPO_ROOT, "state", "last_evaluation.json")
DEFAULT_CRYPTO_UNIVERSE_PATH = os.path.join(REPO_ROOT, "data", "crypto_universe.json")
DEFAULT_WATCHLIST_PATH = os.path.join(REPO_ROOT, "state", "watchlist_snapshot.json")

# A crypto snapshot older than this is still usable, but the validator warns and
# the evaluation prompt is required to refresh it.
CRYPTO_UNIVERSE_MAX_AGE_DAYS = 30

BUDGET_SCHEMA_VERSION = 1
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# Absolute ceiling on what the config file is allowed to authorize. This is a
# tripwire against a runaway edit, not a second source of truth: the real budget
# is whatever config.json says, and this only bounds how wrong it may be.
CONFIG_SANITY_MAX_BUDGET = Decimal("1000.00")


class ConfigError(Exception):
    """Raised when config.json is missing, malformed, or unsafe."""


class CorruptStateError(Exception):
    """Raised when budget state cannot be trusted. Always fails closed."""


class BudgetError(Exception):
    """Raised when a budget mutation would break an invariant."""


class CryptoUniverseError(Exception):
    """Raised when the Robinhood crypto pair snapshot cannot be trusted."""


# --------------------------------------------------------------------------
# Time
# --------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def current_month(now: Optional[datetime] = None) -> str:
    """The authorization month as ``YYYY-MM``, in the **project** timezone.

    Not UTC. Which month a purchase belongs to is the owner's calendar
    question, and answering it in UTC rolls the budget over five to six hours
    early — so a purchase made on the evening of the 31st would be charged to
    the following month's authorization. The boundary follows the project
    timezone for equities and crypto alike, because it is a budget concept
    rather than a market one.
    """
    from .market_calendar import project_month  # local: avoid an import cycle

    return project_month(now)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    monthly_budget_usd: Decimal
    min_investment_horizon_months: int
    execution_mode: str
    agent_enabled: bool
    live_trading: bool
    allow_selling: bool
    allow_options: bool
    allow_margin: bool
    allow_shorting: bool
    allow_crypto: bool
    allow_leveraged_etfs: bool
    allow_inverse_etfs: bool
    allow_transfers: bool
    strategy: str
    path: str = DEFAULT_CONFIG_PATH

    def to_dict(self) -> Dict[str, Any]:
        return {
            "monthly_budget_usd": money_str(self.monthly_budget_usd),
            "min_investment_horizon_months": self.min_investment_horizon_months,
            "execution_mode": self.execution_mode,
            "agent_enabled": self.agent_enabled,
            "live_trading": self.live_trading,
            "allow_selling": self.allow_selling,
            "allow_options": self.allow_options,
            "allow_margin": self.allow_margin,
            "allow_shorting": self.allow_shorting,
            "allow_crypto": self.allow_crypto,
            "allow_leveraged_etfs": self.allow_leveraged_etfs,
            "allow_inverse_etfs": self.allow_inverse_etfs,
            "allow_transfers": self.allow_transfers,
            "strategy": self.strategy,
        }


# The three supported execution modes. Anything else fails closed at load time.
EXECUTION_MODE_DRY_RUN = "DRY_RUN"
EXECUTION_MODE_APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
EXECUTION_MODE_AUTONOMOUS = "AUTONOMOUS"
EXECUTION_MODES = (
    EXECUTION_MODE_DRY_RUN,
    EXECUTION_MODE_APPROVAL_REQUIRED,
    EXECUTION_MODE_AUTONOMOUS,
)

_BOOL_FLAGS = (
    "agent_enabled",
    "live_trading",
    "allow_selling",
    "allow_options",
    "allow_margin",
    "allow_shorting",
    "allow_crypto",
    "allow_leveraged_etfs",
    "allow_inverse_etfs",
    "allow_transfers",
)


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Config:
    """Load and validate config.json. Raises :class:`ConfigError` on any problem."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        raise ConfigError("config.json not found at %s" % path)
    except json.JSONDecodeError as exc:
        raise ConfigError("config.json is not valid JSON (%s)" % exc)
    except OSError as exc:
        raise ConfigError("config.json could not be read: %s" % exc)

    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object")

    if "monthly_budget_usd" not in raw:
        raise ConfigError("config.json is missing 'monthly_budget_usd'")
    try:
        budget = parse_money(raw["monthly_budget_usd"], "monthly_budget_usd")
    except MoneyError as exc:
        raise ConfigError("config.json: %s" % exc)

    if budget <= ZERO:
        raise ConfigError("monthly_budget_usd must be greater than zero")
    if not is_whole_cents(budget):
        raise ConfigError("monthly_budget_usd must be a whole number of cents")
    if budget > CONFIG_SANITY_MAX_BUDGET:
        raise ConfigError(
            "monthly_budget_usd of %s exceeds the sanity ceiling of %s; refusing to load"
            % (money_str(budget), money_str(CONFIG_SANITY_MAX_BUDGET))
        )

    flags: Dict[str, bool] = {}
    for name in _BOOL_FLAGS:
        if name not in raw:
            raise ConfigError("config.json is missing '%s'" % name)
        value = raw[name]
        if not isinstance(value, bool):
            raise ConfigError("config.json: '%s' must be a JSON boolean" % name)
        flags[name] = value

    mode = raw.get("execution_mode", EXECUTION_MODE_DRY_RUN)
    if not isinstance(mode, str) or mode.strip() not in EXECUTION_MODES:
        raise ConfigError(
            "config.json: 'execution_mode' must be exactly one of %s, got %r. An "
            "unrecognized mode fails closed rather than defaulting to anything."
            % (list(EXECUTION_MODES), mode)
        )
    mode = mode.strip()

    horizon = raw.get("min_investment_horizon_months", 24)
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
        raise ConfigError(
            "config.json: 'min_investment_horizon_months' must be a positive integer"
        )

    strategy = raw.get("strategy", "long_term_buy_and_hold")
    if not isinstance(strategy, str) or not strategy.strip():
        raise ConfigError("config.json: 'strategy' must be a non-empty string")

    return Config(
        monthly_budget_usd=budget.quantize(CENTS),
        min_investment_horizon_months=horizon,
        execution_mode=mode,
        strategy=strategy,
        path=path,
        **flags
    )


# --------------------------------------------------------------------------
# Budget state
# --------------------------------------------------------------------------


@dataclass
class BudgetState:
    month: str
    authorized_budget_usd: Decimal
    committed_usd: Decimal
    acted_decision_ids: List[str] = field(default_factory=list)
    last_updated: str = ""
    schema_version: int = BUDGET_SCHEMA_VERSION
    path: str = DEFAULT_BUDGET_PATH

    @property
    def remaining_usd(self) -> Decimal:
        return (self.authorized_budget_usd - self.committed_usd).quantize(CENTS)

    def has_acted(self, decision_id: str) -> bool:
        return decision_id in self.acted_decision_ids

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "month": self.month,
            "authorized_budget_usd": money_str(self.authorized_budget_usd),
            "committed_usd": money_str(self.committed_usd),
            "remaining_usd": money_str(self.remaining_usd),
            "acted_decision_ids": list(self.acted_decision_ids),
            "last_updated": self.last_updated,
        }


def fresh_state(
    config: Config,
    month: Optional[str] = None,
    acted_decision_ids: Optional[List[str]] = None,
    path: str = DEFAULT_BUDGET_PATH,
) -> BudgetState:
    """Build a clean state for ``month``, authorized at exactly the config budget.

    ``acted_decision_ids`` is carried forward so a decision can never be replayed
    in a later month. Money never carries forward.
    """
    return BudgetState(
        month=month or current_month(),
        authorized_budget_usd=config.monthly_budget_usd,
        committed_usd=ZERO,
        acted_decision_ids=list(acted_decision_ids or []),
        last_updated=iso_now(),
        path=path,
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CorruptStateError(
            message
            + " — refusing to continue. Inspect state/budget.json by hand and "
            "repair it, or reset it deliberately with scripts/check_status.py --init."
        )


def parse_budget_state(
    raw: Any, config: Config, path: str = DEFAULT_BUDGET_PATH
) -> BudgetState:
    """Validate a decoded budget-state document. Fails closed on anything odd."""
    _require(isinstance(raw, dict), "budget state must be a JSON object")

    version = raw.get("schema_version")
    _require(
        isinstance(version, int) and version == BUDGET_SCHEMA_VERSION,
        "budget state schema_version must be %d, got %r" % (BUDGET_SCHEMA_VERSION, version),
    )

    month = raw.get("month")
    _require(
        isinstance(month, str) and bool(MONTH_RE.match(month)),
        "budget state 'month' must look like YYYY-MM, got %r" % (month,),
    )

    for key in ("authorized_budget_usd", "committed_usd", "remaining_usd"):
        _require(key in raw, "budget state is missing '%s'" % key)
        _require(
            not isinstance(raw[key], float),
            "budget state '%s' must be a string, not a float — money must not be "
            "stored in binary floating point" % key,
        )

    try:
        authorized = parse_money(raw["authorized_budget_usd"], "authorized_budget_usd")
        committed = parse_money(raw["committed_usd"], "committed_usd")
        remaining = parse_money(raw["remaining_usd"], "remaining_usd")
    except MoneyError as exc:
        _require(False, "budget state has an unparseable money value: %s" % exc)
        raise  # unreachable; keeps type checkers happy

    _require(authorized >= ZERO, "authorized_budget_usd must not be negative")
    _require(committed >= ZERO, "committed_usd must not be negative")
    _require(
        is_whole_cents(authorized) and is_whole_cents(committed) and is_whole_cents(remaining),
        "budget state money values must be whole cents",
    )
    _require(
        committed <= authorized,
        "committed_usd (%s) exceeds authorized_budget_usd (%s)"
        % (money_str(committed), money_str(authorized)),
    )
    _require(
        (authorized - committed).quantize(CENTS) == remaining.quantize(CENTS),
        "budget state is internally inconsistent: authorized %s - committed %s != remaining %s"
        % (money_str(authorized), money_str(committed), money_str(remaining)),
    )
    _require(
        authorized <= config.monthly_budget_usd,
        "authorized_budget_usd (%s) exceeds the configured monthly budget (%s); "
        "unused budget must never accumulate"
        % (money_str(authorized), money_str(config.monthly_budget_usd)),
    )

    ids = raw.get("acted_decision_ids", [])
    _require(isinstance(ids, list), "acted_decision_ids must be a list")
    _require(
        all(isinstance(item, str) and item.strip() for item in ids),
        "acted_decision_ids must contain only non-empty strings",
    )
    _require(len(set(ids)) == len(ids), "acted_decision_ids contains duplicates")

    last_updated = raw.get("last_updated", "")
    _require(isinstance(last_updated, str), "last_updated must be a string")

    return BudgetState(
        month=month,
        authorized_budget_usd=authorized.quantize(CENTS),
        committed_usd=committed.quantize(CENTS),
        acted_decision_ids=list(ids),
        last_updated=last_updated,
        schema_version=version,
        path=path,
    )


def load_budget_state(
    config: Config,
    path: str = DEFAULT_BUDGET_PATH,
    now: Optional[datetime] = None,
) -> BudgetState:
    """Load budget state, applying month rollover in memory.

    A rollover is *not* written to disk here; it is persisted the next time state
    is saved. That keeps a read-only status check free of side effects.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise CorruptStateError(
            "budget state not found at %s. Create it with: "
            "python3 scripts/check_status.py --init" % path
        )
    except OSError as exc:
        raise CorruptStateError("budget state could not be read: %s" % exc)

    if not text.strip():
        raise CorruptStateError(
            "budget state at %s is empty. It will not be silently recreated; "
            "repair it or run scripts/check_status.py --init deliberately." % path
        )

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CorruptStateError(
            "budget state at %s is not valid JSON (%s). Refusing to overwrite it." % (path, exc)
        )

    state = parse_budget_state(raw, config, path=path)

    this_month = current_month(now)
    if state.month != this_month:
        if state.month > this_month:
            raise CorruptStateError(
                "budget state month %s is in the future (now %s). Refusing to continue."
                % (state.month, this_month)
            )
        # Month rollover: spending resets, authorization does NOT accumulate.
        state = fresh_state(
            config,
            month=this_month,
            acted_decision_ids=state.acted_decision_ids,
            path=path,
        )
    else:
        # Re-authorize at the configured amount in case the config changed.
        state.authorized_budget_usd = config.monthly_budget_usd

    return state


def commit_purchase(state: BudgetState, decision_id: str, amount: Decimal) -> BudgetState:
    """Return a new state with ``amount`` committed against ``decision_id``.

    Raises :class:`BudgetError` on a duplicate decision id or an overspend. This
    is the only supported way to increase ``committed_usd``.

    Note: version 1 never calls this from an evaluation, because no purchase is
    ever executed. It exists so the invariant is testable and so a future live
    version has exactly one place to record spending.
    """
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise BudgetError("decision_id must be a non-empty string")
    if state.has_acted(decision_id):
        raise BudgetError("decision_id %r has already been acted upon" % decision_id)

    amount = parse_money(amount, "amount")
    if amount <= ZERO:
        raise BudgetError("purchase amount must be greater than zero")
    if not is_whole_cents(amount):
        raise BudgetError("purchase amount must be a whole number of cents")

    new_committed = (state.committed_usd + amount).quantize(CENTS)
    if new_committed > state.authorized_budget_usd:
        raise BudgetError(
            "purchase of %s would bring this month's total to %s, over the "
            "authorized %s"
            % (
                money_str(amount),
                money_str(new_committed),
                money_str(state.authorized_budget_usd),
            )
        )

    return BudgetState(
        month=state.month,
        authorized_budget_usd=state.authorized_budget_usd,
        committed_usd=new_committed,
        acted_decision_ids=list(state.acted_decision_ids) + [decision_id],
        last_updated=iso_now(),
        schema_version=state.schema_version,
        path=state.path,
    )


# --------------------------------------------------------------------------
# Atomic persistence
# --------------------------------------------------------------------------


def atomic_write_json(path: str, payload: Any) -> None:
    """Write JSON to ``path`` atomically (same-directory temp + ``os.replace``)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=False) + "\n"

    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def save_budget_state(state: BudgetState, path: Optional[str] = None) -> None:
    state.last_updated = iso_now()
    atomic_write_json(path or state.path, state.to_dict())


def save_last_evaluation(record: Dict[str, Any], path: str = DEFAULT_LAST_EVAL_PATH) -> None:
    """Persist a summary of the most recent evaluation."""
    atomic_write_json(path, record)


def load_last_evaluation(path: str = DEFAULT_LAST_EVAL_PATH) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (FileNotFoundError, OSError):
        return None
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# Robinhood crypto universe snapshot
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CryptoPair:
    """One Robinhood-supported currency pair, as captured in the snapshot."""

    symbol: str
    name: Optional[str]
    code: Optional[str]
    tradability: str
    individual_tradability: str
    halted: bool
    halted_regions: List[str]
    display_only: bool
    min_order_size: Optional[Decimal]

    @property
    def is_purchasable(self) -> bool:
        return (
            self.tradability == "tradable"
            and self.individual_tradability == "tradable"
            and not self.halted
            and not self.display_only
        )


@dataclass(frozen=True)
class CryptoUniverse:
    """The set of crypto pairs the agent is deterministically allowed to name."""

    pairs: Dict[str, CryptoPair]
    fetched_at: str
    path: str

    def get(self, symbol: str) -> Optional[CryptoPair]:
        return self.pairs.get((symbol or "").upper())

    def age_days(self, now: Optional[datetime] = None) -> Optional[float]:
        try:
            stamp = datetime.strptime(self.fetched_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, TypeError):
            return None
        return ((now or utc_now()) - stamp).total_seconds() / 86400.0

    @property
    def purchasable_symbols(self) -> List[str]:
        return sorted(s for s, p in self.pairs.items() if p.is_purchasable)


def load_crypto_universe(
    path: str = DEFAULT_CRYPTO_UNIVERSE_PATH,
) -> CryptoUniverse:
    """Load the crypto pair snapshot. Fails closed — a missing or malformed
    snapshot means no crypto proposal can be validated at all."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise CryptoUniverseError(
            "crypto universe snapshot not found at %s. Refresh it with "
            "get_currency_pairs and scripts/update_crypto_universe.py before "
            "any crypto proposal can be validated." % path
        )
    except OSError as exc:
        raise CryptoUniverseError("crypto universe snapshot could not be read: %s" % exc)

    if not text.strip():
        raise CryptoUniverseError("crypto universe snapshot at %s is empty" % path)

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CryptoUniverseError(
            "crypto universe snapshot at %s is not valid JSON (%s)" % (path, exc)
        )

    if not isinstance(raw, dict) or not isinstance(raw.get("pairs"), dict):
        raise CryptoUniverseError(
            "crypto universe snapshot must be an object with a 'pairs' object"
        )
    if not raw["pairs"]:
        raise CryptoUniverseError("crypto universe snapshot contains no pairs")

    pairs: Dict[str, CryptoPair] = {}
    for symbol, entry in raw["pairs"].items():
        if not isinstance(entry, dict):
            raise CryptoUniverseError("crypto pair %r is not an object" % symbol)
        size = entry.get("min_order_size")
        try:
            min_size = parse_money(size, "min_order_size") if size is not None else None
        except MoneyError as exc:
            raise CryptoUniverseError("crypto pair %r: %s" % (symbol, exc))
        pairs[str(symbol).upper()] = CryptoPair(
            symbol=str(symbol).upper(),
            name=entry.get("name"),
            code=entry.get("code"),
            tradability=str(entry.get("tradability") or "unknown"),
            individual_tradability=str(entry.get("individual_tradability") or "unknown"),
            halted=bool(entry.get("halted")),
            halted_regions=list(entry.get("halted_regions") or []),
            display_only=bool(entry.get("display_only")),
            min_order_size=min_size,
        )

    fetched_at = raw.get("fetched_at")
    if not isinstance(fetched_at, str) or not fetched_at.strip():
        raise CryptoUniverseError("crypto universe snapshot is missing 'fetched_at'")

    return CryptoUniverse(pairs=pairs, fetched_at=fetched_at, path=path)
