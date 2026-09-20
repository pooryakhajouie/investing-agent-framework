"""The canonical BUY leg, in exactly one place.

## Why this module exists

On 2026-09-17 a scheduled run produced a BUY that could not be promoted. The
payload was not careless — it was *conformant to what it had been shown*. Its
nested blocks read ``positions_held``, ``economic_significance``,
``quote_and_valuation``, ``material_company_news``; the guardrails require
``position_count``, ``economic_significance_of_this_add``,
``current_quote_and_valuation``, ``material_news``. The run never saw the
difference, because the only document carrying the machine-readable template was
``prompts/evaluate_market.md`` — which a scheduled run does not read. Its own
prompt pointed at ``INVESTMENT_POLICY.md`` §11, a prose table that names fields
without spelling their nested keys and never mentions the ``decision`` verb at
all.

So the producer and the validators were specified by two different documents,
and the drift was invisible until a BUY actually needed promoting.

This module ends that by construction. :func:`buy_leg_template` builds a
*complete, guardrail-valid* leg from the same constants ``src.guardrails``
validates against (:mod:`src.models`), and both prompts now render **this**
template rather than describing one. Two properties are pinned by tests:

* the template passes :func:`src.guardrails.validate` unchanged; and
* the JSON block embedded in each prompt has the same key structure as the
  template, so a prompt cannot drift away from the code again.

## The two numbers a run may not invent

``days_remaining_in_month`` and ``tradable_sessions_remaining`` are checked
against the project's own exchange calendar, and a run that computes them by
hand gets them wrong — as 2026-09-17 did, reporting 13 and 9 against an actual
14 and 10. :func:`optionality_calendar` returns them from the very functions the
guardrail compares against, and ``scripts/emit_buy_scaffold.py`` prints them for
the run to copy verbatim.

Pure functions over plain data. No I/O, no network, no subprocess.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from .guardrails import days_remaining_in_month, tradable_sessions_remaining
from .market_calendar import final_opportunity, project_date
from .models import (
    ASSET_CRYPTO,
    ASSET_US_COMMON_STOCK,
    ASSET_US_ETF,
    RESEARCH_COMPONENTS_FOR_ASSET_TYPE,
)

#: The asset kinds a template can be rendered for, and the ``asset_type`` and
#: ``asset_class`` each one implies.
TEMPLATE_KINDS = {
    "equity": (ASSET_US_COMMON_STOCK, "EQUITY"),
    "etf": (ASSET_US_ETF, "ETF"),
    "crypto": (ASSET_CRYPTO, "CRYPTO"),
}

#: Placeholder prose is long enough to clear the narrative-substance checks, so
#: the rendered template is a *working* payload rather than a shape that only
#: looks right. A run replaces the text and keeps the keys.
_FILL = "<replace with this decision's own reasoning; keep the key>"


def optionality_calendar(now: Optional[datetime] = None) -> Dict[str, Any]:
    """The calendar facts a BUY must report rather than estimate.

    Deliberately delegates to ``src.guardrails``' own helpers instead of
    recomputing: a second implementation is how the payload and the check drift
    apart, which is the whole failure this module exists to prevent.
    """
    reference = now or datetime.utcnow()
    edge = final_opportunity(reference.year, reference.month, "EQUITY")
    return {
        "days_remaining_in_month": days_remaining_in_month(now),
        "tradable_sessions_remaining": tradable_sessions_remaining(now),
        "final_equity_opportunity": edge.strftime("%Y-%m-%d %H:%M"),
        "project_date": project_date(now).isoformat(),
    }


def _research_package(asset_type: str) -> Dict[str, str]:
    """Every component the guardrails require for this asset type, in order."""
    components, mandatory = RESEARCH_COMPONENTS_FOR_ASSET_TYPE[asset_type]
    package: Dict[str, str] = {}
    for component in components:
        if component in mandatory:
            package[component] = (
                "%s — required; findings, or 'NOT_AVAILABLE: <reason>'. "
                "Silence is not the same as unavailable." % _FILL)
        else:
            package[component] = (
                "%s — findings, or 'NOT_AVAILABLE: <reason>'." % _FILL)
    return package


#: What the two calendar fields read in a document rather than in a live payload.
#: A prompt that embedded today's numbers would be wrong tomorrow, so the
#: rendered contract points at the command instead of pretending to know.
CALENDAR_PLACEHOLDER = "<from `python3 scripts/emit_buy_scaffold.py` — never computed by hand>"


def buy_leg_template(
    now: Optional[datetime] = None,
    kind: str = "equity",
    position_type: str = "NEW_POSITION",
    calendar_values: bool = True,
) -> Dict[str, Any]:
    """A complete BUY leg carrying every key the guardrails require.

    The values are placeholders; the *keys* are the contract. Rendered into both
    prompts by ``scripts/emit_buy_scaffold.py``.
    """
    if kind not in TEMPLATE_KINDS:
        raise ValueError("unknown template kind %r; expected one of %s"
                         % (kind, sorted(TEMPLATE_KINDS)))
    asset_type, asset_class = TEMPLATE_KINDS[kind]
    calendar = optionality_calendar(now)
    is_crypto = kind == "crypto"

    leg: Dict[str, Any] = {
        # --- what this is. `decision` is the verb the guardrails read; `action`
        # and `side` are the execution spelling. All three, always.
        "decision": "BUY",
        "action": "buy",
        "side": "buy",
        "asset_class": asset_class,
        "asset_type": asset_type,
        "position_type": position_type,
        "classification": "<a classification from DISCOVERY_POLICY.md that supports a purchase>",
        "ticker": "<TICKER, or a hyphenated crypto pair such as BTC-USD>",
        "symbol": "<same as ticker>",
        "security_name": "<full name>",
        "exchange": "<NASDAQ / NYSE / NYSEARCA; omit for crypto>",

        # --- the money and the clock
        "proposed_amount_usd": "<dollars, 2dp — a choice, never a default>",
        "monthly_budget_before_usd": "<authorization before this leg, 2dp>",
        "monthly_budget_after_usd": "<authorization after this leg, 2dp>",
        "month": "<YYYY-MM>",
        "investment_horizon_months": 36,
        "current_price_usd": "<refreshed quote, from get_equity_quotes or get_crypto_quotes>",
        "quote_timestamp": "<ISO-8601 UTC of that quote>",
        "fractional_eligible": True,
        "confidence": "<LOW | MEDIUM | HIGH>",

        # --- the reasoning the policy requires, one key per question
        "thesis": _FILL,
        "value_creation": _FILL,
        "valuation_reasoning": _FILL,
        "timing_reason": _FILL,
        "why_not_wait": _FILL,
        "margin_for_error": _FILL,
        "portfolio_exposure": _FILL,
        "risks": _FILL,
        "invalidation": _FILL,

        "alternatives_considered": [
            {"symbol": "<TICKER>", "provenance": "<MY_WATCHLIST:<list> | CURRENT_HOLDING | OPEN_DISCOVERY>",
             "classification": "<label from DISCOVERY_POLICY.md>", "why_not": _FILL},
            {"symbol": "<TICKER>", "provenance": "<provenance>",
             "classification": "<label>", "why_not": _FILL},
        ],

        # --- the month as an option, with the two numbers taken from
        # `python3 scripts/emit_buy_scaffold.py`, never computed by hand
        "monthly_optionality": {
            "days_remaining_in_month": (
                calendar["days_remaining_in_month"] if calendar_values
                else CALENDAR_PLACEHOLDER),
            "tradable_sessions_remaining": (
                calendar["tradable_sessions_remaining"] if calendar_values
                else CALENDAR_PLACEHOLDER),
            "known_events_this_month": _FILL,
            "candidates_being_watched": _FILL,
            "probability_ranking_changes": _FILL,
            "dry_powder_benefit": _FILL,
            "partial_deployment_considered": _FILL,
        },

        "portfolio_sprawl_assessment": {
            "position_count": _FILL,
            "smallest_positions": _FILL,
            "economic_significance_of_this_add": _FILL,
            "overlap_analysis": _FILL,
            "concentration_alternative": _FILL,
        },

        "theme": {
            "primary": "<broad theme from the THEME_TAXONOMY in src/models.py>",
            "subtheme": "<subtheme that rolls up to it>",
            "scope_caveat": "<what this position does NOT cover>",
        },

        "events_considered": [
            {"label": _FILL,
             "date": "<YYYY-MM-DD>",
             "asset_class": asset_class,
             "session_timing": "<before the open | intraday | after the close>",
             "occurs_after_close": False,
             "status": "<RESOLVED | PENDING>",
             "actionable_with_this_month_authorization": True,
             "note": _FILL},
        ],

        "evidence": [
            {"tool": "get_equity_quotes", "source_type": "MARKET_DATA",
             "component": "Quote and valuation", "detail": _FILL},
            {"tool": "get_financials", "source_type": "OFFICIAL_FINANCIALS",
             "component": "Financial statements", "detail": _FILL},
            {"tool": "get_sec_filing_facts", "source_type": "SEC_FILING",
             "component": "Primary filing", "detail": _FILL},
        ],
    }

    if is_crypto:
        leg.pop("exchange", None)
        leg["fractional_eligible"] = True
        leg["evidence"] = [
            {"tool": "get_crypto_quotes", "source_type": "MARKET_DATA",
             "component": "Quote and network value", "detail": _FILL},
            {"tool": "WebFetch", "source_type": "PROTOCOL_DOCUMENTATION",
             "component": "Supply schedule", "detail": _FILL},
            {"tool": "WebFetch", "source_type": "ONCHAIN_DATA",
             "component": "Network usage", "detail": _FILL},
        ]

    if position_type == "NEW_POSITION":
        leg["new_position_justification"] = {
            "incremental_expected_return": _FILL,
            "diversification_benefit": _FILL,
            "overlap_with_existing": _FILL,
            "diversification_is_economic_not_cosmetic": _FILL,
            "best_existing_alternative": {
                "symbol": "<the specific existing holding this was weighed against>",
                "why_not": _FILL,
            },
            "why_new_position_beats_adding_to_existing": _FILL,
            "initial_size_significance": _FILL,
        }
        leg["research_package"] = _research_package(asset_type)
    else:
        leg["cost_basis_analysis"] = {
            "average_cost_usd": "<current average cost, 2dp>",
            "current_price_usd": "<refreshed quote, 2dp>",
            "raises_or_lowers_average": "<RAISES | LOWERS>",
            "economic_meaning": _FILL,
        }

    leg["authorization_expiry_acknowledged"] = (
        "<required only when a weighed event falls past this month's final "
        "tradable opportunity (%s); say plainly that the authorization expires "
        "unused>" % calendar["final_equity_opportunity"])
    return leg


def recommendation_template(
    now: Optional[datetime] = None,
    kind: str = "equity",
    plan_type: str = "SINGLE_BUY",
    calendar_values: bool = True,
) -> Dict[str, Any]:
    """The whole ``reports/recommendations/<stamp>.json`` envelope."""
    leg = buy_leg_template(now, kind=kind, calendar_values=calendar_values)
    legs: List[Dict[str, Any]] = [leg]
    if plan_type == "SPLIT_BUY_PLAN":
        sibling = buy_leg_template(now, kind=kind, calendar_values=calendar_values)
        for each in (leg, sibling):
            each["allocation_rationale"] = {
                "why_better_than_other_legs": _FILL,
                "why_this_amount": _FILL,
                "legs_compared_against": "<sibling leg tickers, named>",
            }
        legs = [leg, sibling]
    return {
        "schema_version": 1,
        "generated_at": "<ISO-8601 UTC>",
        "source_report": "reports/<YYYY-MM-DD_HHMM>.md",
        "plan_type": plan_type,
        "legs": legs,
    }


def render(payload: Dict[str, Any]) -> str:
    """The template as the JSON block a prompt embeds."""
    return json.dumps(payload, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# The worked example
# --------------------------------------------------------------------------
#
# The template above carries the *keys*. This carries the keys **and** values
# that satisfy every guardrail, so `tests/test_decision_schema.py` can assert
# something much stronger than "the shape looks right": that a payload built
# from this contract passes `guardrails.validate` with zero violations. The two
# are held to the same key structure by :func:`key_shape`, so the example cannot
# quietly grow a field the template lacks.


def key_shape(value: Any) -> Any:
    """The nested key structure of a payload, with values discarded.

    A list becomes the shape of its first element, because the contract is what
    a leg's entries look like, not how many there are.
    """
    if isinstance(value, dict):
        return {key: key_shape(inner) for key, inner in sorted(value.items())}
    if isinstance(value, list):
        return [key_shape(value[0])] if value else []
    return None


#: Per-kind specifics an example needs to be a *valid* payload rather than a
#: plausible one: a real ticker, a theme that exists in the taxonomy, evidence
#: whose source types count as primary for that asset class.
_EXAMPLE_SPECIFICS = {
    "equity": {
        "classification": "FUNDAMENTALLY_SUPPORTED_MOMENTUM",
        "ticker": "SNDK",
        "security_name": "SanDisk Corporation",
        "exchange": "NASDAQ",
        "current_price_usd": "1609.19",
        "theme": {
            "primary": "storage_memory",
            "subtheme": "nand_flash_storage",
            "scope_caveat": ("NAND flash storage only. This does not provide DRAM "
                             "or HBM memory exposure, and it does not address "
                             "semiconductor equipment."),
        },
        "evidence": [
            {"tool": "get_equity_quotes", "source_type": "MARKET_DATA",
             "component": "Quote and valuation",
             "detail": "Last $1,609.19; market capitalisation $235.41B."},
            {"tool": "get_financials", "source_type": "OFFICIAL_FINANCIALS",
             "component": "Financial statements",
             "detail": "FY revenue $20,248M, gross profit $14,472M, net income $11,433M."},
            {"tool": "get_sec_filing_facts", "source_type": "SEC_FILING",
             "component": "Primary filing",
             "detail": "10-K: long-term debt reduced to zero; cash $4,762M."},
        ],
        "research_package": {
            "current_quote_and_valuation": "Last $1,609.19; 20.61x trailing earnings, 14.10x book.",
            "recent_quarterly_results": "FQ4 revenue $6,231M, net income $4,797M, 77.00% net margin.",
            "multi_quarter_trends": "Net margin progression 4.85%, 26.55%, 60.76%, 77.00% across FY2026.",
            "balance_sheet": "Assets $22,507M, liabilities $6,771M, cash $4,762M, long-term debt zero.",
            "cash_flow": "Operating cash flow $11,671M against $11,433M of net income.",
            "earnings_history": "FY2024 -$672M, FY2025 -$1,641M, FY2026 +$11,433M.",
            "material_news": "Reported in early talks with a foundry partner for US-based capacity.",
            "primary_filings": "FY2026 10-K reviewed: revenue, segment detail and remaining obligations.",
            "competitive_position": "Three-way NAND oligopoly; the installed base is the moat.",
            "major_risks": "Undisclosed contract floors, cycle-peak margins, geographic concentration.",
            "upcoming_events": "Next earnings report falls outside this month's reach.",
            "bull_case": "Contracted minimum revenue collateralised by customer deposits.",
            "bear_case": "The floor prices are undisclosed and the margin is a visible cycle peak.",
        },
        "best_existing_alternative": {
            "symbol": "MU",
            "why_not": ("DRAM and HBM weighted, more expensive on trailing earnings, "
                        "and its resolving event falls outside this month's final "
                        "tradable session."),
        },
        "cost_basis": ("926.56", "RAISES"),
    },
    "etf": {
        "classification": "PROMISING",
        "ticker": "VTI",
        "security_name": "Vanguard Total Stock Market ETF",
        "exchange": "NYSEARCA",
        "current_price_usd": "300.00",
        "theme": {
            "primary": "diversified_index",
            "subtheme": "broad_us_equity",
            "scope_caveat": ("Broad US equity only. This does not address "
                             "international equity, small-cap tilt, or fixed income."),
        },
        "evidence": [
            {"tool": "get_equity_quotes", "source_type": "MARKET_DATA",
             "component": "Quote and valuation",
             "detail": "Last $300.00; index price-earnings 27.4."},
            {"tool": "get_equity_fundamentals", "source_type": "OFFICIAL_FINANCIALS",
             "component": "Fund fundamentals",
             "detail": "Expense ratio 0.03%; assets under management in the hundreds of billions."},
            {"tool": "get_financials", "source_type": "OFFICIAL_FINANCIALS",
             "component": "Underlying index financials",
             "detail": "Aggregate earnings and payout data for the tracked index."},
        ],
        "research_package": {
            "current_quote_and_valuation": "Last $300.00; index price-earnings 27.4.",
            "index_and_methodology": "Market-cap weighted, full US total market.",
            "expense_ratio": "0.03% annually.",
            "holdings_concentration": "The top ten holdings are roughly a third of assets.",
            "overlap_with_existing_holdings": "Substantial overlap with the index funds already held.",
            "liquidity_and_spread": "Multi-million share average volume; a one-cent spread.",
            "fund_size_and_age": "Hundreds of billions in assets and two decades of history.",
            "major_risks": "Broad equity drawdown risk and mega-cap concentration.",
            "bull_case": "The lowest-cost diversified access to US equity returns.",
            "bear_case": "It adds no exposure the portfolio does not already have.",
        },
        "best_existing_alternative": {
            "symbol": "VOO",
            "why_not": ("Already held and almost entirely overlapping, so the "
                        "incremental diversification would be cosmetic."),
        },
        "cost_basis": ("240.00", "RAISES"),
    },
    "crypto": {
        "classification": "PROMISING",
        "ticker": "BTC-USD",
        "security_name": "Bitcoin",
        "exchange": None,
        "current_price_usd": "80000.00",
        "theme": {
            "primary": "crypto",
            "subtheme": "store_of_value_crypto",
            "scope_caveat": ("Store-of-value crypto only. This does not provide "
                             "smart-contract platform exposure and it does not "
                             "address crypto infrastructure equities."),
        },
        "evidence": [
            {"tool": "get_crypto_quotes", "source_type": "MARKET_DATA",
             "component": "Quote and network value",
             "detail": "Mark $80,000.00; roughly $1.6T network value."},
            {"tool": "WebFetch", "source_type": "PROTOCOL_DOCUMENTATION",
             "component": "Supply schedule",
             "detail": "Fixed 21M cap; issuance halves on a published schedule."},
            {"tool": "WebFetch", "source_type": "ONCHAIN_DATA",
             "component": "Network usage",
             "detail": "Settlement volume and active addresses, read from the chain."},
        ],
        "research_package": {
            "current_quote_and_market_cap": "Mark $80,000.00; roughly $1.6T network value.",
            "supply_and_issuance": "Fixed 21M cap; issuance halves on a known schedule.",
            "network_usage_and_adoption": "Settlement volume and active addresses both growing.",
            "ecosystem_and_development": "Steady protocol development and broad client diversity.",
            "security_and_decentralization": "The largest proof-of-work security budget; wide miner distribution.",
            "regulatory_risk": "Spot funds approved in the US; treatment elsewhere is uneven.",
            "competitive_networks": "No network contests the store-of-value role at similar scale.",
            "long_term_price_and_drawdown_history": "Four drawdowns exceeding 70% since inception.",
            "major_risks": ("Extreme volatility, regulatory reversal, and correlation "
                            "with risk assets. Drawdowns of 70-90% are a plausible "
                            "risk rather than a tail case, which makes 24 months a "
                            "short horizon for this asset class."),
            "bull_case": "A durable monetary premium with a fixed supply schedule.",
            "bear_case": "No cash flows; the whole valuation rests on continued adoption.",
        },
        "best_existing_alternative": {
            "symbol": "ETH-USD",
            "why_not": ("A different return driver entirely, and already the second "
                        "largest crypto exposure in the household book."),
        },
        "cost_basis": ("62000.00", "RAISES"),
    },
}

#: Substantial generic prose for any narrative key an example does not name
#: explicitly. Long enough to clear the substance checks; the point of the
#: example is the *keys*, and a run replaces all of this.
_EXAMPLE_PROSE = (
    "Stated explicitly here because the guardrails require this question to be "
    "answered rather than assumed, and because a reader six months from now "
    "cannot reconstruct the reasoning from the numbers alone.")


def _fill_placeholders(value: Any) -> Any:
    """Replace any remaining template placeholder with substantial prose."""
    if isinstance(value, dict):
        return {key: _fill_placeholders(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_fill_placeholders(item) for item in value]
    if isinstance(value, str) and (value == _FILL or value.startswith("<")):
        return _EXAMPLE_PROSE
    return value


def buy_leg_example(
    now: Optional[datetime] = None,
    kind: str = "equity",
    position_type: str = "NEW_POSITION",
    amount_usd: str = "10.00",
    budget_before_usd: str = "25.00",
    budget_after_usd: str = "15.00",
) -> Dict[str, Any]:
    """A filled-in leg with the template's exact key structure, valid to the guardrails.

    Built by *filling* the template rather than by writing a second payload, so
    the two cannot diverge: every key comes from :func:`buy_leg_template`, and
    anything this function does not name explicitly is filled generically.
    """
    leg = buy_leg_template(now, kind=kind, position_type=position_type)
    specifics = _EXAMPLE_SPECIFICS[kind]
    calendar = optionality_calendar(now)
    reference = now or datetime.utcnow()

    leg.update({
        "classification": specifics["classification"],
        "ticker": specifics["ticker"],
        "symbol": specifics["ticker"],
        "security_name": specifics["security_name"],
        "proposed_amount_usd": amount_usd,
        "monthly_budget_before_usd": budget_before_usd,
        "monthly_budget_after_usd": budget_after_usd,
        "month": "%04d-%02d" % (reference.year, reference.month),
        "current_price_usd": specifics["current_price_usd"],
        "quote_timestamp": reference.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "confidence": "MEDIUM",
        "theme": dict(specifics["theme"]),
        "evidence": [dict(item) for item in specifics["evidence"]],
    })
    if "exchange" in leg and specifics["exchange"]:
        leg["exchange"] = specifics["exchange"]

    leg.update({
        "thesis": (
            "Durable long-term exposure at an entry price that already offers an "
            "adequate expected return, held for the stated horizon rather than "
            "traded around."),
        "value_creation": (
            "Future value comes from volume growth against a fixed cost base and "
            "from cash returned to owners, not from the multiple re-rating."),
        "valuation_reasoning": (
            "Three cases were run against the current price. The punitive case "
            "still clears the hurdle, which is what makes the entry defensible "
            "rather than merely attractive."),
        "timing_reason": (
            "The conditions this process committed to waiting for have resolved, "
            "and the resolution is documented rather than asserted, so the reason "
            "to wait no longer exists on its own terms."),
        "why_not_wait": (
            "This rests on expected return at today's entry price rather than on "
            "the absence of upcoming information. Waiting would require "
            "forecasting a better price, which this process will not do."),
        "margin_for_error": (
            "The gap between the current multiple and the punitive downside case. "
            "The thesis can be wrong by that whole distance and the entry price "
            "still works."),
        "portfolio_exposure": (
            "This subtheme is a low single-digit percentage of the book today and "
            "remains so after the purchase."),
        "risks": (
            "A disclosure gap on the key contracted terms, a visible cycle peak in "
            "margins, and concentrated geographic revenue. Any of the three could "
            "compress earnings materially."),
        "invalidation": (
            "Any renegotiation or walk-back of the contracted commitments would end "
            "the thesis, because the downside case rests entirely on them."),
    })

    leg["alternatives_considered"] = [
        {"symbol": "GOOGL", "provenance": "CURRENT_HOLDING", "classification": "WATCH",
         "why_not": "Already the largest non-index holding in a saturated theme."},
        {"symbol": "ITOT", "provenance": "OPEN_DISCOVERY", "classification": "TOO_EXTENDED",
         "why_not": "Equivalent exposure to what is already held, with no edge."},
    ]

    leg["monthly_optionality"].update({
        "days_remaining_in_month": calendar["days_remaining_in_month"],
        "tradable_sessions_remaining": calendar["tradable_sessions_remaining"],
        "known_events_this_month": (
            "One holding reports after the close on the month's final session, so "
            "its reaction is first tradable next month."),
        "candidates_being_watched": (
            "Two watchlist names remain in research and neither is close enough to "
            "overtake this candidate on the data available today."),
        "probability_ranking_changes": (
            "Low. The nearest alternative was rejected on a checkable defect rather "
            "than on enthusiasm, and nothing dated resolves that defect this month."),
        "dry_powder_benefit": (
            "Preserving the balance would allow a response to the late-month print, "
            "which affects a holding this decision does not touch."),
        "partial_deployment_considered": (
            "A smaller tranche was considered and rejected: below this size the "
            "position would be too small to matter against the existing book."),
    })

    leg["portfolio_sprawl_assessment"].update({
        "position_count": "20 equity lines and 9 crypto positions today.",
        "smallest_positions": "Five equity lines sit under $7.00.",
        "economic_significance_of_this_add": (
            "At this size the position ranks 14th of 21 lines and is a 1.21% "
            "household weight, ahead of seven existing positions."),
        "overlap_analysis": (
            "The dominant cluster is already 42.96% of the book and rises to 43.64% "
            "after this purchase; the overlap is stated rather than denied."),
        "concentration_alternative": (
            "Adding to the strongest existing holding was compared directly and "
            "rejected on concentration and correlation grounds."),
    })

    leg["events_considered"] = [
        {"label": "Federal Open Market Committee decision and projections",
         "date": reference.strftime("%Y-%m-%d"),
         "asset_class": leg["asset_class"],
         "session_timing": "intraday",
         "occurs_after_close": False,
         "status": "RESOLVED",
         "actionable_with_this_month_authorization": True,
         "note": ("Tradable opportunities remain in the month after this event, so "
                  "this month's authorization can and does act on it.")},
    ]

    if position_type == "NEW_POSITION":
        leg["new_position_justification"].update({
            "incremental_expected_return": (
                "A materially lower multiple on comparable forward earnings than the "
                "best existing alternative offers."),
            "diversification_benefit": (
                "The household holds almost none of this specific exposure; the "
                "nearest existing position sits in an adjacent subtheme."),
            "overlap_with_existing": (
                "High at the sector level and low at the subtheme level, and the "
                "sector figure is stated above rather than minimised."),
            "diversification_is_economic_not_cosmetic": (
                "The new return driver is genuinely different from the one that "
                "explains most of the book. A missing sector would not on its own "
                "have justified this purchase."),
            "best_existing_alternative": dict(specifics["best_existing_alternative"]),
            "why_new_position_beats_adding_to_existing": (
                "The existing alternative is correlated with the rest of the book and "
                "its own catalyst cannot be acted on with this month's authorization."),
            "initial_size_significance": (
                "A 1.21% household weight, larger than seven existing positions, so it "
                "is economically meaningful from the first dollar."),
        })
        leg["research_package"] = dict(specifics["research_package"])
    else:
        average, direction = specifics["cost_basis"]
        leg["cost_basis_analysis"] = {
            "average_cost_usd": average,
            "current_price_usd": specifics["current_price_usd"],
            "raises_or_lowers_average": direction,
            "economic_meaning": (
                "A rising average cost is not itself a cost; what matters is the "
                "expected forward return from today's price."),
        }

    leg["authorization_expiry_acknowledged"] = (
        "One weighed event falls past this month's final tradable opportunity of "
        "%s. That is stated as a choice: this month's authorization is being "
        "deployed now rather than held for it." % calendar["final_equity_opportunity"])

    return _fill_placeholders(leg)


# --------------------------------------------------------------------------
# The block the prompts embed
# --------------------------------------------------------------------------
#
# Both `prompts/scheduled_evaluation.md` and `prompts/evaluate_market.md` carry
# this block between the markers below, generated rather than hand-maintained.
# `tests/test_decision_schema.py` asserts both copies still match what this
# module renders, so the two prompts cannot drift from each other or from the
# guardrails again.

PROMPT_BLOCK_BEGIN = "<!-- BEGIN CANONICAL_BUY_PAYLOAD -->"
PROMPT_BLOCK_END = "<!-- END CANONICAL_BUY_PAYLOAD -->"

#: Prompts carrying the generated block.
PROMPT_FILES = ("prompts/scheduled_evaluation.md", "prompts/evaluate_market.md")


def prompt_block() -> str:
    """The generated contract, marker to marker, as it appears in a prompt."""
    body = render(recommendation_template(calendar_values=False))
    return "\n".join([
        PROMPT_BLOCK_BEGIN,
        "<!-- Generated by scripts/emit_buy_scaffold.py from src/decision_schema.py.",
        "     Do not edit by hand: run `python3 scripts/emit_buy_scaffold.py --sync-prompts`. -->",
        "",
        "```json",
        body,
        "```",
        "",
        "Every key above is required and spelled exactly as the guardrails read",
        "it. Replace the placeholder values; keep the keys. `days_remaining_in_month`",
        "and `tradable_sessions_remaining` are taken from",
        "`python3 scripts/emit_buy_scaffold.py` and never computed by hand — they",
        "are checked against the project's exchange calendar and a hand-computed",
        "value earns OPTIONALITY_MISREPORTED.",
        "",
        "For an EXISTING_POSITION, drop `new_position_justification` and",
        "`research_package` and carry `cost_basis_analysis` instead. For crypto,",
        "run the command with `--kind crypto`: the research components differ.",
        PROMPT_BLOCK_END,
    ])


def extract_prompt_block(text: str) -> Optional[str]:
    """The marked block inside a prompt, or None when it carries none."""
    start = (text or "").find(PROMPT_BLOCK_BEGIN)
    end = (text or "").find(PROMPT_BLOCK_END)
    if start < 0 or end < 0 or end < start:
        return None
    return text[start:end + len(PROMPT_BLOCK_END)]


def extract_prompt_payload(text: str) -> Optional[Dict[str, Any]]:
    """The JSON contract inside a prompt's marked block, parsed."""
    block = extract_prompt_block(text)
    if block is None:
        return None
    opening = block.find("```json")
    closing = block.find("```", opening + 7)
    if opening < 0 or closing < 0:
        return None
    try:
        return json.loads(block[opening + 7:closing])
    except ValueError:
        return None
