"""Shared test fixtures. Nothing here touches Robinhood or the real state files."""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.guardrails import days_remaining_in_month  # noqa: E402
from src.models import ZERO  # noqa: E402
from src.state import BudgetState, Config, CryptoPair, CryptoUniverse, iso_now  # noqa: E402


def make_config(budget: str = "25.00", **overrides: Any) -> Config:
    values: Dict[str, Any] = {
        "monthly_budget_usd": Decimal(budget),
        "min_investment_horizon_months": 24,
        "execution_mode": "DRY_RUN",
        "agent_enabled": False,
        "live_trading": False,
        "allow_selling": False,
        "allow_options": False,
        "allow_margin": False,
        "allow_shorting": False,
        "allow_crypto": True,
        "allow_leveraged_etfs": False,
        "allow_inverse_etfs": False,
        "allow_transfers": False,
        "strategy": "long_term_buy_and_hold",
        "path": "<test config>",
    }
    values.update(overrides)
    return Config(**values)


def make_state(
    config: Optional[Config] = None,
    month: str = "2026-09",
    committed: str = "0.00",
    acted: Optional[list] = None,
) -> BudgetState:
    config = config or make_config()
    return BudgetState(
        month=month,
        authorized_budget_usd=config.monthly_budget_usd,
        committed_usd=Decimal(committed),
        acted_decision_ids=list(acted or []),
        last_updated="2026-09-01T00:00:00Z",
        path="<test state>",
    )


def _pair(symbol, **over):
    values = dict(
        symbol=symbol,
        name=symbol,
        code=symbol.split("-")[0],
        tradability="tradable",
        individual_tradability="tradable",
        halted=False,
        halted_regions=[],
        display_only=False,
        min_order_size=Decimal("0.000001"),
    )
    values.update(over)
    return CryptoPair(**values)


def make_crypto_universe(fetched_at: Optional[str] = None) -> CryptoUniverse:
    """A small, deterministic stand-in for the Robinhood pair snapshot."""
    pairs = {
        "BTC-USD": _pair("BTC-USD", name="Bitcoin", min_order_size=Decimal("0.000001")),
        "ETH-USD": _pair("ETH-USD", name="Ethereum", min_order_size=Decimal("0.0001")),
        "SOL-USD": _pair("SOL-USD", name="Solana", min_order_size=Decimal("0.0001")),
        "LINK-USD": _pair("LINK-USD", name="Chainlink", min_order_size=Decimal("0.01")),
        "DOGE-USD": _pair("DOGE-USD", name="Dogecoin", min_order_size=Decimal("0.1")),
        "BONK-USD": _pair("BONK-USD", name="Bonk", min_order_size=Decimal("1000")),
        # halted
        "TRUMP-USD": _pair("TRUMP-USD", name="OFFICIAL TRUMP", halted=True,
                           halted_regions=["NY"], min_order_size=Decimal("0.001")),
        # listed but not tradable in an individual account
        "BILL-USD": _pair("BILL-USD", name="Billions Network",
                          individual_tradability="untradable"),
        # display-only
        "GRAM-USD": _pair("GRAM-USD", name="Gram", display_only=True,
                          tradability="untradable"),
    }
    return CryptoUniverse(
        pairs=pairs,
        fetched_at=fetched_at or iso_now(),
        path="<test crypto universe>",
    )


# --------------------------------------------------------------------------
# Decision builders — each returns a decision that passes every rule before
# overrides are applied, so a test can isolate exactly one failure mode.
# --------------------------------------------------------------------------


def research_package(kind: str = "equity", **overrides: Any) -> Dict[str, Any]:
    """A complete research package for the given asset kind."""
    packages = {
        "equity": {
            "current_quote_and_valuation": "Last $300.00; 22x trailing earnings, 4.1x book.",
            "recent_quarterly_results": "Q2 revenue $2.89B, net income $818M, 28.3% net margin.",
            "multi_quarter_trends": "Revenue +41.9% across eight quarters; margin steady 27-31%.",
            "balance_sheet": "Net cash position; no meaningful debt maturities before 2029.",
            "cash_flow": "Operating cash flow covers capex roughly three times over.",
            "earnings_history": "Beat consensus EPS in each of the last eight quarters.",
            "material_news": "New manufacturing facility announced; two analyst upgrades.",
            "primary_filings": "10-Q reviewed: segment revenue and margin detail confirmed.",
            "competitive_position": "Roughly 75% share of its core market; installed-base moat.",
            "major_risks": "Valuation, international competition, and market saturation.",
            "upcoming_events": "Next earnings report in 46 days.",
            "bull_case": "Penetration is low and the installed base compounds recurring revenue.",
            "bear_case": "The de-rating is correct and margins grind down from here.",
        },
        "etf": {
            "current_quote_and_valuation": "Last $300.00; index P/E 27.4.",
            "index_and_methodology": "Market-cap weighted, full US total market.",
            "expense_ratio": "0.03% annually.",
            "holdings_concentration": "Top ten holdings are roughly a third of assets.",
            "overlap_with_existing_holdings": "Substantial overlap with VOO/SPY/QQQ already held.",
            "liquidity_and_spread": "Multi-million share average volume; one-cent spread.",
            "fund_size_and_age": "Hundreds of billions in assets, two decades of history.",
            "major_risks": "Broad equity drawdown risk and mega-cap concentration.",
            "bull_case": "Lowest-cost diversified access to US equity returns.",
            "bear_case": "Adds no exposure the portfolio does not already have.",
        },
        "crypto": {
            "current_quote_and_market_cap": "Mark $80,000.00; roughly $1.6T network value.",
            "supply_and_issuance": "Fixed 21M cap; issuance halves on a known schedule.",
            "network_usage_and_adoption": "Settlement volume and active addresses both growing.",
            "ecosystem_and_development": "Steady protocol development; broad client diversity.",
            "security_and_decentralization": "Largest proof-of-work security budget; wide miner distribution.",
            "regulatory_risk": "Spot ETFs approved in the US; treatment elsewhere is uneven.",
            "competitive_networks": "No network contests the store-of-value role at similar scale.",
            "long_term_price_and_drawdown_history": "Four drawdowns exceeding 70% since inception.",
            "major_risks": "Extreme volatility, regulatory reversal, and correlation with risk assets.",
            "bull_case": "Durable monetary premium with a fixed supply schedule.",
            "bear_case": "No cash flows; the entire valuation rests on continued adoption.",
        },
    }
    package = dict(packages[kind])
    package.update(overrides)
    return package


def _narrative(prefix: str) -> Dict[str, Any]:
    return {
        "thesis": "%s: durable long-term exposure at a defensible entry." % prefix,
        "value_creation": "Future value comes from unit growth and margin expansion, not multiple expansion.",
        "valuation_reasoning": "Trades near the low end of its own five-year range on forward earnings.",
        "timing_reason": "The entry price already offers an adequate expected return; the case does not depend on a pending event.",
        "why_not_wait": "Waiting would require expecting a materially better price, which is a forecast this agent will not make; expected return at $300.00 already clears the threshold.",
        "margin_for_error": "A 30% multiple compression would still leave the position above the cost basis within the stated horizon.",
        "alternatives_considered": [
            {"symbol": "ITOT", "provenance": "OPEN_DISCOVERY", "why_not": "Equivalent exposure, no edge."},
            {"symbol": "BTC-USD", "provenance": "CURRENT_HOLDING", "why_not": "Already the largest single exposure."},
        ],
        "portfolio_exposure": "This subtheme is currently 4% of the portfolio.",
        "risks": "Cyclical demand, customer concentration, and multiple compression.",
        "invalidation": "Two consecutive quarters of declining unit volume would end the thesis.",
        "evidence": [
            {"tool": "get_equity_quotes", "detail": "last price as of today"},
            {"tool": "get_financials", "source_type": "OFFICIAL_FINANCIALS", "detail": "eight quarters of revenue and margin"},
            {"tool": "get_sec_filing_facts", "source_type": "SEC_FILING", "detail": "balance sheet concepts from the latest 10-Q"},
        ],
        "monthly_optionality": {
            "days_remaining_in_month": None,  # filled in by the builders
            "known_events_this_month": "One portfolio holding reports earnings later this month.",
            "candidates_being_watched": "Two names on the watchlist pending deeper research.",
            "probability_ranking_changes": "Low: no candidate is close enough to overtake this one on the available data.",
            "dry_powder_benefit": "Preserving budget would allow a response to the late-month print.",
            "partial_deployment_considered": "Considered a smaller tranche; the position would be too small to matter.",
        },
        "monthly_optionality_analysis": (
            "This purchase consumes more than half the month's authorization before mid-month. "
            "That is justified here because the alternative uses of the capital were explicitly "
            "ranked and none is close, the known late-month event affects a holding this decision "
            "does not touch, and a smaller tranche would produce a position too small to matter "
            "against the existing book."
        ),
        "portfolio_sprawl_assessment": {
            "position_count": "20 equity and 9 crypto positions today.",
            "smallest_positions": "Six positions are under $10.",
            "economic_significance_of_this_add": "A $25 position is 1.9% of the portfolio, larger than 12 existing positions.",
            "overlap_analysis": "No meaningful overlap with existing holdings.",
            "concentration_alternative": "Adding to the strongest existing holding was compared and rejected on concentration grounds.",
        },
        "theme": {
            "primary": "diversified_index",
            "subtheme": "broad_us_equity",
            "scope_caveat": "This covers broad US equity only; it does not address international, small-cap, or fixed income.",
        },
    }


def buy_decision(state: BudgetState, amount: str, **overrides: Any) -> Dict[str, Any]:
    """A well-formed EQUITY/ETF BUY."""
    amount_dec = Decimal(amount)
    decision: Dict[str, Any] = {
        "decision_id": "test-eq-" + amount.replace(".", "").replace("-", "n") + "-" + state.month,
        "decision": "BUY",
        "action": "buy",
        "side": "buy",
        "asset_class": "ETF",
        "position_type": "NEW_POSITION",
        "classification": "PROMISING",
        "ticker": "VTI",
        "security_name": "Vanguard Total Stock Market ETF",
        "asset_type": "us_etf",
        "exchange": "NYSEARCA",
        "proposed_amount_usd": amount,
        "current_price_usd": "300.00",
        "quote_timestamp": "2026-09-04T15:30:00Z",
        "fractional_eligible": True,
        "investment_horizon_months": 60,
        "monthly_budget_before_usd": str(state.remaining_usd),
        "monthly_budget_after_usd": str(state.remaining_usd - amount_dec),
        "confidence": "MEDIUM",
    }
    decision.update(_narrative("Broad market index"))
    decision["monthly_optionality"]["days_remaining_in_month"] = days_remaining_in_month()
    decision["research_package"] = research_package("etf")
    decision["new_position_justification"] = new_position_justification()
    decision.update(overrides)
    return decision


def crypto_decision(state: BudgetState, amount: str, **overrides: Any) -> Dict[str, Any]:
    """A well-formed CRYPTO BUY."""
    amount_dec = Decimal(amount)
    decision: Dict[str, Any] = {
        "decision_id": "test-cr-" + amount.replace(".", "").replace("-", "n") + "-" + state.month,
        "decision": "BUY",
        "action": "buy",
        "side": "buy",
        "asset_class": "CRYPTO",
        "position_type": "NEW_POSITION",
        "classification": "PROMISING",
        "ticker": "BTC-USD",
        "security_name": "Bitcoin",
        "asset_type": "crypto",
        "proposed_amount_usd": amount,
        "current_price_usd": "80000.00",
        "quote_timestamp": "2026-09-04T23:40:00Z",
        "investment_horizon_months": 60,
        "monthly_budget_before_usd": str(state.remaining_usd),
        "monthly_budget_after_usd": str(state.remaining_usd - amount_dec),
        "confidence": "MEDIUM",
    }
    decision.update(_narrative("Bitcoin"))
    decision["monthly_optionality"]["days_remaining_in_month"] = days_remaining_in_month()
    # A blockchain files no 10-Q, so the equity evidence the shared narrative
    # carries cannot be the primary record here. Crypto's record is the
    # protocol's own specification, the chain itself, and reputable providers —
    # obtained by read-only external research, since Robinhood publishes none
    # of it and is authoritative only for the account side.
    decision["evidence"] = [
        {
            "tool": "get_crypto_quotes",
            "source_type": "MARKET_DATA",
            "detail": "mark price as of the quote timestamp",
        },
        {
            "tool": "WebFetch",
            "source_type": "PROTOCOL_DOCUMENTATION",
            "detail": "the protocol specification's fixed 21M supply cap and halving schedule",
        },
        {
            "tool": "WebFetch",
            "source_type": "ONCHAIN_DATA",
            "detail": "circulating supply, issuance to date and active-address trend, dated",
        },
        {
            "tool": "get_currency_pairs",
            "source_type": "BROKER_ELIGIBILITY",
            "detail": "Robinhood is authoritative here: pair supported, tradable, not halted",
        },
    ]
    decision["research_package"] = research_package("crypto")
    decision["new_position_justification"] = new_position_justification()
    decision["theme"] = {
        "primary": "crypto",
        "subtheme": "store_of_value_crypto",
        "scope_caveat": "Store-of-value crypto only; this is not exposure to smart-contract platforms or crypto infrastructure.",
    }
    decision.update(overrides)
    return decision


def new_position_justification(**overrides: Any) -> Dict[str, Any]:
    block = {
        "incremental_expected_return": "Higher expected forward return than the best existing alternative.",
        "diversification_benefit": "Adds a return driver uncorrelated with the existing book.",
        "overlap_with_existing": "No existing holding provides comparable exposure.",
        "diversification_is_economic_not_cosmetic": "The new driver is procedure volume, not the AI capex cycle that explains most of the book.",
        "best_existing_alternative": {"symbol": "GOOGL", "why_not": "Already the fourth-largest position in a saturated theme."},
        "why_new_position_beats_adding_to_existing": "The existing alternative is already large and correlated with the rest of the portfolio.",
        "initial_size_significance": "At $25 the position is 1.9% of the portfolio, larger than 12 existing positions.",
    }
    block.update(overrides)
    return block


def add_to_existing(state: BudgetState, amount: str, **overrides: Any) -> Dict[str, Any]:
    """A well-formed BUY that adds to an existing position, with cost-basis analysis."""
    decision = buy_decision(state, amount)
    decision.pop("new_position_justification", None)
    decision.pop("research_package", None)
    decision.update(
        {
            "decision_id": "test-add-" + amount.replace(".", "") + "-" + state.month,
            "position_type": "EXISTING_POSITION",
            "classification": "ADD_CANDIDATE",
            "ticker": "NVDA",
            "security_name": "NVIDIA Corporation",
            "asset_type": "us_common_stock",
            "asset_class": "EQUITY",
            "exchange": "NASDAQ",
            "current_price_usd": "230.00",
            "theme": {
                "primary": "semiconductors",
                "subtheme": "ai_accelerators",
                "scope_caveat": "AI accelerators only; this does not address memory, networking, or semiconductor equipment.",
            },
            "cost_basis_analysis": {
                "average_cost_usd": "83.92",
                "current_price_usd": "230.00",
                "raises_or_lowers_average": "RAISES",
                "economic_meaning": "The average cost rising is not itself a cost; what matters "
                                    "is expected forward return from today's price.",
            },
        }
    )
    decision.update(overrides)
    return decision


def wait_decision(state: BudgetState, **overrides: Any) -> Dict[str, Any]:
    decision: Dict[str, Any] = {
        "decision_id": "test-wait-" + state.month,
        "decision": "WAIT",
        "confidence": "MEDIUM",
        "wait_basis": ["VALUATION", "RISK_REWARD_MEDIOCRE"],
        "monthly_budget_remaining_usd": str(state.remaining_usd),
        "thesis": "No candidate currently clears the quality-and-price bar.",
        "timing_reason": "Re-evaluate after this month's earnings from the memory names.",
        "alternatives_considered": [{"symbol": "VTI", "why_not": "Prefer to accumulate later."}],
        "evidence": [{"tool": "get_portfolio", "detail": "current allocation"}],
        # The five-way capital-use comparison. WAIT itself is the fifth use.
        "best_existing_equity_candidate": {
            "symbol": "NVDA", "classification": "HOLD_NO_ADD",
            "why_not": "Already the largest equity exposure.",
        },
        "best_new_equity_candidate": {
            "symbol": "SNDK", "classification": "ATTRACTIVE_ON_PULLBACK",
            "why_not": "Wants a better entry after the recent run.",
        },
        "best_existing_crypto_candidate": {
            "symbol": "BTC-USD", "classification": "HOLD_NO_ADD",
            "why_not": "Already the largest single crypto exposure; adding worsens it.",
        },
        "best_new_crypto_candidate": {
            "symbol": "ETH-USD", "classification": "WATCH",
            "why_not": "Prefer to see the network upgrade land first.",
        },
    }
    decision.update(overrides)
    return decision


def legacy_wait_decision(state: BudgetState, **overrides: Any) -> Dict[str, Any]:
    """A WAIT in the pre-Stage-7 three-bucket shape, for the alias path."""
    decision = wait_decision(state)
    decision["best_existing_position_candidate"] = decision.pop(
        "best_existing_equity_candidate")
    decision["best_crypto_candidate"] = decision.pop("best_existing_crypto_candidate")
    decision.pop("best_new_crypto_candidate", None)
    decision.update(overrides)
    return decision


# --------------------------------------------------------------------------
# Stage 7 — allocation plans
# --------------------------------------------------------------------------


def allocation_rationale(compared: str = "BTC-USD", **overrides: Any) -> Dict[str, Any]:
    """The cross-leg reasoning every split leg must carry."""
    block: Dict[str, Any] = {
        "why_better_than_other_legs": (
            "This leg buys broad-market exposure whose expected return is less "
            "dependent on a single thesis holding, so the first dollars go here."
        ),
        "why_this_amount": (
            "Sized so the remainder still funds the second leg meaningfully rather "
            "than leaving it economically trivial."
        ),
        "legs_compared_against": "Weighed directly against %s in this plan." % compared,
    }
    block.update(overrides)
    return block


def plan_sprawl_assessment(**overrides: Any) -> Dict[str, Any]:
    block: Dict[str, Any] = {
        "positions_after_this_plan": "Two positions in an account that currently holds none.",
        "why_not_concentrate_into_one": (
            "The two legs are exposed to genuinely different return drivers, so the "
            "diversification is economic rather than cosmetic."
        ),
        "smallest_leg_significance": (
            "The smaller leg is still above the broker minimum and large enough to "
            "matter at this account's scale."
        ),
    }
    block.update(overrides)
    return block


def split_plan(
    state: BudgetState,
    amounts: Optional[List[str]] = None,
    **overrides: Any,
) -> Dict[str, Any]:
    """A well-formed SPLIT_BUY_PLAN whose legs total within the authorization."""
    amounts = amounts or ["15.00", "10.00"]
    legs: List[Dict[str, Any]] = []
    for index, amount in enumerate(amounts):
        if index == 0:
            leg = buy_decision(state, amount)
            leg["decision_id"] = "test-leg%d-eq-%s" % (index, state.month)
            leg["allocation_rationale"] = allocation_rationale("BTC-USD")
        else:
            leg = crypto_decision(state, amount)
            leg["decision_id"] = "test-leg%d-cr-%s" % (index, state.month)
            pairs = ["BTC-USD", "ETH-USD", "SOL-USD", "LINK-USD", "DOGE-USD"]
            leg["ticker"] = pairs[(index - 1) % len(pairs)]
            leg["allocation_rationale"] = allocation_rationale("VTI")
        legs.append(leg)

    # Each leg reports the budget it actually faces, after the legs ahead of it.
    running = ZERO
    for leg in legs:
        before = state.remaining_usd - running
        amount_dec = Decimal(str(leg["proposed_amount_usd"]))
        leg["monthly_budget_before_usd"] = str(before)
        leg["monthly_budget_after_usd"] = str(before - amount_dec)
        running += amount_dec

    plan: Dict[str, Any] = {
        "plan_id": "plan-test-" + state.month,
        "plan_type": "SPLIT_BUY_PLAN",
        "split_rationale": (
            "Splitting beats concentrating here because the two candidates clear the "
            "bar on independent grounds and their return drivers are uncorrelated, so "
            "the combined risk-adjusted expectation is better than either alone. This "
            "is a deliberate allocation, not a way to avoid choosing."
        ),
        "plan_sprawl_assessment": plan_sprawl_assessment(),
        "legs": legs,
    }
    plan.update(overrides)
    return plan


def single_buy_plan(state: BudgetState, amount: str = "25.00", **overrides: Any) -> Dict[str, Any]:
    leg = buy_decision(state, amount)
    plan: Dict[str, Any] = {
        "plan_id": "plan-single-" + state.month,
        "plan_type": "SINGLE_BUY",
        "legs": [leg],
    }
    plan.update(overrides)
    return plan


def wait_plan(state: BudgetState, **overrides: Any) -> Dict[str, Any]:
    plan: Dict[str, Any] = {
        "plan_id": "plan-wait-" + state.month,
        "plan_type": "WAIT",
        "legs": [],
        "decision": wait_decision(state),
    }
    plan.update(overrides)
    return plan


__all__ = [
    "research_package",
    "new_position_justification",
    "days_remaining_in_month",
    "make_config",
    "make_state",
    "make_crypto_universe",
    "buy_decision",
    "crypto_decision",
    "add_to_existing",
    "wait_decision",
    "legacy_wait_decision",
    "allocation_rationale",
    "plan_sprawl_assessment",
    "split_plan",
    "single_buy_plan",
    "wait_plan",
    "ZERO",
]
