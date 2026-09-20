"""Deterministic guardrail tests.

Every test here asserts that the *code* refuses something, independent of any
model reasoning.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import guardrails  # noqa: E402
from src.guardrails import LiveTradingDisabled, validate  # noqa: E402
from src.state import commit_purchase, load_config  # noqa: E402
from tests.helpers import (
    legacy_wait_decision,  # noqa: E402
    add_to_existing,
    buy_decision,
    crypto_decision,
    days_remaining_in_month,
    make_config,
    make_crypto_universe,
    make_state,
    new_position_justification,
    research_package,
    wait_decision,
)


class BudgetLimitTests(unittest.TestCase):
    def test_full_budget_purchase_from_untouched_month_passes(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "25.00"), config, state)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertIn("monthly_budget_not_exceeded", result.checks_run)

    def test_one_cent_over_budget_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "25.01"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_MONTHLY_BUDGET", result.violation_codes)

    def test_two_purchases_totalling_exactly_the_budget_pass(self):
        config = make_config()
        state = make_state(config)

        first = buy_decision(state, "10.00", decision_id="d1")
        result_one = validate(first, config, state)
        self.assertTrue(result_one.valid, result_one.violation_codes)

        state = commit_purchase(state, "d1", Decimal("10.00"))
        self.assertEqual(state.remaining_usd, Decimal("15.00"))

        second = buy_decision(state, "15.00", decision_id="d2")
        result_two = validate(second, config, state)
        self.assertTrue(result_two.valid, result_two.violation_codes)

        state = commit_purchase(state, "d2", Decimal("15.00"))
        self.assertEqual(state.committed_usd, Decimal("25.00"))
        self.assertEqual(state.remaining_usd, Decimal("0.00"))

    def test_cumulative_purchases_exceeding_the_budget_by_a_cent_fail(self):
        config = make_config()
        state = make_state(config, committed="25.00", acted=["d1"])
        result = validate(buy_decision(state, "0.01", decision_id="d2"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_REMAINING_BUDGET", result.violation_codes)
        self.assertIn("EXCEEDS_CUMULATIVE_MONTHLY_BUDGET", result.violation_codes)

    def test_cumulative_walk_of_24_99_then_two_cents_fails(self):
        config = make_config()
        state = make_state(config, committed="24.99", acted=["d1"])
        result = validate(buy_decision(state, "0.02", decision_id="d2"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_CUMULATIVE_MONTHLY_BUDGET", result.violation_codes)

    def test_cash_in_the_account_is_not_authorization(self):
        """A huge cash balance is irrelevant: the cap is the monthly budget."""
        config = make_config()
        state = make_state(config)
        decision = buy_decision(
            state,
            "500.00",
            buying_power_usd="10000.00",
            thesis="There is plenty of cash available.",
        )
        result = validate(decision, config, state)
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_MONTHLY_BUDGET", result.violation_codes)

    def test_misreported_budget_arithmetic_fails(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(state, "10.00", monthly_budget_after_usd="25.00")
        result = validate(decision, config, state)
        self.assertFalse(result.valid)
        self.assertIn("BUDGET_MISREPORTED", result.violation_codes)


class AmountTests(unittest.TestCase):
    def test_zero_amount_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "0.00"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("ZERO_AMOUNT", result.violation_codes)

    def test_negative_amount_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "-5.00"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("NEGATIVE_AMOUNT", result.violation_codes)

    def test_sub_cent_amount_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.005"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("SUB_CENT_AMOUNT", result.violation_codes)

    def test_unparseable_amount_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "0.00", proposed_amount_usd="a lot"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("MALFORMED_AMOUNT", result.violation_codes)

    def test_float_money_does_not_drift(self):
        """0.1 + 0.2 style drift must never produce a budget pass/fail flip."""
        config = make_config()
        state = make_state(config, committed="23.90")
        decision = buy_decision(state, "1.10", proposed_amount_usd=1.1)
        decision["monthly_budget_before_usd"] = "1.10"
        decision["monthly_budget_after_usd"] = "0.00"
        result = validate(decision, config, state)
        self.assertTrue(result.valid, result.violation_codes)


class ProhibitedActionTests(unittest.TestCase):
    def test_sell_side_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", side="sell"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("SELL_FORBIDDEN", result.violation_codes)

    def test_sell_action_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", action="sell"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("SELL_FORBIDDEN", result.violation_codes)

    def test_short_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", is_short=True), config, state)
        self.assertFalse(result.valid)
        self.assertIn("SHORT_FORBIDDEN", result.violation_codes)

    def test_short_side_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", side="sell_short"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("SHORT_FORBIDDEN", result.violation_codes)

    def test_option_asset_type_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", asset_type="option"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("OPTIONS_FORBIDDEN", result.violation_codes)

    def test_option_flag_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", uses_options=True), config, state)
        self.assertFalse(result.valid)
        self.assertIn("OPTIONS_FORBIDDEN", result.violation_codes)

    def test_option_contract_fields_fail_even_without_a_flag(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(state, "10.00", strike_price="400", expiration_date="2027-01-15")
        result = validate(decision, config, state)
        self.assertFalse(result.valid)
        self.assertIn("OPTIONS_FORBIDDEN", result.violation_codes)

    def test_margin_flag_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", uses_margin=True), config, state)
        self.assertFalse(result.valid)
        self.assertIn("MARGIN_FORBIDDEN", result.violation_codes)

    def test_margin_account_usage_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", account_usage="margin"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("MARGIN_FORBIDDEN", result.violation_codes)

    def test_crypto_fails_when_the_config_forbids_it(self):
        """Turning allow_crypto off must still hard-block crypto."""
        config = make_config(allow_crypto=False)
        state = make_state(config)
        result = validate(
            crypto_decision(state, "10.00"), config, state, crypto_universe=make_crypto_universe()
        )
        self.assertFalse(result.valid)
        self.assertIn("CRYPTO_FORBIDDEN", result.violation_codes)

    def test_a_crypto_pair_symbol_is_not_a_valid_equity_ticker(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(state, "10.00", ticker="ETH-USD", security_name="Ethereum")
        result = validate(decision, config, state)
        self.assertFalse(result.valid)
        self.assertIn("MALFORMED_TICKER", result.violation_codes)

    def test_asset_class_must_match_asset_type(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(state, "10.00", asset_type="crypto", asset_class="ETF")
        result = validate(decision, config, state, crypto_universe=make_crypto_universe())
        self.assertFalse(result.valid)
        self.assertIn("ASSET_CLASS_MISMATCH", result.violation_codes)

    def test_crypto_tracking_etp_is_allowed_but_warns_about_duplicate_exposure(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(state, "10.00", ticker="IBIT",
                                security_name="iShares Bitcoin Trust ETF")
        result = validate(decision, config, state)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertTrue(any("crypto-tracking ETP" in w for w in result.warnings))

    def test_transfer_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", is_transfer=True), config, state)
        self.assertFalse(result.valid)
        self.assertIn("TRANSFER_FORBIDDEN", result.violation_codes)

    def test_withdrawal_action_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", action="withdraw"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("TRANSFER_FORBIDDEN", result.violation_codes)

    def test_settings_change_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", action="account_upgrade"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("SETTINGS_CHANGE_FORBIDDEN", result.violation_codes)


class LeveragedAndInverseTests(unittest.TestCase):
    def test_known_leveraged_ticker_fails(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(state, "10.00", ticker="TQQQ", security_name="ProShares UltraPro QQQ")
        result = validate(decision, config, state)
        self.assertFalse(result.valid)
        self.assertIn("LEVERAGED_PRODUCT_FORBIDDEN", result.violation_codes)

    def test_leveraged_name_pattern_fails_for_an_unknown_ticker(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(
            state, "10.00", ticker="ZZZL",
            security_name="Direxion Daily Widget Bull 3X Shares",
        )
        result = validate(decision, config, state)
        self.assertFalse(result.valid)
        self.assertIn("LEVERAGED_PRODUCT_FORBIDDEN", result.violation_codes)

    def test_inverse_name_pattern_fails(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(
            state, "10.00", ticker="ZZZI",
            security_name="Acme Inverse Widget Index Fund",
        )
        result = validate(decision, config, state)
        self.assertFalse(result.valid)
        self.assertIn("INVERSE_PRODUCT_FORBIDDEN", result.violation_codes)

    def test_explicit_leveraged_flag_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", is_leveraged=True), config, state)
        self.assertFalse(result.valid)
        self.assertIn("LEVERAGED_PRODUCT_FORBIDDEN", result.violation_codes)

    def test_explicit_inverse_flag_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", is_inverse=True), config, state)
        self.assertFalse(result.valid)
        self.assertIn("INVERSE_PRODUCT_FORBIDDEN", result.violation_codes)

    def test_ordinary_index_fund_name_is_not_flagged(self):
        config = make_config()
        state = make_state(config)
        for name, ticker in (
            ("Vanguard Total Stock Market ETF", "VTI"),
            ("iShares Core S&P 500 ETF", "IVV"),
            ("SPDR S&P 500 ETF Trust", "SPY"),
            ("Schwab U.S. Dividend Equity ETF", "SCHD"),
            ("Vanguard S&P 500 ETF", "VOO"),
        ):
            with self.subTest(ticker=ticker):
                decision = buy_decision(state, "10.00", ticker=ticker, security_name=name)
                result = validate(decision, config, state)
                self.assertTrue(result.valid, (ticker, result.violation_codes))


class AssetEligibilityTests(unittest.TestCase):
    def test_missing_ticker_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", ticker=""), config, state)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_TICKER", result.violation_codes)

    def test_malformed_ticker_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", ticker="NOT A TICKER"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("MALFORMED_TICKER", result.violation_codes)

    def test_otc_flag_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", is_otc=True), config, state)
        self.assertFalse(result.valid)
        self.assertIn("OTC_FORBIDDEN", result.violation_codes)

    def test_non_major_exchange_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", exchange="OTCMKTS"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("OTC_FORBIDDEN", result.violation_codes)

    def test_penny_stock_fails(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(
            state, "10.00", ticker="PNNY", asset_type="us_common_stock", asset_class="EQUITY",
            security_name="Penny Example Corp", current_price_usd="0.42", exchange="NASDAQ",
        )
        result = validate(decision, config, state)
        self.assertFalse(result.valid)
        self.assertIn("PENNY_STOCK_FORBIDDEN", result.violation_codes)

    def test_low_price_produces_a_warning_not_a_violation(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(
            state, "10.00", ticker="LOWP", asset_type="us_common_stock", asset_class="EQUITY",
            security_name="Low Price Corp", current_price_usd="3.50", exchange="NASDAQ",
            research_package=research_package("equity"),
        )
        result = validate(decision, config, state)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertTrue(any("under $5" in w or "under 5.00" in w for w in result.warnings))

    def test_disallowed_asset_type_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", asset_type="mutual_fund"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("ASSET_TYPE_NOT_ALLOWED", result.violation_codes)

    def test_non_fractional_asset_priced_above_the_budget_fails(self):
        config = make_config()
        state = make_state(config)
        decision = buy_decision(
            state, "25.00", ticker="EXPN", asset_type="us_common_stock", asset_class="EQUITY",
            security_name="Expensive Corp", current_price_usd="900.00",
            fractional_eligible=False,
        )
        result = validate(decision, config, state)
        self.assertFalse(result.valid)
        self.assertIn("FRACTIONAL_NOT_ELIGIBLE", result.violation_codes)


class DecisionShapeTests(unittest.TestCase):
    def test_a_decision_that_is_not_buy_or_wait_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", decision="ACCUMULATE"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("INVALID_DECISION", result.violation_codes)

    def test_a_buy_not_explicitly_marked_buy_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", side="unknown"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("NOT_EXPLICIT_BUY", result.violation_codes)

    def test_missing_decision_id_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", decision_id=""), config, state)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_DECISION_ID", result.violation_codes)

    def test_duplicate_decision_id_fails(self):
        config = make_config()
        state = make_state(config, acted=["already-done"])
        result = validate(buy_decision(state, "10.00", decision_id="already-done"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("DUPLICATE_DECISION_ID", result.violation_codes)

    def test_missing_narrative_fields_fail(self):
        config = make_config()
        state = make_state(config)
        for field in ("thesis", "timing_reason", "alternatives_considered", "risks", "evidence"):
            with self.subTest(field=field):
                result = validate(buy_decision(state, "10.00", **{field: ""}), config, state)
                self.assertFalse(result.valid)
                self.assertIn("MISSING_REQUIRED_FIELD", result.violation_codes)

    def test_bad_confidence_value_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "10.00", confidence="VERY HIGH"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("INVALID_CONFIDENCE", result.violation_codes)

    def test_non_dict_proposal_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(["BUY", "VTI"], config, state)
        self.assertFalse(result.valid)
        self.assertIn("MALFORMED_PROPOSAL", result.violation_codes)

    def test_valid_wait_passes(self):
        config = make_config()
        state = make_state(config)
        result = validate(wait_decision(state), config, state)
        self.assertTrue(result.valid, result.violation_codes)

    def test_wait_with_a_dollar_amount_fails(self):
        config = make_config()
        state = make_state(config)
        result = validate(wait_decision(state, proposed_amount_usd="10.00"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("WAIT_WITH_AMOUNT", result.violation_codes)

    def test_wait_misreporting_the_budget_fails(self):
        config = make_config()
        state = make_state(config, committed="5.00")
        result = validate(wait_decision(state, monthly_budget_remaining_usd="25.00"), config, state)
        self.assertFalse(result.valid)
        self.assertIn("BUDGET_MISREPORTED", result.violation_codes)


class LiveTradingTests(unittest.TestCase):
    def test_shipped_config_has_live_trading_disabled(self):
        config = load_config()
        self.assertFalse(config.live_trading, "config.json must ship with live_trading=false")

    def test_validation_never_marks_anything_executable(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "25.00"), config, state)
        self.assertTrue(result.valid)
        self.assertFalse(result.executable)
        self.assertEqual(result.execution_status, "DRY_RUN_NOT_EXECUTED")

    def test_arming_execution_does_not_invalidate_a_sound_decision(self):
        """The contradiction that made the documented live path impossible.

        The policy fingerprint forces arm-then-approve, and approve_decision.py
        re-validates through validate(). While validate() emitted
        LIVE_TRADING_NOT_PERMITTED on an armed config, arming was guaranteed to
        invalidate the decision the operator was about to approve, so the
        documented sequence could never complete.

        Investment validity and execution readiness are separate questions.
        The switches are enforced by src.execution.execution_gate_blockers().
        """
        state = make_state()
        decision = buy_decision(state, "25.00")
        for mode, agent, live in (("DRY_RUN", False, False),
                                  ("APPROVAL_REQUIRED", True, True)):
            with self.subTest(execution_mode=mode, live_trading=live):
                config = make_config(execution_mode=mode, agent_enabled=agent,
                                     live_trading=live)
                result = validate(decision, config, make_state(config))
                self.assertTrue(result.valid, result.violation_codes)
                self.assertNotIn("LIVE_TRADING_NOT_PERMITTED", result.violation_codes)
                self.assertFalse(result.executable, "validation never authorizes")

    def test_an_armed_config_is_warned_about_but_not_fatal(self):
        config = make_config(execution_mode="APPROVAL_REQUIRED",
                             agent_enabled=True, live_trading=True)
        state = make_state(config)
        result = validate(buy_decision(state, "25.00"), config, state)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertTrue(any("execution is armed" in w for w in result.warnings),
                        result.warnings)

    def test_the_separation_of_layers_is_recorded_as_a_check(self):
        config = make_config()
        state = make_state(config)
        result = validate(buy_decision(state, "25.00"), config, state)
        self.assertIn("execution_arming_is_not_an_investment_concern",
                      result.checks_run)

    def test_execution_gate_always_raises(self):
        for live in (False, True):
            with self.subTest(live_trading=live):
                with self.assertRaises(LiveTradingDisabled):
                    guardrails.assert_execution_allowed(make_config(live_trading=live))

    def test_no_module_in_src_can_reach_the_network_or_a_subprocess(self):
        """Strongest form of 'there is no order path': an AST check.

        A Python module in this repo cannot call an MCP tool directly -- MCP
        tools are not importable. The only way src/ could ever reach the broker
        is via the network or a subprocess, so those imports are banned outright.
        """
        import ast
        import src

        banned_modules = {
            "requests", "httpx", "urllib", "urllib.request", "socket", "http",
            "http.client", "subprocess", "ftplib", "telnetlib", "asyncio",
            "aiohttp", "websockets", "xmlrpc", "smtplib",
        }
        package_dir = os.path.dirname(os.path.abspath(src.__file__))
        for filename in sorted(os.listdir(package_dir)):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(package_dir, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        self.assertNotIn(
                            root, banned_modules,
                            "%s imports %s; src/ must not be able to reach the network"
                            % (filename, alias.name),
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    root = node.module.split(".")[0]
                    self.assertNotIn(
                        root, banned_modules,
                        "%s imports from %s; src/ must not be able to reach the network"
                        % (filename, node.module),
                    )

    def test_no_order_tool_is_ever_in_a_callable_position(self):
        """Order tool names may be documented, never invoked."""
        import ast
        import src

        banned = {
            "place_equity_order", "place_option_order", "place_crypto_order",
            "review_equity_order", "review_option_order", "preview_crypto_order",
            "cancel_equity_order", "cancel_option_order", "cancel_crypto_order",
            "exercise_option", "submit_order",
        }
        package_dir = os.path.dirname(os.path.abspath(src.__file__))
        for filename in sorted(os.listdir(package_dir)):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(package_dir, filename)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename)
            for node in ast.walk(tree):
                # any call, attribute access, or name binding of an order tool
                if isinstance(node, ast.Call):
                    func = node.func
                    name = getattr(func, "id", None) or getattr(func, "attr", None)
                    self.assertNotIn(name, banned, "%s calls %s" % (filename, name))
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, banned, "%s accesses .%s" % (filename, node.attr))
                elif isinstance(node, ast.Name):
                    self.assertNotIn(node.id, banned, "%s binds %s" % (filename, node.id))

    # Two modules may name an order tool, for opposite-of-dangerous reasons:
    #
    #   execution.py  documents what a future adapter would send;
    #   scheduling.py lists them so it can assert they stay DENIED during an
    #                 unattended report-only run.
    #
    # Neither may ever *call* one — that is enforced separately, and far more
    # strictly, by test_no_module_calls_an_order_tool above, which walks the AST
    # of every module and rejects any call, attribute access or name binding.
    MODULES_THAT_MAY_NAME_ORDER_TOOLS = ("execution.py", "scheduling.py")

    def test_order_tool_names_appear_only_where_they_are_documented_or_denied(self):
        import src

        banned = ("place_equity_order", "place_crypto_order", "place_option_order",
                  "review_equity_order", "preview_crypto_order", "submit_order")
        package_dir = os.path.dirname(os.path.abspath(src.__file__))
        for filename in sorted(os.listdir(package_dir)):
            if not filename.endswith(".py"):
                continue
            with open(os.path.join(package_dir, filename), encoding="utf-8") as handle:
                content = handle.read()
            for needle in banned:
                if needle in content:
                    self.assertIn(
                        filename, self.MODULES_THAT_MAY_NAME_ORDER_TOOLS,
                        "%s mentions %s; only %s may name order tools"
                        % (filename, needle,
                           " or ".join(self.MODULES_THAT_MAY_NAME_ORDER_TOOLS)),
                    )

    def test_scheduling_names_order_tools_only_inside_its_denial_lists(self):
        """scheduling.py's mentions must be inert data, never anything callable."""
        from src import scheduling

        # Every order tool it names is in the denylist it enforces...
        for tool in ("place_equity_order", "place_crypto_order", "place_option_order",
                     "review_equity_order", "preview_crypto_order"):
            self.assertIn(tool, scheduling.ORDER_TOOLS)
            self.assertIn(tool, scheduling.FORBIDDEN_TOOLS)

        # ...and they are plain strings, so there is nothing to invoke.
        for tool in scheduling.FORBIDDEN_TOOLS:
            self.assertIsInstance(tool, str)
            self.assertFalse(hasattr(scheduling, tool),
                             "scheduling.%s must not be a module attribute" % tool)

    def test_scheduling_module_imports_no_network_or_subprocess(self):
        """The AST rule that governs src/ applies to the new module too."""
        import ast

        path = os.path.join(os.path.dirname(os.path.abspath(
            __import__("src").__file__)), "scheduling.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), "scheduling.py")
        forbidden = {"socket", "subprocess", "http", "urllib", "requests",
                     "httpx", "aiohttp", "ftplib", "telnetlib", "asyncio"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], forbidden)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], forbidden)

    def test_execute_always_raises(self):
        from src.execution import ExecutionDisabled, execute

        with self.assertRaises(ExecutionDisabled):
            execute()


class CryptoTests(unittest.TestCase):
    """Crypto is allowed by an explicit allow-list, not by a missing rule."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)
        self.universe = make_crypto_universe()

    def check(self, decision, universe=None):
        return validate(
            decision, self.config, self.state,
            crypto_universe=universe if universe is not None else self.universe,
        )

    # --- the happy path -------------------------------------------------
    def test_supported_crypto_within_budget_passes(self):
        result = self.check(crypto_decision(self.state, "25.00"))
        self.assertTrue(result.valid, result.violation_codes)
        self.assertIn("crypto_pair_supported_by_robinhood", result.checks_run)
        self.assertIn("crypto_pair_not_halted", result.checks_run)
        self.assertIn("crypto_min_order_size", result.checks_run)

    def test_a_crypto_buy_runs_the_crypto_path_not_the_equity_path(self):
        result = self.check(crypto_decision(self.state, "5.00"))
        self.assertTrue(result.valid, result.violation_codes)
        self.assertNotIn("penny_stock_rejected", result.checks_run)
        self.assertNotIn("major_us_exchange", result.checks_run)

    # --- allow-list enforcement ------------------------------------------
    def test_unsupported_crypto_symbol_fails(self):
        result = self.check(
            crypto_decision(self.state, "10.00", ticker="MOONCOIN-USD",
                            security_name="Mooncoin", current_price_usd="0.10")
        )
        self.assertFalse(result.valid)
        self.assertIn("CRYPTO_PAIR_NOT_SUPPORTED", result.violation_codes)

    def test_halted_crypto_pair_fails(self):
        result = self.check(
            crypto_decision(self.state, "10.00", ticker="TRUMP-USD",
                            security_name="OFFICIAL TRUMP", current_price_usd="2.30")
        )
        self.assertFalse(result.valid)
        self.assertIn("CRYPTO_PAIR_HALTED", result.violation_codes)

    def test_pair_untradable_in_an_individual_account_fails(self):
        result = self.check(
            crypto_decision(self.state, "10.00", ticker="BILL-USD",
                            security_name="Billions Network", current_price_usd="1.00")
        )
        self.assertFalse(result.valid)
        self.assertIn("CRYPTO_PAIR_NOT_TRADABLE", result.violation_codes)

    def test_display_only_pair_fails(self):
        result = self.check(
            crypto_decision(self.state, "10.00", ticker="GRAM-USD",
                            security_name="Gram", current_price_usd="1.00")
        )
        self.assertFalse(result.valid)
        self.assertIn("CRYPTO_PAIR_NOT_TRADABLE", result.violation_codes)

    def test_malformed_pair_symbol_fails(self):
        for symbol in ("BTC", "BTC/USD", "btcusd", "BTC-EUR"):
            with self.subTest(symbol=symbol):
                result = self.check(crypto_decision(self.state, "10.00", ticker=symbol))
                self.assertFalse(result.valid)
                self.assertIn("MALFORMED_CRYPTO_PAIR", result.violation_codes)

    def test_missing_universe_fails_closed(self):
        """No snapshot means no crypto proposal can be validated at all.

        The real snapshot ships in the repo, so the loader is stubbed to fail.
        """
        import src.guardrails as g
        original = g.load_crypto_universe

        def boom(*_a, **_k):
            raise g.CryptoUniverseError("snapshot missing")

        g.load_crypto_universe = boom
        try:
            result = validate(
                crypto_decision(self.state, "10.00"), self.config, self.state, crypto_universe=None
            )
        finally:
            g.load_crypto_universe = original
        self.assertFalse(result.valid)
        self.assertIn("CRYPTO_UNIVERSE_UNAVAILABLE", result.violation_codes)

    def test_stale_universe_warns_but_does_not_block(self):
        stale = make_crypto_universe(fetched_at="2020-01-01T00:00:00Z")
        result = self.check(crypto_decision(self.state, "10.00"), universe=stale)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertTrue(any("snapshot is" in w and "days old" in w for w in result.warnings))

    # --- order sizing -----------------------------------------------------
    def test_below_minimum_order_size_fails(self):
        """$1.00 of BONK at $0.01 buys 100 units, under BONK's 1,000-unit floor."""
        result = self.check(
            crypto_decision(self.state, "1.00", ticker="BONK-USD", security_name="Bonk",
                            current_price_usd="0.01",
                            monthly_budget_after_usd=str(self.state.remaining_usd - Decimal("1.00")))
        )
        self.assertFalse(result.valid)
        self.assertIn("CRYPTO_BELOW_MIN_ORDER_SIZE", result.violation_codes)

    def test_above_minimum_order_size_passes(self):
        result = self.check(
            crypto_decision(self.state, "25.00", ticker="BONK-USD", security_name="Bonk",
                            current_price_usd="0.000005")
        )
        self.assertTrue(result.valid, result.violation_codes)

    # --- budget -----------------------------------------------------------
    def test_crypto_above_the_monthly_budget_fails(self):
        result = self.check(crypto_decision(self.state, "25.01"))
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_MONTHLY_BUDGET", result.violation_codes)

    def test_crypto_above_the_remaining_authorization_fails(self):
        self.state = make_state(self.config, committed="20.00", acted=["earlier"])
        result = self.check(crypto_decision(self.state, "10.00"))
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_REMAINING_BUDGET", result.violation_codes)

    def test_crypto_zero_and_negative_amounts_fail(self):
        for amount, code in (("0.00", "ZERO_AMOUNT"), ("-5.00", "NEGATIVE_AMOUNT")):
            with self.subTest(amount=amount):
                result = self.check(crypto_decision(self.state, amount))
                self.assertFalse(result.valid)
                self.assertIn(code, result.violation_codes)

    # --- prohibitions still bite on crypto --------------------------------
    def test_crypto_sell_fails(self):
        for override in ({"side": "sell"}, {"action": "sell"}):
            with self.subTest(override=override):
                result = self.check(crypto_decision(self.state, "10.00", **override))
                self.assertFalse(result.valid)
                self.assertIn("SELL_FORBIDDEN", result.violation_codes)

    def test_crypto_short_fails(self):
        result = self.check(crypto_decision(self.state, "10.00", is_short=True))
        self.assertFalse(result.valid)
        self.assertIn("SHORT_FORBIDDEN", result.violation_codes)

    def test_crypto_on_margin_fails(self):
        result = self.check(crypto_decision(self.state, "10.00", uses_margin=True))
        self.assertFalse(result.valid)
        self.assertIn("MARGIN_FORBIDDEN", result.violation_codes)

    def test_crypto_futures_and_leveraged_crypto_fail(self):
        for asset_type, code in (
            ("crypto_futures", "FUTURES_FORBIDDEN"),
            ("leveraged_crypto", "LEVERAGED_PRODUCT_FORBIDDEN"),
        ):
            with self.subTest(asset_type=asset_type):
                result = self.check(crypto_decision(self.state, "10.00", asset_type=asset_type))
                self.assertFalse(result.valid)
                self.assertIn(code, result.violation_codes)

    def test_crypto_transfer_fails(self):
        result = self.check(crypto_decision(self.state, "10.00", is_transfer=True))
        self.assertFalse(result.valid)
        self.assertIn("TRANSFER_FORBIDDEN", result.violation_codes)

    def test_crypto_must_not_carry_an_exchange(self):
        result = self.check(crypto_decision(self.state, "10.00", exchange="NASDAQ"))
        self.assertFalse(result.valid)
        self.assertIn("CRYPTO_FIELD_MISMATCH", result.violation_codes)

    # --- reasoning-quality warnings ---------------------------------------
    def test_meme_coin_produces_a_warning(self):
        result = self.check(
            crypto_decision(self.state, "10.00", ticker="DOGE-USD", security_name="Dogecoin",
                            current_price_usd="0.085")
        )
        self.assertTrue(result.valid, result.violation_codes)
        self.assertTrue(any("meme asset" in w for w in result.warnings))


class CrossAssetBudgetTests(unittest.TestCase):
    """The $25 is one shared authorization across every asset class."""

    def setUp(self):
        self.config = make_config()
        self.universe = make_crypto_universe()

    def test_equity_then_crypto_summing_to_exactly_the_budget_passes(self):
        state = make_state(self.config)

        equity = buy_decision(state, "15.00", decision_id="eq-1")
        first = validate(equity, self.config, state, crypto_universe=self.universe)
        self.assertTrue(first.valid, first.violation_codes)
        state = commit_purchase(state, "eq-1", Decimal("15.00"))

        crypto = crypto_decision(state, "10.00", decision_id="cr-1")
        second = validate(crypto, self.config, state, crypto_universe=self.universe)
        self.assertTrue(second.valid, second.violation_codes)
        state = commit_purchase(state, "cr-1", Decimal("10.00"))

        self.assertEqual(state.committed_usd, Decimal("25.00"))
        self.assertEqual(state.remaining_usd, Decimal("0.00"))

    def test_equity_plus_crypto_over_the_budget_fails_on_the_second_leg(self):
        state = make_state(self.config, committed="15.00", acted=["eq-1"])
        crypto = crypto_decision(state, "10.01", decision_id="cr-1")
        result = validate(crypto, self.config, state, crypto_universe=self.universe)
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_REMAINING_BUDGET", result.violation_codes)
        self.assertIn("EXCEEDS_CUMULATIVE_MONTHLY_BUDGET", result.violation_codes)

    def test_crypto_first_then_equity_over_budget_also_fails(self):
        state = make_state(self.config, committed="24.00", acted=["cr-1"])
        result = validate(
            buy_decision(state, "2.00", decision_id="eq-1"), self.config, state,
            crypto_universe=self.universe,
        )
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_CUMULATIVE_MONTHLY_BUDGET", result.violation_codes)

    def test_a_full_crypto_month_leaves_nothing_for_equities(self):
        state = make_state(self.config, committed="25.00", acted=["cr-1"])
        result = validate(
            buy_decision(state, "0.01", decision_id="eq-1"), self.config, state,
            crypto_universe=self.universe,
        )
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_REMAINING_BUDGET", result.violation_codes)

    def test_duplicate_decision_id_fails_across_asset_classes(self):
        """An id acted on as an equity may not be replayed as a crypto buy."""
        state = make_state(self.config, acted=["shared-id"])
        for builder in (buy_decision, crypto_decision):
            with self.subTest(builder=builder.__name__):
                result = validate(
                    builder(state, "10.00", decision_id="shared-id"), self.config, state,
                    crypto_universe=self.universe,
                )
                self.assertFalse(result.valid)
                self.assertIn("DUPLICATE_DECISION_ID", result.violation_codes)

    def test_unused_authorization_does_not_carry_into_a_crypto_month(self):
        from src.state import fresh_state

        quiet = make_state(self.config, month="2026-09", committed="0.00")
        october = fresh_state(self.config, month="2026-10",
                              acted_decision_ids=quiet.acted_decision_ids)
        self.assertEqual(october.authorized_budget_usd, Decimal("25.00"))
        result = validate(
            crypto_decision(october, "50.00"), self.config, october,
            crypto_universe=self.universe,
        )
        self.assertFalse(result.valid)
        self.assertIn("EXCEEDS_MONTHLY_BUDGET", result.violation_codes)

    def test_arming_does_not_invalidate_a_sound_crypto_decision_either(self):
        config = make_config(execution_mode="APPROVAL_REQUIRED",
                             agent_enabled=True, live_trading=True)
        state = make_state(config)
        result = validate(
            crypto_decision(state, "10.00"), config, state, crypto_universe=self.universe
        )
        self.assertTrue(result.valid, result.violation_codes)
        self.assertNotIn("LIVE_TRADING_NOT_PERMITTED", result.violation_codes)
        self.assertFalse(result.executable)


class LongTermDisciplineTests(unittest.TestCase):
    """Rules that keep the agent an investor rather than a trader."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def test_a_short_horizon_fails(self):
        for months in (0, 1, 6, 23):
            with self.subTest(months=months):
                result = validate(
                    buy_decision(self.state, "10.00", investment_horizon_months=months),
                    self.config, self.state,
                )
                self.assertFalse(result.valid)
                self.assertIn("HORIZON_TOO_SHORT", result.violation_codes)

    def test_a_missing_horizon_fails(self):
        decision = buy_decision(self.state, "10.00")
        del decision["investment_horizon_months"]
        result = validate(decision, self.config, self.state)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_REQUIRED_FIELD", result.violation_codes)

    def test_a_two_year_horizon_passes(self):
        result = validate(
            buy_decision(self.state, "10.00", investment_horizon_months=24),
            self.config, self.state,
        )
        self.assertTrue(result.valid, result.violation_codes)

    def test_a_classification_that_contradicts_the_buy_fails(self):
        for label in ("TOO_EXPENSIVE", "TOO_EXTENDED", "SPECULATIVE_HYPE",
                      "FUNDAMENTALS_WEAK", "WATCH", "REJECTED", "HOLD_NO_ADD",
                      "THESIS_WEAKENING", "OVERCONCENTRATED", "ATTRACTIVE_ON_PULLBACK"):
            with self.subTest(classification=label):
                result = validate(
                    buy_decision(self.state, "10.00", classification=label),
                    self.config, self.state,
                )
                self.assertFalse(result.valid)
                self.assertIn("CLASSIFICATION_CONTRADICTS_BUY", result.violation_codes)

    def test_an_unknown_classification_fails(self):
        result = validate(
            buy_decision(self.state, "10.00", classification="LOOKS_GREAT"),
            self.config, self.state,
        )
        self.assertFalse(result.valid)
        self.assertIn("INVALID_CLASSIFICATION", result.violation_codes)

    def test_missing_position_type_fails(self):
        decision = buy_decision(self.state, "10.00")
        del decision["position_type"]
        result = validate(decision, self.config, self.state)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_POSITION_TYPE", result.violation_codes)

    def test_invalid_position_type_fails(self):
        result = validate(
            buy_decision(self.state, "10.00", position_type="MAYBE"), self.config, self.state
        )
        self.assertFalse(result.valid)
        self.assertIn("INVALID_POSITION_TYPE", result.violation_codes)

    def test_new_v2_narrative_fields_are_required(self):
        for field in ("value_creation", "valuation_reasoning", "portfolio_exposure", "invalidation"):
            with self.subTest(field=field):
                result = validate(
                    buy_decision(self.state, "10.00", **{field: ""}), self.config, self.state
                )
                self.assertFalse(result.valid)
                self.assertIn("MISSING_REQUIRED_FIELD", result.violation_codes)


class CostBasisTests(unittest.TestCase):
    """Averaging down must be reasoned about honestly, and the arithmetic checked."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def test_adding_to_an_existing_position_requires_cost_basis_analysis(self):
        decision = add_to_existing(self.state, "10.00")
        del decision["cost_basis_analysis"]
        result = validate(decision, self.config, self.state)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_COST_BASIS_ANALYSIS", result.violation_codes)

    def test_incomplete_cost_basis_analysis_fails(self):
        for key in ("average_cost_usd", "raises_or_lowers_average", "economic_meaning"):
            with self.subTest(key=key):
                decision = add_to_existing(self.state, "10.00")
                decision["cost_basis_analysis"][key] = ""
                result = validate(decision, self.config, self.state)
                self.assertFalse(result.valid)
                self.assertIn("MISSING_COST_BASIS_ANALYSIS", result.violation_codes)

    def test_claiming_a_purchase_above_cost_lowers_the_average_fails(self):
        """Buying at $230 against an $83.92 average RAISES it. Saying otherwise
        is arithmetic, not judgment — so the code checks it."""
        decision = add_to_existing(self.state, "10.00")
        decision["cost_basis_analysis"]["raises_or_lowers_average"] = "LOWERS"
        result = validate(decision, self.config, self.state)
        self.assertFalse(result.valid)
        self.assertIn("COST_BASIS_DIRECTION_WRONG", result.violation_codes)

    def test_a_genuine_average_down_is_labelled_correctly(self):
        decision = add_to_existing(
            self.state, "10.00", ticker="CIFR", security_name="Cipher Mining Inc.",
            current_price_usd="17.75",
            cost_basis_analysis={
                "average_cost_usd": "26.37",
                "raises_or_lowers_average": "LOWERS",
                "economic_meaning": "A lower average is a side effect, not the reason; the "
                                    "question is whether forward return from $17.75 is attractive.",
            },
        )
        result = validate(decision, self.config, self.state)
        self.assertTrue(result.valid, result.violation_codes)

    def test_claiming_an_average_down_when_buying_higher_fails(self):
        decision = add_to_existing(
            self.state, "10.00", ticker="CIFR", security_name="Cipher Mining Inc.",
            current_price_usd="30.00",
            cost_basis_analysis={
                "average_cost_usd": "26.37",
                "raises_or_lowers_average": "LOWERS",
                "economic_meaning": "...",
            },
        )
        result = validate(decision, self.config, self.state)
        self.assertFalse(result.valid)
        self.assertIn("COST_BASIS_DIRECTION_WRONG", result.violation_codes)

    def test_a_new_position_needs_no_cost_basis_analysis(self):
        result = validate(buy_decision(self.state, "10.00"), self.config, self.state)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertNotIn("cost_basis_analysis_present_and_consistent", result.checks_run)

    def test_crypto_add_also_requires_cost_basis_analysis(self):
        decision = crypto_decision(
            self.state, "10.00", position_type="EXISTING_POSITION", classification="ADD_CANDIDATE"
        )
        result = validate(
            decision, self.config, self.state, crypto_universe=make_crypto_universe()
        )
        self.assertFalse(result.valid)
        self.assertIn("MISSING_COST_BASIS_ANALYSIS", result.violation_codes)


class WaitCompetitionTests(unittest.TestCase):
    """WAIT must still name the strongest candidate in each competing bucket."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def test_valid_wait_passes(self):
        result = validate(wait_decision(self.state), self.config, self.state)
        self.assertTrue(result.valid, result.violation_codes)

    def test_wait_must_compare_all_four_capital_use_buckets(self):
        """Equities, ETFs and crypto compete for one authorization."""
        for bucket in ("best_existing_equity_candidate", "best_new_equity_candidate",
                       "best_existing_crypto_candidate", "best_new_crypto_candidate"):
            for empty in ("", None, {}):
                with self.subTest(bucket=bucket, empty=empty):
                    result = validate(
                        wait_decision(self.state, **{bucket: empty}),
                        self.config, self.state,
                    )
                    self.assertFalse(result.valid)
                    self.assertIn("MISSING_CAPITAL_USE_COMPARISON",
                                  result.violation_codes)

    def test_a_dropped_bucket_is_caught(self):
        for bucket in ("best_existing_equity_candidate", "best_new_equity_candidate",
                       "best_existing_crypto_candidate", "best_new_crypto_candidate"):
            with self.subTest(bucket=bucket):
                decision = wait_decision(self.state)
                del decision[bucket]
                result = validate(decision, self.config, self.state)
                self.assertFalse(result.valid)
                self.assertIn("MISSING_CAPITAL_USE_COMPARISON", result.violation_codes)

    def test_crypto_is_not_an_afterthought_both_buckets_are_required(self):
        """Naming one crypto candidate does not satisfy both crypto buckets."""
        decision = wait_decision(self.state)
        del decision["best_new_crypto_candidate"]
        result = validate(decision, self.config, self.state)
        self.assertFalse(result.valid)
        message = " ".join(v.message for v in result.violations)
        self.assertIn("best_new_crypto_candidate", message)

    def test_a_bucket_may_be_marked_not_applicable_with_a_reason(self):
        # A genuine inapplicability: no crypto is held in ANY account. An empty
        # agentic account alone would not justify this -- see the policy docs.
        decision = wait_decision(
            self.state,
            best_existing_crypto_candidate=(
                "NOT_APPLICABLE: no cryptocurrency is held in any of the owner's "
                "accounts, so there is nothing to add to."
            ),
        )
        result = validate(decision, self.config, self.state)
        self.assertTrue(result.valid, result.violation_codes)

    def test_not_applicable_without_a_reason_is_rejected(self):
        for value in ("NOT_APPLICABLE", "NOT_APPLICABLE:", "NOT_APPLICABLE: n/a"):
            with self.subTest(value=value):
                result = validate(
                    wait_decision(self.state, best_existing_crypto_candidate=value),
                    self.config, self.state,
                )
                self.assertFalse(result.valid)
                self.assertIn("MISSING_CAPITAL_USE_COMPARISON", result.violation_codes)

    def test_a_bucket_must_say_why_the_candidate_fell_short(self):
        result = validate(
            wait_decision(self.state,
                          best_new_crypto_candidate={"symbol": "SOL-USD"}),
            self.config, self.state,
        )
        self.assertFalse(result.valid)
        self.assertIn("MISSING_CAPITAL_USE_COMPARISON", result.violation_codes)

    def test_the_pre_stage7_three_bucket_shape_still_validates(self):
        """Decisions logged before Stage 7 must not retroactively become invalid."""
        result = validate(legacy_wait_decision(self.state), self.config, self.state)
        self.assertTrue(result.valid, result.violation_codes)


# ==========================================================================
# Version 3: capital optionality, hurdles, research gates, theme precision
# ==========================================================================

from datetime import datetime, timezone  # noqa: E402


def at(day):
    """A fixed point in September 2026."""
    return datetime(2026, 9, day, 12, 0, tzinfo=timezone.utc)


def optionality(day, **overrides):
    """A complete optionality block consistent with a given day of September."""
    block = {
        "days_remaining_in_month": 30 - day + 1,
        "known_events_this_month": "MU reports on 2026-09-30.",
        "candidates_being_watched": "GEV and WDC pending deeper research.",
        "probability_ranking_changes": "Moderate: the late-month print could reorder the memory names.",
        "dry_powder_benefit": "Preserved budget could respond to the 2026-09-30 print.",
        "partial_deployment_considered": "A $10 tranche was considered and compared against $25.",
    }
    block.update(overrides)
    return block


class MonthlyOptionalityTests(unittest.TestCase):
    """The $25 is a month-long option, not a today-only budget."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def test_a_buy_without_an_optionality_block_fails(self):
        decision = buy_decision(self.state, "10.00", monthly_optionality=None)
        result = validate(decision, self.config, self.state, now=at(20))
        self.assertFalse(result.valid)
        self.assertIn("MISSING_OPTIONALITY_ANALYSIS", result.violation_codes)

    def test_an_incomplete_optionality_block_fails(self):
        for key in ("known_events_this_month", "candidates_being_watched",
                    "dry_powder_benefit", "partial_deployment_considered"):
            with self.subTest(key=key):
                decision = buy_decision(
                    self.state, "10.00", monthly_optionality=optionality(20, **{key: ""})
                )
                result = validate(decision, self.config, self.state, now=at(20))
                self.assertFalse(result.valid)
                self.assertIn("MISSING_OPTIONALITY_ANALYSIS", result.violation_codes)

    def test_misreporting_days_remaining_fails(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20, days_remaining_in_month=3)
        )
        result = validate(decision, self.config, self.state, now=at(20))
        self.assertFalse(result.valid)
        self.assertIn("OPTIONALITY_MISREPORTED", result.violation_codes)

    def test_majority_spend_before_mid_month_requires_written_justification(self):
        """$25 of $25 on day 5 -- 100% of the month's flexibility surrendered."""
        decision = buy_decision(self.state, "25.00", monthly_optionality=optionality(5))
        decision.pop("monthly_optionality_analysis")
        result = validate(decision, self.config, self.state, now=at(5))
        self.assertFalse(result.valid)
        self.assertIn("MISSING_OPTIONALITY_ANALYSIS", result.violation_codes)

    def test_a_token_justification_does_not_satisfy_the_gate(self):
        decision = buy_decision(
            self.state, "25.00",
            monthly_optionality=optionality(5),
            monthly_optionality_analysis="Seems fine.",
        )
        result = validate(decision, self.config, self.state, now=at(5))
        self.assertFalse(result.valid)
        self.assertIn("MISSING_OPTIONALITY_ANALYSIS", result.violation_codes)

    def test_majority_spend_before_mid_month_passes_with_a_real_justification(self):
        decision = buy_decision(self.state, "25.00", monthly_optionality=optionality(5))
        result = validate(decision, self.config, self.state, now=at(5))
        self.assertTrue(result.valid, result.violation_codes)
        self.assertTrue(any("day 5 of the month" in w for w in result.warnings))

    def test_minority_spend_before_mid_month_needs_no_extra_justification(self):
        decision = buy_decision(self.state, "10.00", monthly_optionality=optionality(5))
        decision.pop("monthly_optionality_analysis")
        result = validate(decision, self.config, self.state, now=at(5))
        self.assertTrue(result.valid, result.violation_codes)

    def test_the_gate_is_cumulative_not_per_order(self):
        """$10 already spent plus $8 today crosses the half-authorization mark."""
        state = make_state(self.config, committed="10.00", acted=["earlier"])
        decision = buy_decision(state, "8.00", monthly_optionality=optionality(6))
        decision.pop("monthly_optionality_analysis")
        result = validate(decision, self.config, state, now=at(6))
        self.assertFalse(result.valid)
        self.assertIn("MISSING_OPTIONALITY_ANALYSIS", result.violation_codes)

    def test_after_mid_month_the_extra_gate_does_not_apply(self):
        decision = buy_decision(self.state, "25.00", monthly_optionality=optionality(20))
        decision.pop("monthly_optionality_analysis")
        result = validate(decision, self.config, self.state, now=at(20))
        self.assertTrue(result.valid, result.violation_codes)

    def test_partial_deployment_amounts_are_all_valid(self):
        for amount in ("5.00", "10.00", "15.00", "20.00", "25.00"):
            with self.subTest(amount=amount):
                decision = buy_decision(
                    self.state, amount, monthly_optionality=optionality(20)
                )
                result = validate(decision, self.config, self.state, now=at(20))
                self.assertTrue(result.valid, (amount, result.violation_codes))

    def test_below_the_broker_minimum_fails(self):
        for amount in ("0.01", "0.50", "0.99"):
            with self.subTest(amount=amount):
                decision = buy_decision(
                    self.state, amount, monthly_optionality=optionality(20)
                )
                result = validate(decision, self.config, self.state, now=at(20))
                self.assertFalse(result.valid)
                self.assertIn("BELOW_BROKER_MINIMUM", result.violation_codes)

    def test_exactly_one_dollar_is_allowed(self):
        decision = buy_decision(self.state, "1.00", monthly_optionality=optionality(20))
        result = validate(decision, self.config, self.state, now=at(20))
        self.assertTrue(result.valid, result.violation_codes)


class WaitPhilosophyTests(unittest.TestCase):
    """WAIT does not require a pending event. Valuation alone is enough."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def test_a_wait_must_declare_its_basis(self):
        decision = wait_decision(self.state)
        del decision["wait_basis"]
        result = validate(decision, self.config, self.state)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_REQUIRED_FIELD", result.violation_codes)

    def test_an_unknown_wait_basis_fails(self):
        result = validate(
            wait_decision(self.state, wait_basis=["VIBES"]), self.config, self.state
        )
        self.assertFalse(result.valid)
        self.assertIn("INVALID_WAIT_BASIS", result.violation_codes)

    def test_valuation_alone_is_a_sufficient_basis_to_wait(self):
        """No future event needed -- the price is simply not good enough."""
        decision = wait_decision(
            self.state,
            wait_basis=["VALUATION"],
            thesis="Every finalist is priced for an outcome better than the evidence supports.",
            timing_reason="Nothing needs to happen; the entry prices themselves are the problem.",
        )
        result = validate(decision, self.config, self.state)
        self.assertTrue(result.valid, result.violation_codes)

    def test_every_standalone_basis_is_accepted_on_its_own(self):
        from src.guardrails import STANDALONE_WAIT_BASES

        for basis in sorted(STANDALONE_WAIT_BASES):
            with self.subTest(basis=basis):
                result = validate(
                    wait_decision(self.state, wait_basis=basis), self.config, self.state
                )
                self.assertTrue(result.valid, (basis, result.violation_codes))

    def test_buying_on_the_absence_of_information_is_flagged(self):
        decision = buy_decision(
            self.state, "10.00",
            monthly_optionality=optionality(20),
            timing_reason="There is no pending catalyst, so there is nothing to wait for.",
            why_not_wait="Waiting buys no information before the next report.",
        )
        result = validate(decision, self.config, self.state, now=at(20))
        self.assertTrue(
            any("absence of upcoming information" in w for w in result.warnings),
            result.warnings,
        )

    def test_a_buy_must_carry_why_not_wait_and_margin_for_error(self):
        for field in ("why_not_wait", "margin_for_error"):
            with self.subTest(field=field):
                decision = buy_decision(
                    self.state, "10.00", monthly_optionality=optionality(20), **{field: ""}
                )
                result = validate(decision, self.config, self.state, now=at(20))
                self.assertFalse(result.valid)
                self.assertIn("MISSING_REQUIRED_FIELD", result.violation_codes)


class NewPositionHurdleTests(unittest.TestCase):
    """A new ticker is not automatically better than more of a good one."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def check(self, decision):
        return validate(decision, self.config, self.state, now=at(20))

    def test_a_new_position_without_a_justification_block_fails(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            new_position_justification=None,
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_NEW_POSITION_JUSTIFICATION", result.violation_codes)

    def test_each_required_comparison_is_enforced(self):
        for key in ("incremental_expected_return", "diversification_benefit",
                    "overlap_with_existing", "diversification_is_economic_not_cosmetic",
                    "why_new_position_beats_adding_to_existing", "initial_size_significance"):
            with self.subTest(key=key):
                decision = buy_decision(
                    self.state, "10.00", monthly_optionality=optionality(20),
                    new_position_justification=new_position_justification(**{key: ""}),
                )
                result = self.check(decision)
                self.assertFalse(result.valid)
                self.assertIn("MISSING_NEW_POSITION_JUSTIFICATION", result.violation_codes)

    def test_the_comparison_must_name_a_specific_existing_holding(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            new_position_justification=new_position_justification(best_existing_alternative=""),
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_NEW_POSITION_JUSTIFICATION", result.violation_codes)

    def test_comparing_the_asset_against_itself_fails(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20), ticker="VTI",
            new_position_justification=new_position_justification(
                best_existing_alternative={"symbol": "VTI", "why_not": "circular"}
            ),
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("MISSING_NEW_POSITION_JUSTIFICATION", result.violation_codes)

    def test_filling_an_empty_sector_is_flagged_as_a_weak_argument(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            new_position_justification=new_position_justification(
                diversification_benefit="Fills an empty sector in the portfolio.",
            ),
        )
        result = self.check(decision)
        self.assertTrue(
            any("Missing exposure is a research signal" in w for w in result.warnings),
            result.warnings,
        )

    def test_adding_to_an_existing_position_needs_no_new_position_block(self):
        decision = add_to_existing(self.state, "10.00", monthly_optionality=optionality(20))
        result = self.check(decision)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertNotIn("new_position_hurdle", result.checks_run)


class ResearchCompletenessTests(unittest.TestCase):
    """No BUY on a new position from incomplete research."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def check(self, decision, universe=None):
        return validate(decision, self.config, self.state,
                        crypto_universe=universe, now=at(20))

    def test_research_incomplete_is_never_a_buyable_classification(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            classification="RESEARCH_INCOMPLETE",
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("CLASSIFICATION_CONTRADICTS_BUY", result.violation_codes)

    def test_a_new_position_without_a_research_package_fails(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20), research_package=None
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("RESEARCH_INCOMPLETE_FOR_NEW_POSITION", result.violation_codes)

    def test_an_omitted_component_fails(self):
        package = research_package("etf")
        del package["expense_ratio"]
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20), research_package=package
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("RESEARCH_INCOMPLETE_FOR_NEW_POSITION", result.violation_codes)

    def test_a_mandatory_component_declared_unavailable_fails(self):
        for key in ("current_quote_and_valuation", "recent_quarterly_results",
                    "multi_quarter_trends", "balance_sheet", "major_risks",
                    "bull_case", "bear_case"):
            with self.subTest(key=key):
                package = research_package("equity", **{key: "NOT_AVAILABLE: no data"})
                decision = buy_decision(
                    self.state, "10.00", monthly_optionality=optionality(20),
                    asset_type="us_common_stock", asset_class="EQUITY", ticker="ISRG",
                    research_package=package,
                )
                result = self.check(decision)
                self.assertFalse(result.valid)
                self.assertIn("RESEARCH_INCOMPLETE_FOR_NEW_POSITION", result.violation_codes)

    def test_a_waivable_component_declared_unavailable_only_warns(self):
        package = research_package("equity", cash_flow="NOT_AVAILABLE: no tool exposes it")
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            asset_type="us_common_stock", asset_class="EQUITY", ticker="ISRG",
            theme={"primary": "healthcare", "subtheme": "robotic_assisted_surgery",
                   "scope_caveat": "Surgical robotics only; not pharma or medtech broadly."},
            research_package=package,
        )
        result = self.check(decision)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertTrue(any("cash_flow" in w for w in result.warnings))

    def test_an_etf_package_does_not_satisfy_an_equity_position(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            asset_type="us_common_stock", asset_class="EQUITY", ticker="ISRG",
            research_package=research_package("etf"),
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("RESEARCH_INCOMPLETE_FOR_NEW_POSITION", result.violation_codes)

    def test_crypto_uses_its_own_adapted_package(self):
        decision = crypto_decision(
            self.state, "10.00", monthly_optionality=optionality(20)
        )
        result = self.check(decision, universe=make_crypto_universe())
        self.assertTrue(result.valid, result.violation_codes)

    def test_a_crypto_position_with_an_equity_package_fails(self):
        decision = crypto_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            research_package=research_package("equity"),
        )
        result = self.check(decision, universe=make_crypto_universe())
        self.assertFalse(result.valid)
        self.assertIn("RESEARCH_INCOMPLETE_FOR_NEW_POSITION", result.violation_codes)


class PrimarySourceTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def check(self, decision):
        return validate(decision, self.config, self.state, now=at(20))

    def test_a_new_position_needs_primary_sources(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            evidence=[{"tool": "get_equity_quotes", "detail": "price"}],
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("INSUFFICIENT_PRIMARY_SOURCES", result.violation_codes)

    def test_one_primary_source_is_not_enough(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            evidence=[
                {"tool": "get_financials", "detail": "revenue"},
                {"tool": "get_equity_news", "detail": "an analyst upgrade"},
            ],
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("INSUFFICIENT_PRIMARY_SOURCES", result.violation_codes)

    def test_analyst_opinion_cannot_substitute_for_filings(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            evidence=[
                {"source_type": "ANALYST_OPINION", "detail": "PT $500"},
                {"source_type": "ANALYST_OPINION", "detail": "upgraded to Buy"},
                {"source_type": "ANALYST_OPINION", "detail": "mean target $480"},
            ],
        )
        result = self.check(decision)
        self.assertFalse(result.valid)
        self.assertIn("INSUFFICIENT_PRIMARY_SOURCES", result.violation_codes)

    def test_sec_filings_and_financials_satisfy_the_requirement(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            evidence=[
                {"tool": "get_sec_filing_facts", "source_type": "SEC_FILING", "detail": "Assets"},
                {"tool": "get_financials", "source_type": "OFFICIAL_FINANCIALS", "detail": "revenue"},
            ],
        )
        result = self.check(decision)
        self.assertTrue(result.valid, result.violation_codes)

    def test_analyst_heavy_evidence_warns(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            evidence=[
                {"tool": "get_sec_filing_facts", "detail": "Assets"},
                {"tool": "get_financials", "detail": "revenue"},
                {"tool": "get_equity_news", "detail": "upgrade"},
                {"source_type": "ANALYST_OPINION", "detail": "target"},
            ],
        )
        result = self.check(decision)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertTrue(any("must not carry the thesis" in w for w in result.warnings))


class PortfolioSprawlTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def test_a_buy_without_a_sprawl_assessment_fails(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            portfolio_sprawl_assessment=None,
        )
        result = validate(decision, self.config, self.state, now=at(20))
        self.assertFalse(result.valid)
        self.assertIn("MISSING_SPRAWL_ASSESSMENT", result.violation_codes)

    def test_each_sprawl_component_is_required(self):
        from src.models import REQUIRED_SPRAWL_KEYS

        for key in REQUIRED_SPRAWL_KEYS:
            with self.subTest(key=key):
                block = dict(buy_decision(self.state, "10.00")["portfolio_sprawl_assessment"])
                block[key] = ""
                decision = buy_decision(
                    self.state, "10.00", monthly_optionality=optionality(20),
                    portfolio_sprawl_assessment=block,
                )
                result = validate(decision, self.config, self.state, now=at(20))
                self.assertFalse(result.valid)
                self.assertIn("MISSING_SPRAWL_ASSESSMENT", result.violation_codes)

    def test_the_assessment_is_required_for_adds_too(self):
        decision = add_to_existing(
            self.state, "10.00", monthly_optionality=optionality(20),
            portfolio_sprawl_assessment=None,
        )
        result = validate(decision, self.config, self.state, now=at(20))
        self.assertFalse(result.valid)
        self.assertIn("MISSING_SPRAWL_ASSESSMENT", result.violation_codes)


class ThemePrecisionTests(unittest.TestCase):
    """Surgical robotics is healthcare. It is not warehouse automation."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def isrg(self, **theme):
        block = {
            "primary": "healthcare",
            "subtheme": "robotic_assisted_surgery",
            "scope_caveat": "Robotic-assisted surgery only; this is not industrial robotics, "
                            "humanoid robotics, warehouse automation, or autonomous systems, "
                            "and it is not broad healthcare exposure.",
        }
        block.update(theme)
        return buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20),
            asset_type="us_common_stock", asset_class="EQUITY", ticker="ISRG",
            security_name="Intuitive Surgical, Inc.", exchange="NASDAQ",
            research_package=research_package("equity"), theme=block,
        )

    def test_a_buy_without_a_theme_fails(self):
        decision = buy_decision(
            self.state, "10.00", monthly_optionality=optionality(20), theme=None
        )
        result = validate(decision, self.config, self.state, now=at(20))
        self.assertFalse(result.valid)
        self.assertIn("MISSING_REQUIRED_FIELD", result.violation_codes)

    def test_an_unknown_subtheme_fails(self):
        for subtheme in ("robotics", "tech", "ai", "healthcare"):
            with self.subTest(subtheme=subtheme):
                result = validate(
                    self.isrg(subtheme=subtheme), self.config, self.state, now=at(20)
                )
                self.assertFalse(result.valid)
                self.assertIn("INVALID_THEME", result.violation_codes)

    def test_surgical_robotics_classified_as_industrial_robotics_fails(self):
        for subtheme in ("industrial_robotics", "humanoid_robotics", "warehouse_automation"):
            with self.subTest(subtheme=subtheme):
                result = validate(
                    self.isrg(subtheme=subtheme), self.config, self.state, now=at(20)
                )
                self.assertFalse(result.valid)
                self.assertIn("THEME_MISMATCH", result.violation_codes)

    def test_surgical_robotics_classified_as_healthcare_passes(self):
        result = validate(self.isrg(), self.config, self.state, now=at(20))
        self.assertTrue(result.valid, result.violation_codes)

    def test_a_missing_scope_caveat_fails(self):
        result = validate(self.isrg(scope_caveat=""), self.config, self.state, now=at(20))
        self.assertFalse(result.valid)
        self.assertIn("MISSING_REQUIRED_FIELD", result.violation_codes)

    def test_the_taxonomy_separates_medical_from_industrial_robotics(self):
        from src.models import THEME_TAXONOMY

        self.assertEqual(THEME_TAXONOMY["robotic_assisted_surgery"], "healthcare")
        for subtheme in ("industrial_robotics", "humanoid_robotics",
                         "warehouse_automation", "motion_control"):
            self.assertEqual(THEME_TAXONOMY[subtheme], "robotics_automation")
        self.assertEqual(THEME_TAXONOMY["autonomous_vehicles"], "autonomous_systems")


# --------------------------------------------------------------------------
# The calendar boundary of a non-carrying authorization
# --------------------------------------------------------------------------


def mu_print(**overrides):
    """MU's FQ4 print: after the close on 2026-09-30, September's last session."""
    event = {
        "label": "MU FQ4 earnings",
        "date": "2026-09-30",
        "asset_class": "EQUITY",
        "occurs_after_close": True,
        "actionable_with_this_month_authorization": False,
    }
    event.update(overrides)
    return event


ACKNOWLEDGED = (
    "MU reports after the close on 2026-09-30, the final September session, so the "
    "post-earnings equity reaction is first tradable on 2026-10-01 with October's "
    "authorization. Waiting for it therefore means allowing September's $25.00 to "
    "expire unused, which is accepted deliberately: no candidate on offer clears the "
    "bar, and an expired authorization is a better outcome than a purchase that does "
    "not."
)


class CalendarBoundaryTests(unittest.TestCase):
    """An event past the month's last tradable moment is next month's information.

    This is the failure the September report actually made: it treated MU's
    2026-09-30 after-close print as something September's $25 could respond to.
    There is no September session after that close, so it could not.
    """

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def wait(self, **overrides):
        decision = wait_decision(self.state)
        decision["wait_basis"] = ["AWAITING_INFORMATION", "VALUATION"]
        decision.update(overrides)
        return decision

    # --- the specific MU case ------------------------------------------

    def test_claiming_the_mu_print_is_actionable_in_september_is_rejected(self):
        decision = self.wait(
            events_considered=[mu_print(actionable_with_this_month_authorization=True)],
            authorization_expiry_acknowledged=ACKNOWLEDGED,
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("EVENT_ACTIONABILITY_MISREPORTED", result.violation_codes)

    def test_the_violation_names_the_first_tradable_session(self):
        decision = self.wait(
            events_considered=[mu_print(actionable_with_this_month_authorization=True)],
            authorization_expiry_acknowledged=ACKNOWLEDGED,
        )
        result = validate(decision, self.config, self.state, now=at(7))
        blob = " ".join(v.message for v in result.violations)
        self.assertIn("2026-10-01", blob)
        self.assertIn("2026-09-30 16:00", blob)

    def test_waiting_through_it_must_state_that_the_authorization_expires(self):
        decision = self.wait(events_considered=[mu_print()])
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("UNACKNOWLEDGED_AUTHORIZATION_EXPIRY", result.violation_codes)

    def test_the_expiry_violation_explains_the_consequence(self):
        decision = self.wait(events_considered=[mu_print()])
        result = validate(decision, self.config, self.state, now=at(7))
        message = next(
            v.message for v in result.violations
            if v.code == "UNACKNOWLEDGED_AUTHORIZATION_EXPIRY"
        )
        self.assertIn("does not roll over", message)
        self.assertIn("expire unused", message)
        self.assertIn("MU FQ4 earnings", message)

    def test_an_honest_wait_through_the_boundary_passes(self):
        decision = self.wait(
            events_considered=[mu_print()],
            authorization_expiry_acknowledged=ACKNOWLEDGED,
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertTrue(result.valid, result.violation_codes)
        self.assertIn(
            "authorization_expiry_stated_when_waiting_past_the_month", result.checks_run
        )

    def test_an_honest_wait_still_warns_so_the_expiry_is_visible(self):
        decision = self.wait(
            events_considered=[mu_print()],
            authorization_expiry_acknowledged=ACKNOWLEDGED,
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertTrue(
            any("expires unused" in w for w in result.warnings), result.warnings
        )

    # --- the boundary is about the clock, not the date -------------------

    def test_the_same_print_before_the_open_is_actionable_in_september(self):
        decision = self.wait(
            events_considered=[
                mu_print(
                    occurs_after_close=False,
                    actionable_with_this_month_authorization=True,
                )
            ],
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertTrue(result.valid, result.violation_codes)

    def test_session_timing_after_close_is_equivalent_to_the_boolean(self):
        event = mu_print()
        event.pop("occurs_after_close")
        event["session_timing"] = "AFTER_CLOSE"
        result = validate(
            self.wait(events_considered=[event]), self.config, self.state, now=at(7)
        )
        self.assertIn("UNACKNOWLEDGED_AUTHORIZATION_EXPIRY", result.violation_codes)

    def test_a_crypto_event_after_the_equity_close_stays_inside_the_month(self):
        """Crypto has no closing bell; the same instant is still September."""
        decision = self.wait(
            events_considered=[
                mu_print(
                    label="A protocol upgrade",
                    asset_class="CRYPTO",
                    actionable_with_this_month_authorization=True,
                )
            ],
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertTrue(result.valid, result.violation_codes)

    def test_an_event_inside_the_month_needs_no_expiry_acknowledgement(self):
        decision = self.wait(
            events_considered=[
                {
                    "label": "August CPI",
                    "date": "2026-09-11",
                    "asset_class": "EQUITY",
                    "actionable_with_this_month_authorization": True,
                }
            ],
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertTrue(result.valid, result.violation_codes)

    def test_an_event_in_a_later_month_is_next_month_only(self):
        decision = self.wait(
            events_considered=[
                {
                    "label": "Q3 GDP",
                    "date": "2026-10-29",
                    "asset_class": "EQUITY",
                    "actionable_with_this_month_authorization": True,
                }
            ],
            authorization_expiry_acknowledged=ACKNOWLEDGED,
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("EVENT_ACTIONABILITY_MISREPORTED", result.violation_codes)

    # --- the rule applies to a BUY too ---------------------------------

    def test_a_buy_may_not_misdescribe_a_boundary_event_either(self):
        """'Nothing to wait for' cannot be built on a misdated event."""
        decision = buy_decision(
            self.state, "10.00",
            monthly_optionality=optionality(7),
            events_considered=[mu_print(actionable_with_this_month_authorization=True)],
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("EVENT_ACTIONABILITY_MISREPORTED", result.violation_codes)

    # --- structural requirements ---------------------------------------

    def test_awaiting_information_must_list_the_events(self):
        decision = self.wait()
        decision.pop("events_considered", None)
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("MISSING_EVENT_ACTIONABILITY", result.violation_codes)

    def test_a_wait_on_other_bases_needs_no_event_list(self):
        """Valuation and risk/reward stand alone; they wait for nothing."""
        decision = wait_decision(self.state)
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertTrue(result.valid, result.violation_codes)

    def test_an_event_missing_its_actionability_verdict_fails(self):
        event = mu_print()
        event.pop("actionable_with_this_month_authorization")
        result = validate(
            self.wait(
                events_considered=[event],
                authorization_expiry_acknowledged=ACKNOWLEDGED,
            ),
            self.config, self.state, now=at(7),
        )
        self.assertFalse(result.valid)
        self.assertIn("MISSING_EVENT_ACTIONABILITY", result.violation_codes)

    def test_a_malformed_event_date_fails(self):
        result = validate(
            self.wait(events_considered=[mu_print(date="September 30")]),
            self.config, self.state, now=at(7),
        )
        self.assertFalse(result.valid)
        self.assertIn("MALFORMED_EVENT_DATE", result.violation_codes)

    def test_a_non_list_event_field_fails(self):
        result = validate(
            self.wait(events_considered="MU reports on the 30th"),
            self.config, self.state, now=at(7),
        )
        self.assertFalse(result.valid)
        self.assertIn("MALFORMED_EVENT_LIST", result.violation_codes)

    def test_an_unknown_event_asset_class_fails(self):
        result = validate(
            self.wait(events_considered=[mu_print(asset_class="OPTIONS")]),
            self.config, self.state, now=at(7),
        )
        self.assertFalse(result.valid)
        self.assertIn("INVALID_EVENT_ASSET_CLASS", result.violation_codes)

    def test_a_non_boolean_actionability_verdict_fails(self):
        result = validate(
            self.wait(
                events_considered=[
                    mu_print(actionable_with_this_month_authorization="maybe")
                ],
                authorization_expiry_acknowledged=ACKNOWLEDGED,
            ),
            self.config, self.state, now=at(7),
        )
        self.assertFalse(result.valid)
        self.assertIn("MISSING_EVENT_ACTIONABILITY", result.violation_codes)


class TradableSessionTests(unittest.TestCase):
    """Calendar days are not opportunities."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def test_sessions_remaining_excludes_weekends_and_holidays(self):
        # From Labor Day 2026-09-07 (a holiday) through the 30th: 17 sessions,
        # against 24 calendar days.
        self.assertEqual(guardrails.tradable_sessions_remaining(at(7)), 17)
        self.assertEqual(guardrails.days_remaining_in_month(at(7)), 24)

    def test_the_last_session_of_the_month_leaves_exactly_one(self):
        self.assertEqual(guardrails.tradable_sessions_remaining(at(30)), 1)

    def test_a_misreported_session_count_is_rejected(self):
        decision = buy_decision(
            self.state, "10.00",
            monthly_optionality=optionality(7, tradable_sessions_remaining=24),
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("OPTIONALITY_MISREPORTED", result.violation_codes)

    def test_a_correct_session_count_passes(self):
        decision = buy_decision(
            self.state, "10.00",
            monthly_optionality=optionality(7, tradable_sessions_remaining=17),
        )
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertTrue(result.valid, result.violation_codes)

    def test_the_field_is_optional(self):
        decision = buy_decision(self.state, "10.00", monthly_optionality=optionality(7))
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertTrue(result.valid, result.violation_codes)


# --------------------------------------------------------------------------
# Crypto research: Robinhood's scope is not the limit of what is knowable
# --------------------------------------------------------------------------


class CryptoResearchSourceTests(unittest.TestCase):
    """Robinhood is authoritative for the account, not for the network.

    A blockchain files no 10-Q. Demanding SEC-shaped evidence from one makes
    every crypto candidate unbuyable for a reason that says nothing about the
    asset; accepting the broker's silence as proof of unavailability does the
    same thing from the other direction.
    """

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)
        self.universe = make_crypto_universe()

    def crypto(self, **overrides):
        decision = crypto_decision(self.state, "10.00", **overrides)
        decision["monthly_optionality"] = optionality(7)
        return decision

    def validate_crypto(self, decision):
        return validate(
            decision, self.config, self.state,
            crypto_universe=self.universe, now=at(7),
        )

    def test_protocol_and_onchain_evidence_satisfies_the_primary_source_bar(self):
        result = self.validate_crypto(self.crypto())
        self.assertTrue(result.valid, result.violation_codes)
        self.assertIn("primary_sources_present", result.checks_run)

    def test_market_data_alone_does_not_satisfy_it(self):
        decision = self.crypto(
            evidence=[
                {"tool": "get_crypto_quotes", "source_type": "MARKET_DATA", "detail": "mark"},
                {"tool": "get_crypto_positions", "source_type": "MARKET_DATA", "detail": "held"},
            ]
        )
        result = self.validate_crypto(decision)
        self.assertFalse(result.valid)
        self.assertIn("INSUFFICIENT_PRIMARY_SOURCES", result.violation_codes)

    def test_the_shortfall_message_points_at_external_research(self):
        decision = self.crypto(
            evidence=[{"tool": "get_crypto_quotes", "source_type": "MARKET_DATA", "detail": "mark"}]
        )
        result = self.validate_crypto(decision)
        message = next(
            v.message for v in result.violations
            if v.code == "INSUFFICIENT_PRIMARY_SOURCES"
        )
        self.assertIn("Read-only external research", message)
        self.assertIn("PROTOCOL_DOCUMENTATION", message)

    def test_a_single_primary_source_is_not_enough(self):
        decision = self.crypto(
            evidence=[
                {"tool": "WebFetch", "source_type": "PROTOCOL_DOCUMENTATION", "detail": "spec"},
                {"tool": "get_crypto_quotes", "source_type": "MARKET_DATA", "detail": "mark"},
            ]
        )
        result = self.validate_crypto(decision)
        self.assertFalse(result.valid)
        self.assertIn("INSUFFICIENT_PRIMARY_SOURCES", result.violation_codes)

    def test_secondary_reporting_outweighing_primary_sources_warns(self):
        decision = self.crypto(
            evidence=[
                {"tool": "WebFetch", "source_type": "PROTOCOL_DOCUMENTATION", "detail": "spec"},
                {"tool": "WebFetch", "source_type": "ONCHAIN_DATA", "detail": "supply"},
                {"tool": "WebSearch", "source_type": "SECONDARY_REPORTING", "detail": "a"},
                {"tool": "WebSearch", "source_type": "ANALYST_OPINION", "detail": "b"},
            ]
        )
        result = self.validate_crypto(decision)
        self.assertTrue(result.valid, result.violation_codes)
        self.assertTrue(
            any("must not carry the thesis" in w for w in result.warnings), result.warnings
        )

    def test_equity_sources_are_unaffected(self):
        """The equity hierarchy is unchanged: filings still carry an equity thesis."""
        result = validate(
            buy_decision(self.state, "10.00", monthly_optionality=optionality(7)),
            self.config, self.state, now=at(7),
        )
        self.assertTrue(result.valid, result.violation_codes)


class CryptoResearchGapTests(unittest.TestCase):
    """A waived crypto component must name a real obstacle, not the tool list."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)
        self.universe = make_crypto_universe()

    def crypto_with_gap(self, component, reason):
        package = research_package("crypto")
        package[component] = reason
        decision = crypto_decision(self.state, "10.00", research_package=package)
        decision["monthly_optionality"] = optionality(7)
        return validate(
            decision, self.config, self.state,
            crypto_universe=self.universe, now=at(7),
        )

    BROKER_EXCUSES = (
        "NOT_AVAILABLE: Robinhood does not expose network usage data.",
        "NOT_AVAILABLE: no Robinhood tool provides an issuance schedule.",
        "NOT_AVAILABLE: the connected read-only tools do not provide this.",
        "NOT_AVAILABLE: not available from Robinhood.",
        "NOT_AVAILABLE: the MCP server has no endpoint for protocol development.",
        "NOT_AVAILABLE: no connected tool exposes developer activity.",
        "NOT_AVAILABLE: Robinhood, being a broker, has no on-chain data.",
        "NOT_AVAILABLE: outside the scope of the Robinhood tool surface.",
    )

    REAL_OBSTACLES = (
        "NOT_AVAILABLE: read-only web research was unavailable in this run.",
        "NOT_AVAILABLE: no reputable provider publishes a dated fee-revenue series "
        "for this network.",
        "NOT_AVAILABLE: the protocol specification site did not respond.",
        "NOT_AVAILABLE: the figure does not exist yet; the chain is under a year old.",
    )

    def test_blaming_the_broker_for_a_gap_is_rejected(self):
        for reason in self.BROKER_EXCUSES:
            with self.subTest(reason=reason):
                result = self.crypto_with_gap("ecosystem_and_development", reason)
                self.assertFalse(result.valid)
                self.assertIn("RESEARCH_GAP_NOT_JUSTIFIED", result.violation_codes)

    def test_the_violation_says_what_robinhood_is_authoritative_for(self):
        result = self.crypto_with_gap(
            "ecosystem_and_development",
            "NOT_AVAILABLE: Robinhood does not expose developer activity.",
        )
        message = next(
            v.message for v in result.violations if v.code == "RESEARCH_GAP_NOT_JUSTIFIED"
        )
        self.assertIn("cost basis", message)
        self.assertIn("tradability", message)
        self.assertIn("read-only external sources", message)

    def test_a_mandatory_component_blamed_on_the_broker_is_rejected(self):
        result = self.crypto_with_gap(
            "network_usage_and_adoption",
            "NOT_AVAILABLE: Robinhood provides no on-chain usage data.",
        )
        self.assertFalse(result.valid)
        self.assertIn("RESEARCH_GAP_NOT_JUSTIFIED", result.violation_codes)

    def test_a_real_obstacle_is_still_a_valid_waiver(self):
        """The escape hatch stays open, provided the obstacle is the real one."""
        for reason in self.REAL_OBSTACLES:
            with self.subTest(reason=reason):
                result = self.crypto_with_gap("ecosystem_and_development", reason)
                self.assertNotIn("RESEARCH_GAP_NOT_JUSTIFIED", result.violation_codes)

    def test_the_check_only_looks_at_components_claimed_unavailable(self):
        """Naming Robinhood in real findings is fine; only a waiver is judged."""
        package = research_package("crypto")
        package["ecosystem_and_development"] = (
            "Robinhood does not publish this and does not need to: the reference "
            "implementation shipped four releases this quarter, per its own changelog."
        )
        decision = crypto_decision(self.state, "10.00", research_package=package)
        decision["monthly_optionality"] = optionality(7)
        result = validate(
            decision, self.config, self.state,
            crypto_universe=self.universe, now=at(7),
        )
        self.assertTrue(result.valid, result.violation_codes)

    def test_an_equity_gap_is_not_subject_to_this_check(self):
        """Robinhood *is* a reasonable source for equity fundamentals."""
        package = research_package("etf")
        package["liquidity_and_spread"] = (
            "NOT_AVAILABLE: no Robinhood tool provides a bid/ask spread history."
        )
        result = validate(
            buy_decision(
                self.state, "10.00",
                research_package=package, monthly_optionality=optionality(7),
            ),
            self.config, self.state, now=at(7),
        )
        self.assertNotIn("RESEARCH_GAP_NOT_JUSTIFIED", result.violation_codes)


class CryptoWaitBucketTests(unittest.TestCase):
    """A WAIT may not write crypto off as unresearchable because of the broker."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def wait_with_bucket(self, bucket, value):
        decision = wait_decision(self.state)
        decision[bucket] = value
        return validate(decision, self.config, self.state, now=at(7))

    def test_research_incomplete_blamed_on_the_broker_is_rejected(self):
        for bucket in ("best_existing_crypto_candidate", "best_new_crypto_candidate"):
            with self.subTest(bucket=bucket):
                result = self.wait_with_bucket(
                    bucket,
                    {
                        "symbol": "LINK-USD",
                        "classification": "RESEARCH_INCOMPLETE",
                        "why_not": "Supply, network usage and ecosystem development are "
                                   "not available from Robinhood, so no package can be "
                                   "assembled.",
                    },
                )
                self.assertFalse(result.valid)
                self.assertIn("RESEARCH_GAP_NOT_JUSTIFIED", result.violation_codes)

    def test_not_applicable_blamed_on_the_broker_is_rejected(self):
        result = self.wait_with_bucket(
            "best_new_crypto_candidate",
            "NOT_APPLICABLE: the connected read-only tools do not provide the research "
            "fields a crypto package needs.",
        )
        self.assertFalse(result.valid)
        self.assertIn("RESEARCH_GAP_NOT_JUSTIFIED", result.violation_codes)

    def test_research_incomplete_for_a_real_reason_is_accepted(self):
        result = self.wait_with_bucket(
            "best_new_crypto_candidate",
            {
                "symbol": "LINK-USD",
                "classification": "RESEARCH_INCOMPLETE",
                "why_not": "The protocol's own documentation was reviewed, but no "
                           "reputable provider publishes a dated fee-revenue series "
                           "for the oracle network, so the usage component is still "
                           "open. Deferred rather than guessed at.",
            },
        )
        self.assertTrue(result.valid, result.violation_codes)

    def test_a_bucket_rejected_on_merit_is_untouched(self):
        """Mentioning Robinhood is fine; using it as the excuse is not."""
        result = self.wait_with_bucket(
            "best_existing_crypto_candidate",
            {
                "symbol": "BTC-USD",
                "classification": "HOLD_NO_ADD",
                "why_not": "Robinhood shows the position 8.1% above cost, and crypto is "
                           "already 35.9% of household value. Adding worsens "
                           "concentration for no improvement in expected return.",
            },
        )
        self.assertTrue(result.valid, result.violation_codes)

    def test_the_check_runs_on_every_wait(self):
        result = validate(wait_decision(self.state), self.config, self.state, now=at(7))
        self.assertIn("crypto_buckets_not_dismissed_on_broker_scope", result.checks_run)


class LessonsAreAdvisoryTests(unittest.TestCase):
    """A lesson informs a decision. It never authorises one.

    The learning layer's whole risk is that history becomes permission. These
    are the checks that stop it: a lesson may not authorise a purchase, satisfy
    a research requirement, or relax a guardrail.
    """

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config)

    def buy(self, **overrides):
        decision = buy_decision(self.state, "10.00", monthly_optionality=optionality(7))
        decision.update(overrides)
        return decision

    def test_a_lesson_authorization_field_is_rejected(self):
        from src.lessons import LESSON_MISUSE_FIELDS

        for field in LESSON_MISUSE_FIELDS:
            with self.subTest(field=field):
                result = validate(
                    self.buy(**{field: "L-001"}), self.config, self.state, now=at(7))
                self.assertFalse(result.valid, "%s was accepted" % field)
                self.assertIn("LESSON_MISUSED_AS_AUTHORIZATION", result.violation_codes)

    def test_the_violation_says_to_cite_it_as_reasoning_instead(self):
        result = validate(
            self.buy(lesson_authorizes="L-001"), self.config, self.state, now=at(7))
        message = next(v.message for v in result.violations
                       if v.code == "LESSON_MISUSED_AS_AUTHORIZATION")
        self.assertIn("advisory", message)
        self.assertIn("thesis", message)

    def test_a_thesis_claiming_a_precedent_authorises_is_rejected(self):
        result = validate(
            self.buy(thesis="The lesson authorizes this addition without more work."),
            self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("LESSON_MISUSED_AS_AUTHORIZATION", result.violation_codes)

    def test_it_worked_last_time_is_not_a_thesis(self):
        result = validate(
            self.buy(timing_reason="Buy now because it worked last time."),
            self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("LESSON_MISUSED_AS_AUTHORIZATION", result.violation_codes)

    def test_a_research_component_answered_by_a_lesson_is_rejected(self):
        package = research_package("etf")
        package["overlap_with_existing_holdings"] = "See research/lessons/L-004.md"
        result = validate(
            self.buy(research_package=package), self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("LESSON_CANNOT_SATISFY_RESEARCH", result.violation_codes)

    def test_that_violation_explains_the_category_error(self):
        package = research_package("etf")
        package["expense_ratio"] = "LESSON: index funds are cheap"
        result = validate(
            self.buy(research_package=package), self.config, self.state, now=at(7))
        message = next(v.message for v in result.violations
                       if v.code == "LESSON_CANNOT_SATISFY_RESEARCH")
        self.assertIn("conclusion about past decisions", message)
        self.assertIn("not", message)

    def test_citing_a_lesson_descriptively_is_fine(self):
        """Learning from history is the point; using it as permission is not."""
        result = validate(
            self.buy(thesis=(
                "Cost basis has risen across every prior lot in this name, which "
                "is the habitual pattern the history shows; the case here rests "
                "on expected return at today's price rather than on that pattern."
            )),
            self.config, self.state, now=at(7))
        self.assertTrue(result.valid, result.violation_codes)

    def test_the_check_runs_on_every_decision_including_wait(self):
        result = validate(wait_decision(self.state), self.config, self.state, now=at(7))
        self.assertIn("lessons_are_advisory_only", result.checks_run)
        self.assertIn("research_not_satisfied_by_a_lesson", result.checks_run)

    def test_a_wait_may_not_be_authorised_by_a_lesson_either(self):
        decision = wait_decision(self.state)
        decision["precedent_authorizes"] = "L-002"
        result = validate(decision, self.config, self.state, now=at(7))
        self.assertFalse(result.valid)
        self.assertIn("LESSON_MISUSED_AS_AUTHORIZATION", result.violation_codes)
