"""Approval, state machine, and pre-execution tests.

Every broker interaction is a synthetic dict. No Robinhood tool is called, no
order is placed, and `src.execution.execute` is expected to raise in every path.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.approval import (  # noqa: E402
    ApprovalError,
    ApprovalRecord,
    binding_view,
    broker_ref_id,
    canonical_payload,
    create_approval,
    fingerprint,
    policy_fingerprint,
    verify_approval,
)
from src.execution import (  # noqa: E402
    ALL_STATES,
    IN_FLIGHT_STATES,
    MAX_CRYPTO_SLIPPAGE_PCT,
    MAX_EQUITY_SLIPPAGE_PCT,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    BrokerSnapshot,
    ExecutionDisabled,
    ExecutionState,
    InvalidTransition,
    assert_transition,
    build_order_request,
    execute,
    execution_gate_blockers,
    preflight,
)
from src.execution_store import (  # noqa: E402
    ExecutionRecord,
    ExecutionStoreError,
    load_executions,
    read_audit,
    save_executions,
    transition,
    write_audit,
)
from src.state import ConfigError, current_month, load_config  # noqa: E402
from src.models import ZERO  # noqa: E402
from tests.helpers import buy_decision, crypto_decision, make_config, make_state  # noqa: E402

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


def month():
    return current_month(NOW)


def live_config(**overrides):
    """A config with all three switches open — for tests only, never shipped."""
    values = dict(
        execution_mode="APPROVAL_REQUIRED", agent_enabled=True, live_trading=True
    )
    values.update(overrides)
    return make_config(**values)


def equity_decision(amount="10.00", **overrides):
    state = make_state(make_config(), month=month())
    decision = buy_decision(state, amount, decision_id="dec_equity_1",
                            asset_type="us_common_stock", asset_class="EQUITY",
                            ticker="ISRG", security_name="Intuitive Surgical, Inc.",
                            exchange="NASDAQ", current_price_usd="366.67")
    decision["month"] = month()
    decision.update(overrides)
    return decision


def crypto_dec(amount="10.00", **overrides):
    state = make_state(make_config(), month=month())
    decision = crypto_decision(state, amount, decision_id="dec_crypto_1")
    decision["month"] = month()
    decision.update(overrides)
    return decision


def snapshot(**overrides):
    values = dict(
        as_of=NOW,
        account_is_agentic=True,
        account_masked="••••0002",
        equity_orders={"data": {"orders": []}},
        crypto_orders={"data": {"results": []}},
        buying_power_usd=Decimal("100.00"),
        cash_usd=Decimal("100.00"),
        unsettled_funds_usd=Decimal("0.00"),
        account_type="limited_margin",
        quote_price_usd=Decimal("366.67"),
        quote_timestamp=NOW - timedelta(seconds=30),
        tradable=True,
        fractional_tradable=True,
        account_type_tradable=True,
    )
    values.update(overrides)
    return BrokerSnapshot(**values)


def crypto_snapshot(**overrides):
    values = dict(
        quote_price_usd=Decimal("80000.00"),
        tradable=None, fractional_tradable=None, account_type_tradable=None,
        crypto_pair_halted=False,
        crypto_min_order_size=Decimal("0.000001"),
    )
    values.update(overrides)
    return snapshot(**values)


def order(order_id, amount, state="filled", side="buy", day=2, executed=None):
    raw = {
        "id": order_id, "symbol": "VTI", "side": side, "state": state,
        "created_at": "%s-%02dT15:00:00Z" % (month(), day),
        "dollar_based_amount": {"amount": amount, "currency_code": "USD"},
        "cumulative_quantity": "0", "average_price": None,
    }
    if executed:
        raw["cumulative_quantity"], raw["average_price"] = executed
    elif state == "filled":
        raw["cumulative_quantity"], raw["average_price"] = "1", amount
    return raw


_UNSET = object()


def run(decision, approval, config=None, state=None, snap=_UNSET,
        exec_state=ExecutionState.PROPOSED, siblings=ZERO):
    """Pass snap=None explicitly to simulate an unreadable broker."""
    return preflight(
        decision, approval,
        config or live_config(),
        state or make_state(make_config(), month=month()),
        snapshot() if snap is _UNSET else snap,
        execution_state=exec_state, now=NOW,
        sibling_reservations_usd=siblings,
    )


def approve(decision, **kwargs):
    kwargs.setdefault("approved_by", "test")
    return create_approval(decision, now=NOW, **kwargs)


# ==========================================================================


class ShippedConfigTests(unittest.TestCase):
    def test_the_shipped_config_is_dry_run_with_the_agent_disabled(self):
        config = load_config()
        self.assertEqual(config.execution_mode, "DRY_RUN")
        self.assertFalse(config.agent_enabled)
        self.assertFalse(config.live_trading)

    def test_all_three_switches_are_closed_today(self):
        codes = [c for c, _ in execution_gate_blockers(load_config())]
        self.assertIn("AGENT_DISABLED", codes)
        self.assertIn("EXECUTION_MODE_DRY_RUN", codes)
        self.assertIn("LIVE_TRADING_DISABLED", codes)

    def test_a_malformed_execution_mode_fails_closed(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open("config.json", encoding="utf-8") as handle:
            base = json.load(handle)
        for bad in ("live", "AUTO", "", None, 7, "dry_run", "APPROVAL REQUIRED"):
            with self.subTest(mode=bad):
                base["execution_mode"] = bad
                path = os.path.join(tmp, "c.json")
                with open(path, "w") as handle:
                    json.dump(base, handle)
                with self.assertRaises(ConfigError):
                    load_config(path)


class SwitchTests(unittest.TestCase):
    def test_kill_switch_alone_blocks_execution(self):
        config = live_config(agent_enabled=False)
        result = run(equity_decision(), approve(equity_decision()), config=config)
        self.assertFalse(result.ok)
        self.assertIn("AGENT_DISABLED", result.codes)

    def test_dry_run_mode_blocks_execution(self):
        config = live_config(execution_mode="DRY_RUN")
        result = run(equity_decision(), approve(equity_decision()), config=config)
        self.assertFalse(result.ok)
        self.assertIn("EXECUTION_MODE_DRY_RUN", result.codes)

    def test_autonomous_is_refused_unconditionally(self):
        config = live_config(execution_mode="AUTONOMOUS")
        result = run(equity_decision(), approve(equity_decision()), config=config)
        self.assertFalse(result.ok)
        self.assertIn("AUTONOMOUS_NOT_IMPLEMENTED", result.codes)

    def test_live_trading_interlock_alone_blocks_execution(self):
        config = live_config(live_trading=False)
        result = run(equity_decision(), approve(equity_decision()), config=config)
        self.assertFalse(result.ok)
        self.assertIn("LIVE_TRADING_DISABLED", result.codes)

    def test_opening_only_two_of_three_switches_is_not_enough(self):
        for closed in ("agent_enabled", "live_trading"):
            with self.subTest(closed=closed):
                config = live_config(**{closed: False})
                result = run(equity_decision(), approve(equity_decision()), config=config)
                self.assertFalse(result.ok)

    def test_execute_always_raises_regardless_of_switches(self):
        with self.assertRaises(ExecutionDisabled):
            execute()


class ApprovalRequiredTests(unittest.TestCase):
    def test_an_unapproved_buy_cannot_execute(self):
        result = run(equity_decision(), None)
        self.assertFalse(result.ok)
        self.assertIn("NOT_APPROVED", result.codes)

    def test_an_approved_exact_decision_reaches_pre_execution_validated(self):
        decision = equity_decision()
        result = run(decision, approve(decision))
        self.assertTrue(result.ok, result.blockers)
        self.assertEqual(result.next_state, ExecutionState.PRE_EXECUTION_VALIDATED)

    def test_a_decision_cannot_approve_itself(self):
        for key in ("approved", "approval", "approved_by", "user_approved", "execution_state"):
            with self.subTest(key=key):
                decision = equity_decision(**{key: True})
                result = run(decision, approve(equity_decision()))
                self.assertFalse(result.ok)
                self.assertIn("SELF_APPROVAL_ATTEMPTED", result.codes)

    def test_only_a_buy_can_be_approved(self):
        with self.assertRaises(ApprovalError):
            approve(equity_decision(side="sell", action="sell"))


class ApprovalBindingTests(unittest.TestCase):
    """One approval authorizes exactly one thing."""

    def setUp(self):
        self.decision = equity_decision("10.00")
        self.approval = approve(self.decision)

    def assert_blocked(self, changed, *expected_codes):
        result = run(changed, self.approval)
        self.assertFalse(result.ok)
        for code in expected_codes:
            self.assertIn(code, result.codes)
        return result

    def test_a_different_ticker_fails(self):
        self.assert_blocked(
            equity_decision("10.00", ticker="MU", security_name="Micron"),
            "DECISION_MODIFIED", "APPROVAL_FIELD_MISMATCH",
        )

    def test_a_different_crypto_asset_fails(self):
        changed = crypto_dec("10.00", decision_id=self.decision["decision_id"])
        self.assert_blocked(changed, "DECISION_MODIFIED", "APPROVAL_FIELD_MISMATCH")

    def test_an_increased_amount_fails(self):
        result = self.assert_blocked(
            equity_decision("15.00"), "DECISION_MODIFIED", "AMOUNT_EXCEEDS_APPROVAL"
        )
        self.assertTrue(any("10.00" in m for _, m in result.blockers))

    def test_even_one_cent_more_fails(self):
        self.assert_blocked(equity_decision("10.01"), "AMOUNT_EXCEEDS_APPROVAL")

    def test_a_changed_asset_class_fails(self):
        self.assert_blocked(
            equity_decision("10.00", asset_class="ETF", asset_type="us_etf"),
            "DECISION_MODIFIED", "APPROVAL_FIELD_MISMATCH",
        )

    def test_a_changed_decision_id_fails(self):
        self.assert_blocked(
            equity_decision("10.00", decision_id="dec_other"),
            "APPROVAL_DECISION_ID_MISMATCH", "DECISION_MODIFIED",
        )

    def test_a_changed_month_fails(self):
        self.assert_blocked(equity_decision("10.00", month="2026-01"), "DECISION_MODIFIED")

    def test_a_changed_position_type_changes_the_hash(self):
        self.assert_blocked(
            equity_decision("10.00", position_type="EXISTING_POSITION"), "DECISION_MODIFIED"
        )

    def test_a_sell_side_fails(self):
        self.assert_blocked(equity_decision("10.00", side="sell"), "DECISION_MODIFIED")

    def test_the_same_decision_still_verifies(self):
        self.assertTrue(verify_approval(self.approval, self.decision, now=NOW).ok)

    def test_narrative_edits_do_not_invalidate_approval(self):
        """Only the binding fields bind. Rewording a thesis is not a material change."""
        reworded = dict(self.decision, thesis="A completely different sentence entirely.")
        self.assertTrue(verify_approval(self.approval, reworded, now=NOW).ok)


class ApprovalExpiryTests(unittest.TestCase):
    def test_an_expired_approval_fails(self):
        decision = equity_decision()
        approval = approve(decision)
        later = NOW + timedelta(hours=25)
        verdict = verify_approval(approval, decision, now=later)
        self.assertFalse(verdict.ok)
        self.assertIn("APPROVAL_EXPIRED", verdict.codes)

    def test_the_default_ttl_is_twenty_four_hours(self):
        approval = approve(equity_decision())
        self.assertEqual(approval.expires_at, "2026-09-21T15:00:00Z")

    def test_a_decision_may_shorten_but_never_lengthen_the_ttl(self):
        short = approve(equity_decision(approval_ttl_hours=2))
        self.assertEqual(short.expires_at, "2026-09-20T17:00:00Z")
        long = approve(equity_decision(approval_ttl_hours=999))
        self.assertEqual(long.expires_at, "2026-09-21T15:00:00Z")

    def test_approval_expires_when_the_calendar_month_changes(self):
        decision = equity_decision()
        approval = approve(decision)
        # 06:00Z on the 1st is 01:00 in America/Chicago — October by any clock.
        next_month = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
        verdict = verify_approval(approval, decision, now=next_month)
        self.assertFalse(verdict.ok)
        self.assertIn("APPROVAL_MONTH_ROLLED_OVER", verdict.codes)

    def test_the_month_boundary_follows_the_project_clock_not_utc(self):
        """00:30Z on the 1st is still the 30th at 19:30 in Chicago.

        Rolling the authorization month on UTC would charge a purchase made on
        the evening of the last day to the *following* month's budget, and
        would expire an approval that is still validly inside its month.
        """
        decision = equity_decision()
        approval = approve(decision)
        still_september = datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)
        verdict = verify_approval(approval, decision, now=still_september)
        self.assertNotIn("APPROVAL_MONTH_ROLLED_OVER", verdict.codes)

    def test_approval_expires_when_the_policy_surface_changes(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ("config.json", "INVESTMENT_POLICY.md"):
            shutil.copy(name, os.path.join(tmp, name))
        os.makedirs(os.path.join(tmp, "src"), exist_ok=True)
        for name in ("guardrails.py", "models.py"):
            shutil.copy(os.path.join("src", name), os.path.join(tmp, "src", name))

        decision = equity_decision()
        approval = approve(decision)
        approval.policy_fingerprint = policy_fingerprint(tmp)  # as if approved under old rules
        with open(os.path.join(tmp, "INVESTMENT_POLICY.md"), "a") as handle:
            handle.write("\n<!-- a policy edit -->\n")
        verdict = verify_approval(approval, decision, now=NOW, repo_root=tmp)
        self.assertFalse(verdict.ok)
        self.assertIn("POLICY_CHANGED", verdict.codes)


class IdempotencyTests(unittest.TestCase):
    def test_a_second_run_after_submission_is_refused(self):
        decision = equity_decision()
        approval = approve(decision)
        for state in (ExecutionState.SUBMITTED, ExecutionState.PARTIALLY_FILLED,
                      ExecutionState.FILLED):
            with self.subTest(state=state):
                result = run(decision, approval, exec_state=state)
                self.assertFalse(result.ok)
                self.assertIn("ALREADY_SUBMITTED", result.codes)

    def test_an_uncertain_submission_demands_reconciliation_not_a_retry(self):
        decision = equity_decision()
        result = run(decision, approve(decision),
                     exec_state=ExecutionState.SUBMISSION_UNCERTAIN)
        self.assertFalse(result.ok)
        self.assertIn("RECONCILIATION_REQUIRED", result.codes)
        self.assertNotIn("ALREADY_SUBMITTED", result.codes)

    def test_a_decision_id_already_in_the_budget_ledger_is_refused(self):
        decision = equity_decision()
        state = make_state(make_config(), month=month(), acted=[decision["decision_id"]])
        result = run(decision, approve(decision), state=state)
        self.assertFalse(result.ok)
        self.assertIn("DUPLICATE_DECISION_ID", result.codes)

    def test_terminal_states_cannot_be_re_executed(self):
        decision = equity_decision()
        for state in sorted(TERMINAL_STATES):
            with self.subTest(state=state):
                result = run(decision, approve(decision), exec_state=state)
                self.assertFalse(result.ok)
                self.assertIn("TERMINAL_STATE", result.codes)

    def test_the_broker_ref_id_is_deterministic_from_the_decision_id(self):
        first = broker_ref_id("dec_abc")
        self.assertEqual(first, broker_ref_id("dec_abc"))
        self.assertNotEqual(first, broker_ref_id("dec_abd"))

    def test_a_rebuilt_order_reuses_the_same_ref_id(self):
        decision = equity_decision()
        one = build_order_request(decision, snapshot(), "acct")
        two = build_order_request(decision, snapshot(), "acct")
        self.assertEqual(one["ref_id"], two["ref_id"])


class ReconciliationGateTests(unittest.TestCase):
    def test_pending_orders_reduce_available_authorization(self):
        decision = equity_decision("20.00")
        snap = snapshot(equity_orders={"data": {"orders": [
            order("o1", "10.00", state="confirmed"),
        ]}})
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("EXCEEDS_RECONCILED_AUTHORIZATION", result.codes)
        self.assertEqual(result.max_executable_usd, Decimal("15.00"))

    def test_partial_fills_reduce_authorization(self):
        decision = equity_decision("10.00")
        snap = snapshot(equity_orders={"data": {"orders": [
            order("o1", "20.00", state="partially_filled", executed=("0.01", "500.00")),
        ]}})
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertEqual(result.max_executable_usd, Decimal("5.00"))

    def test_a_manual_agentic_purchase_reduces_authorization(self):
        decision = equity_decision("10.00")
        snap = snapshot(equity_orders={"data": {"orders": [
            order("manual", "20.00", state="filled", executed=("0.04", "500.00")),
        ]}})
        state = make_state(make_config(), month=month(), committed="0.00")
        result = run(decision, approve(decision), state=state, snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("EXCEEDS_RECONCILED_AUTHORIZATION", result.codes)
        self.assertEqual(result.max_executable_usd, Decimal("5.00"))

    def test_monthly_authorization_exceeded_fails(self):
        decision = equity_decision("10.00")
        state = make_state(make_config(), month=month(), committed="20.00")
        result = run(decision, approve(decision), state=state)
        self.assertFalse(result.ok)
        self.assertIn("EXCEEDS_RECONCILED_AUTHORIZATION", result.codes)

    def test_an_exactly_fitting_order_passes(self):
        decision = equity_decision("5.00")
        state = make_state(make_config(), month=month(), committed="20.00")
        result = run(decision, approve(decision), state=state)
        self.assertTrue(result.ok, result.blockers)

    def test_a_malformed_broker_response_fails_closed(self):
        decision = equity_decision()
        for bad in ({"unexpected": True}, "orders", 42):
            with self.subTest(payload=bad):
                result = run(decision, approve(decision), snap=snapshot(equity_orders=bad))
                self.assertFalse(result.ok)
                self.assertIn("RECONCILIATION_FAILED", result.codes)

    def test_an_unavailable_broker_read_fails_closed(self):
        decision = equity_decision()
        result = run(decision, approve(decision), snap=None)
        self.assertFalse(result.ok)
        self.assertIn("BROKER_UNREADABLE", result.codes)

    def test_reported_read_errors_fail_closed(self):
        decision = equity_decision()
        snap = snapshot(read_errors=["get_equity_orders timed out"])
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("BROKER_UNREADABLE", result.codes)

    def test_the_wrong_account_fails(self):
        decision = equity_decision()
        result = run(decision, approve(decision), snap=snapshot(account_is_agentic=False))
        self.assertFalse(result.ok)
        self.assertIn("WRONG_ACCOUNT", result.codes)

    def test_a_sell_in_the_agentic_account_blocks_everything(self):
        decision = equity_decision("5.00")
        snap = snapshot(equity_orders={"data": {"orders": [
            order("s1", "50.00", state="filled", side="sell", executed=("0.1", "500.00")),
        ]}})
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("RECONCILIATION_BLOCKING", result.codes)

    def test_a_stale_month_in_local_state_fails(self):
        decision = equity_decision()
        state = make_state(make_config(), month="2026-08")
        result = run(decision, approve(decision), state=state)
        self.assertFalse(result.ok)
        self.assertIn("MONTH_ROLLED_OVER", result.codes)


class FundsAndTradabilityTests(unittest.TestCase):
    def test_insufficient_buying_power_fails(self):
        decision = equity_decision("10.00")
        result = run(decision, approve(decision),
                     snap=snapshot(buying_power_usd=Decimal("3.00"), cash_usd=Decimal("3.00")))
        self.assertFalse(result.ok)
        self.assertIn("INSUFFICIENT_BUYING_POWER", result.codes)

    def test_unknown_buying_power_fails_closed(self):
        decision = equity_decision()
        result = run(decision, approve(decision), snap=snapshot(buying_power_usd=None))
        self.assertFalse(result.ok)
        self.assertIn("BUYING_POWER_UNKNOWN", result.codes)


class CashOnlyTests(unittest.TestCase):
    """Execution is cash-only. Buying power is never the funding basis."""

    def test_unknown_cash_fails_closed(self):
        decision = equity_decision("10.00")
        result = run(decision, approve(decision), snap=snapshot(cash_usd=None))
        self.assertFalse(result.ok)
        self.assertIn("CASH_UNKNOWN", result.codes)

    def test_margin_buying_power_cannot_fund_an_order(self):
        """The real shape of the risk: a limited_margin account with $5 cash and
        $2000 of buying power must not be allowed to buy $25."""
        decision = equity_decision("25.00")
        snap = snapshot(cash_usd=Decimal("5.00"), buying_power_usd=Decimal("2000.00"),
                        account_type="limited_margin")
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("INSUFFICIENT_SETTLED_CASH", result.codes)
        self.assertIn("MARGIN_RISK", result.codes)

    def test_unsettled_funds_are_excluded_from_spendable_cash(self):
        decision = equity_decision("10.00")
        snap = snapshot(cash_usd=Decimal("12.00"), unsettled_funds_usd=Decimal("8.00"))
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("INSUFFICIENT_SETTLED_CASH", result.codes)

    def test_settled_cash_exactly_covering_the_order_passes(self):
        decision = equity_decision("10.00")
        snap = snapshot(cash_usd=Decimal("10.00"), unsettled_funds_usd=Decimal("0.00"),
                        buying_power_usd=Decimal("10.00"))
        result = run(decision, approve(decision), snap=snap)
        self.assertTrue(result.ok, result.blockers)

    def test_partially_unsettled_cash_still_works_if_settled_portion_covers_it(self):
        decision = equity_decision("10.00")
        snap = snapshot(cash_usd=Decimal("30.00"), unsettled_funds_usd=Decimal("15.00"))
        result = run(decision, approve(decision), snap=snap)
        self.assertTrue(result.ok, result.blockers)
        self.assertTrue(any("unsettled" in w for w in result.warnings))

    def test_a_cash_account_needs_no_margin_warning(self):
        decision = equity_decision("10.00")
        snap = snapshot(account_type="cash", cash_usd=Decimal("50.00"))
        result = run(decision, approve(decision), snap=snap)
        self.assertTrue(result.ok, result.blockers)
        self.assertNotIn("MARGIN_RISK", result.codes)

    def test_a_zero_cash_account_can_never_execute(self):
        """Today's actual state: the Agentic account holds $0.00."""
        decision = equity_decision("5.00")
        snap = snapshot(cash_usd=Decimal("0.00"), buying_power_usd=Decimal("0.00"))
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("INSUFFICIENT_SETTLED_CASH", result.codes)

    def test_a_missing_account_type_warns_but_cash_still_binds(self):
        decision = equity_decision("10.00")
        snap = snapshot(account_type=None, cash_usd=Decimal("2.00"))
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("INSUFFICIENT_SETTLED_CASH", result.codes)

    def test_an_untradable_security_fails(self):
        decision = equity_decision()
        result = run(decision, approve(decision), snap=snapshot(tradable=False))
        self.assertFalse(result.ok)
        self.assertIn("SECURITY_UNTRADABLE", result.codes)

    def test_a_security_untradable_in_this_account_type_fails(self):
        decision = equity_decision()
        result = run(decision, approve(decision), snap=snapshot(account_type_tradable=False))
        self.assertFalse(result.ok)
        self.assertIn("SECURITY_UNTRADABLE", result.codes)

    def test_unknown_tradability_fails_closed(self):
        decision = equity_decision()
        result = run(decision, approve(decision), snap=snapshot(tradable=None))
        self.assertFalse(result.ok)
        self.assertIn("TRADABILITY_UNKNOWN", result.codes)

    def test_a_halted_crypto_pair_fails(self):
        decision = crypto_dec()
        result = run(decision, approve(decision), snap=crypto_snapshot(crypto_pair_halted=True))
        self.assertFalse(result.ok)
        self.assertIn("SECURITY_UNTRADABLE", result.codes)

    def test_below_the_crypto_minimum_order_size_fails(self):
        decision = crypto_dec("2.00")
        snap = crypto_snapshot(crypto_min_order_size=Decimal("1000"),
                               quote_price_usd=Decimal("0.01"))
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("BELOW_MIN_ORDER_SIZE", result.codes)

    def test_below_the_equity_broker_minimum_fails(self):
        decision = equity_decision("0.50")
        result = run(decision, approve(decision))
        self.assertFalse(result.ok)
        self.assertIn("BELOW_BROKER_MINIMUM", result.codes)


class FreshnessAndSlippageTests(unittest.TestCase):
    def test_a_stale_equity_quote_forces_reevaluation(self):
        decision = equity_decision()
        snap = snapshot(quote_timestamp=NOW - timedelta(minutes=30))
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("STALE_QUOTE", result.codes)
        self.assertEqual(result.next_state, ExecutionState.REEVALUATION_REQUIRED)

    def test_crypto_has_a_tighter_freshness_window(self):
        decision = crypto_dec()
        snap = crypto_snapshot(quote_timestamp=NOW - timedelta(seconds=120))
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("STALE_QUOTE", result.codes)
        # the same age is fine for an equity
        eq = equity_decision()
        eq_result = run(eq, approve(eq), snap=snapshot(quote_timestamp=NOW - timedelta(seconds=120)))
        self.assertTrue(eq_result.ok, eq_result.blockers)

    def test_a_missing_quote_fails_closed(self):
        decision = equity_decision()
        result = run(decision, approve(decision), snap=snapshot(quote_price_usd=None))
        self.assertFalse(result.ok)
        self.assertIn("STALE_QUOTE", result.codes)

    def test_a_large_equity_price_move_requires_reapproval(self):
        decision = equity_decision()  # priced at 366.67
        snap = snapshot(quote_price_usd=Decimal("400.00"))  # +9.1%
        result = run(decision, approve(decision), snap=snap)
        self.assertFalse(result.ok)
        self.assertIn("PRICE_MOVED_BEYOND_TOLERANCE", result.codes)
        self.assertEqual(result.next_state, ExecutionState.REAPPROVAL_REQUIRED)

    def test_a_large_downward_move_also_requires_reapproval(self):
        decision = equity_decision()
        result = run(decision, approve(decision), snap=snapshot(quote_price_usd=Decimal("300.00")))
        self.assertFalse(result.ok)
        self.assertIn("PRICE_MOVED_BEYOND_TOLERANCE", result.codes)

    def test_a_small_equity_move_passes_with_a_warning(self):
        decision = equity_decision()
        snap = snapshot(quote_price_usd=Decimal("372.00"))  # +1.45%
        result = run(decision, approve(decision), snap=snap)
        self.assertTrue(result.ok, result.blockers)
        self.assertTrue(any("price moved" in w for w in result.warnings))

    def test_crypto_tolerates_more_movement_than_equity(self):
        decision = crypto_dec()  # priced at 80000
        snap = crypto_snapshot(quote_price_usd=Decimal("82400.00"))  # +3.0%
        self.assertTrue(run(decision, approve(decision), snap=snap).ok)
        far = crypto_snapshot(quote_price_usd=Decimal("88000.00"))  # +10%
        self.assertIn("PRICE_MOVED_BEYOND_TOLERANCE",
                      run(decision, approve(decision), snap=far).codes)

    def test_the_thresholds_are_what_the_policy_says(self):
        self.assertEqual(MAX_EQUITY_SLIPPAGE_PCT, Decimal("2.0"))
        self.assertEqual(MAX_CRYPTO_SLIPPAGE_PCT, Decimal("5.0"))


class ForbiddenActionTests(unittest.TestCase):
    """Nothing outside a long BUY can reach execution."""

    def test_a_sell_cannot_reach_execution(self):
        decision = equity_decision(side="sell", action="sell")
        result = run(decision, None)
        self.assertFalse(result.ok)
        with self.assertRaises(ApprovalError):
            approve(decision)
        with self.assertRaises(ExecutionDisabled):
            build_order_request(decision, snapshot(), "acct")

    def test_an_option_cannot_reach_execution(self):
        decision = equity_decision(asset_type="option", uses_options=True,
                                   strike_price="400", expiration_date="2027-01-15")
        result = run(decision, approve(equity_decision()))
        self.assertFalse(result.ok)

    def test_a_transfer_cannot_reach_execution(self):
        decision = equity_decision(action="withdraw", is_transfer=True)
        result = run(decision, approve(equity_decision()))
        self.assertFalse(result.ok)
        with self.assertRaises(ApprovalError):
            approve(decision)

    def test_margin_and_shorting_never_produce_an_order(self):
        for override in ({"uses_margin": True}, {"is_short": True}):
            with self.subTest(override=override):
                decision = equity_decision(**override)
                approval = approve(equity_decision())
                result = run(decision, approval)
                self.assertFalse(result.ok)


class OrderConstructionTests(unittest.TestCase):
    """What WOULD be sent, derived from the real tool schemas. Nothing is sent."""

    def test_equity_order_is_a_dollar_based_market_order_in_regular_hours(self):
        request = build_order_request(equity_decision("10.00"), snapshot(), "ACCT")
        self.assertEqual(request["_tool"], "place_equity_order")
        self.assertEqual(request["type"], "market")
        self.assertEqual(request["dollar_amount"], "10.00")
        self.assertEqual(request["market_hours"], "regular_hours")
        self.assertEqual(request["side"], "buy")
        self.assertNotIn("limit_price", request)
        self.assertIn("ref_id", request)

    def test_crypto_order_is_a_dollar_based_limit_order(self):
        request = build_order_request(crypto_dec("10.00"), crypto_snapshot(), "RHS")
        self.assertEqual(request["_tool"], "place_crypto_order")
        self.assertEqual(request["type"], "limit")
        self.assertEqual(request["dollar_amount"], "10.00")
        self.assertIn("limit_price", request)
        self.assertEqual(request["time_in_force"], "gtc")
        self.assertGreater(Decimal(request["limit_price"]), Decimal("80000.00"))

    def test_a_crypto_order_needs_a_quote_to_price_the_limit(self):
        with self.assertRaises(ExecutionDisabled):
            build_order_request(crypto_dec(), crypto_snapshot(quote_price_usd=None), "RHS")

    def test_no_constructed_order_is_ever_actually_sent(self):
        build_order_request(equity_decision(), snapshot(), "ACCT")
        with self.assertRaises(ExecutionDisabled):
            execute()


class StateMachineTests(unittest.TestCase):
    def test_the_happy_path_transitions_are_permitted(self):
        path = [ExecutionState.PROPOSED, ExecutionState.APPROVED,
                ExecutionState.PRE_EXECUTION_VALIDATED, ExecutionState.SUBMITTED,
                ExecutionState.PARTIALLY_FILLED, ExecutionState.FILLED]
        for current, target in zip(path, path[1:]):
            assert_transition(current, target)

    def test_skipping_approval_is_impossible(self):
        for target in (ExecutionState.PRE_EXECUTION_VALIDATED, ExecutionState.SUBMITTED,
                       ExecutionState.FILLED):
            with self.subTest(target=target):
                with self.assertRaises(InvalidTransition):
                    assert_transition(ExecutionState.PROPOSED, target)

    def test_terminal_states_permit_nothing(self):
        for state in sorted(TERMINAL_STATES):
            for target in ALL_STATES:
                with self.assertRaises(InvalidTransition):
                    assert_transition(state, target)

    def test_an_uncertain_submission_cannot_be_resubmitted_by_transition(self):
        assert_transition(ExecutionState.SUBMISSION_UNCERTAIN, ExecutionState.FILLED)
        with self.assertRaises(InvalidTransition):
            assert_transition(ExecutionState.SUBMISSION_UNCERTAIN,
                              ExecutionState.PRE_EXECUTION_VALIDATED)

    def test_unknown_states_are_rejected(self):
        with self.assertRaises(InvalidTransition):
            assert_transition("MADE_UP", ExecutionState.FILLED)
        with self.assertRaises(InvalidTransition):
            assert_transition(ExecutionState.PROPOSED, "MADE_UP")

    def test_every_in_flight_state_is_treated_as_possibly_live(self):
        self.assertIn(ExecutionState.SUBMISSION_UNCERTAIN, IN_FLIGHT_STATES)
        self.assertIn(ExecutionState.SUBMITTED, IN_FLIGHT_STATES)


class ExecutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.exec_path = os.path.join(self.tmp, "executions.json")
        self.audit_path = os.path.join(self.tmp, "audit.jsonl")

    def test_records_round_trip(self):
        record = ExecutionRecord(decision_id="d1", month=month(), asset="ISRG")
        save_executions({"d1": record}, self.exec_path)
        back = load_executions(self.exec_path)
        self.assertEqual(back["d1"].state, ExecutionState.PROPOSED)

    def test_a_corrupt_execution_ledger_fails_closed(self):
        with open(self.exec_path, "w") as handle:
            handle.write("{not json")
        with self.assertRaises(ExecutionStoreError):
            load_executions(self.exec_path)

    def test_an_unknown_state_in_the_ledger_fails_closed(self):
        with open(self.exec_path, "w") as handle:
            json.dump({"executions": {"d1": {"decision_id": "d1", "state": "TELEPORTED"}}}, handle)
        with self.assertRaises(ExecutionStoreError):
            load_executions(self.exec_path)

    def test_transition_enforces_the_state_machine(self):
        record = ExecutionRecord(decision_id="d1")
        with self.assertRaises(InvalidTransition):
            transition(record, ExecutionState.SUBMITTED, audit_path=self.audit_path)
        # An APPROVED record must carry the approval that authorized it.
        transition(record, ExecutionState.APPROVED, audit_path=self.audit_path,
                   approval_id="apr_test")
        self.assertEqual(record.state, ExecutionState.APPROVED)
        self.assertEqual(len(record.history), 1)

    def test_the_audit_log_is_append_only_and_redacted(self):
        write_audit("TEST", decision_id="d1",
                    detail={"account_number": "123456789", "auth_token": "s3cret", "ok": True},
                    path=self.audit_path)
        write_audit("TEST2", decision_id="d1", detail={"ok": False}, path=self.audit_path)
        events = read_audit(self.audit_path)
        self.assertEqual(len(events), 2)
        with open(self.audit_path, encoding="utf-8") as handle:
            blob = handle.read()
        self.assertNotIn("s3cret", blob)
        self.assertNotIn("123456789", blob)
        self.assertEqual(events[0]["detail"]["account_number"], "[REDACTED]")


class FingerprintTests(unittest.TestCase):
    def test_the_canonical_payload_is_order_independent(self):
        decision = equity_decision()
        shuffled = dict(reversed(list(decision.items())))
        self.assertEqual(canonical_payload(decision), canonical_payload(shuffled))

    def test_every_binding_field_changes_the_fingerprint(self):
        base = equity_decision("10.00")
        original = fingerprint(base)
        changes = [
            {"decision_id": "other"}, {"ticker": "MU"}, {"asset_class": "ETF"},
            {"asset_type": "us_etf"}, {"position_type": "EXISTING_POSITION"},
            {"proposed_amount_usd": "10.01"}, {"side": "sell"}, {"month": "2026-01"},
        ]
        for change in changes:
            with self.subTest(change=change):
                self.assertNotEqual(original, fingerprint(dict(base, **change)))

    def test_amount_normalization_is_stable(self):
        base = equity_decision("10.00")
        self.assertEqual(fingerprint(base), fingerprint(dict(base, proposed_amount_usd="10.000")))
        self.assertEqual(fingerprint(base), fingerprint(dict(base, proposed_amount_usd="$10")))

    def test_a_decision_without_an_id_cannot_be_fingerprinted(self):
        with self.assertRaises(ApprovalError):
            fingerprint({"ticker": "ISRG"})


class EndToEndArchitectureTests(unittest.TestCase):
    """The full mocked path, for both asset classes. Still never executes."""

    def test_equity_buy_architecture_end_to_end(self):
        decision = equity_decision("10.00")
        approval = approve(decision)
        result = run(decision, approval)
        self.assertTrue(result.ok, result.blockers)
        self.assertEqual(result.next_state, ExecutionState.PRE_EXECUTION_VALIDATED)

        record = ExecutionRecord(decision_id=decision["decision_id"])
        transition(record, ExecutionState.APPROVED, audit_path=None,
                   approval_id=approval.approval_id)
        transition(record, ExecutionState.PRE_EXECUTION_VALIDATED, audit_path=None)
        with self.assertRaises(ExecutionDisabled):
            execute()
        self.assertEqual(record.state, ExecutionState.PRE_EXECUTION_VALIDATED)

    def test_crypto_buy_architecture_end_to_end(self):
        decision = crypto_dec("10.00")
        approval = approve(decision)
        result = run(decision, approval, snap=crypto_snapshot())
        self.assertTrue(result.ok, result.blockers)
        request = build_order_request(decision, crypto_snapshot(), "RHS")
        self.assertEqual(request["type"], "limit")
        with self.assertRaises(ExecutionDisabled):
            execute()

    def test_the_shipped_config_can_never_reach_pre_execution_validated(self):
        decision = equity_decision("10.00")
        result = run(decision, approve(decision), config=load_config())
        self.assertFalse(result.ok)
        self.assertNotEqual(result.next_state, ExecutionState.PRE_EXECUTION_VALIDATED)


class SiblingLegReservationTests(unittest.TestCase):
    """Stage 7: a sibling leg already approved holds dollars the broker cannot see."""

    def test_default_behaviour_is_unchanged_when_there_are_no_siblings(self):
        decision = equity_decision("25.00")
        result = run(decision, approve(decision))
        self.assertTrue(result.ok, [c for c, _ in result.blockers])
        self.assertEqual(result.max_executable_usd, Decimal("25.00"))

    def test_an_approved_sibling_reduces_what_this_leg_may_take(self):
        decision = equity_decision("10.00")
        result = run(decision, approve(decision), siblings=Decimal("10.00"))
        self.assertTrue(result.ok, [c for c, _ in result.blockers])
        self.assertEqual(result.max_executable_usd, Decimal("15.00"))

    def test_a_leg_that_only_fits_by_ignoring_its_sibling_is_blocked(self):
        # $15 would fit the bare $25, but a $15 sibling is already approved.
        decision = equity_decision("15.00")
        result = run(decision, approve(decision), siblings=Decimal("15.00"))
        self.assertFalse(result.ok)
        codes = [c for c, _ in result.blockers]
        self.assertIn("EXCEEDS_RECONCILED_AUTHORIZATION", codes)

    def test_the_blocker_message_explains_the_reservation(self):
        decision = equity_decision("15.00")
        result = run(decision, approve(decision), siblings=Decimal("15.00"))
        message = " ".join(m for _, m in result.blockers)
        self.assertIn("sibling plan legs", message)

    def test_two_legs_totalling_exactly_the_authorization_both_fit(self):
        first = equity_decision("15.00")
        self.assertTrue(run(first, approve(first)).ok)
        second = equity_decision("10.00", decision_id="dec_equity_2")
        result = run(second, approve(second), siblings=Decimal("15.00"))
        self.assertTrue(result.ok, [c for c, _ in result.blockers])

    def test_reservations_larger_than_the_authorization_leave_nothing(self):
        decision = equity_decision("1.00")
        result = run(decision, approve(decision), siblings=Decimal("40.00"))
        self.assertFalse(result.ok)
        self.assertEqual(result.max_executable_usd, ZERO)

    def test_a_negative_reservation_is_rejected(self):
        decision = equity_decision("10.00")
        result = run(decision, approve(decision), siblings=Decimal("-5.00"))
        self.assertFalse(result.ok)
        self.assertIn("MALFORMED_NUMBER", [c for c, _ in result.blockers])

    def test_reservation_is_recorded_as_a_warning(self):
        decision = equity_decision("5.00")
        result = run(decision, approve(decision), siblings=Decimal("10.00"))
        self.assertTrue(any("sibling plan legs" in w for w in result.warnings))
        self.assertIn("sibling_leg_reservations", result.checks)


if __name__ == "__main__":
    unittest.main()


class SupersededProposalTests(unittest.TestCase):
    """Withdrawing an unapproved proposal, and keeping it withdrawn.

    A proposal a later evaluation replaced must stop being offered for
    approval. The state machine already has the vocabulary for that, so the
    disposition is a transition rather than a deletion — and the listing that
    advertises what a human may approve has to agree with the machine.
    """

    def test_a_proposal_may_be_sent_back_for_re_evaluation(self):
        assert_transition(ExecutionState.PROPOSED,
                          ExecutionState.REEVALUATION_REQUIRED)

    def test_a_proposal_may_be_withdrawn_outright(self):
        for target in (ExecutionState.CANCELLED, ExecutionState.EXPIRED,
                       ExecutionState.REJECTED):
            with self.subTest(target=target):
                assert_transition(ExecutionState.PROPOSED, target)

    def test_re_evaluation_required_cannot_become_approved(self):
        """The point of the state: it is not approvable without a new decision."""
        with self.assertRaises(InvalidTransition):
            assert_transition(ExecutionState.REEVALUATION_REQUIRED,
                              ExecutionState.APPROVED)

    def test_a_withdrawn_proposal_can_never_be_approved(self):
        for state in (ExecutionState.CANCELLED, ExecutionState.EXPIRED,
                      ExecutionState.REJECTED):
            with self.subTest(state=state):
                with self.assertRaises(InvalidTransition):
                    assert_transition(state, ExecutionState.APPROVED)

    def test_a_withdrawn_proposal_reserves_no_authorization(self):
        """Terminal states are excluded, so the dollars go back to the month."""
        from src.allocation import sibling_reservations_usd

        approval = {"month": "2026-09", "max_amount_usd": "25.00"}
        live = sibling_reservations_usd(
            {"dec_old": approval}, {"dec_old": ExecutionState.APPROVED}, "2026-09")
        self.assertEqual(live, Decimal("25.00"))
        for state in (ExecutionState.CANCELLED, ExecutionState.EXPIRED,
                      ExecutionState.REJECTED):
            with self.subTest(state=state):
                self.assertEqual(
                    sibling_reservations_usd(
                        {"dec_old": approval}, {"dec_old": state}, "2026-09"),
                    Decimal("0.00"))

    def test_the_transition_is_recorded_in_history_and_the_audit_log(self):
        from src.execution_store import ExecutionRecord, transition

        with tempfile.TemporaryDirectory() as tmp:
            audit = os.path.join(tmp, "audit.jsonl")
            record = ExecutionRecord(decision_id="dec_x", month="2026-09",
                                     asset="ISRG", amount_usd="25.00")
            transition(record, ExecutionState.REEVALUATION_REQUIRED,
                       reason="stale quote", audit_path=audit)
            transition(record, ExecutionState.CANCELLED,
                       reason="superseded", audit_path=audit)

            self.assertEqual(record.state, ExecutionState.CANCELLED)
            self.assertEqual(
                [(h["from"], h["to"]) for h in record.history],
                [(ExecutionState.PROPOSED, ExecutionState.REEVALUATION_REQUIRED),
                 (ExecutionState.REEVALUATION_REQUIRED, ExecutionState.CANCELLED)])
            self.assertEqual([h["reason"] for h in record.history],
                             ["stale quote", "superseded"])

            with open(audit, encoding="utf-8") as handle:
                events = [json.loads(line) for line in handle if line.strip()]
            self.assertEqual([e["event"] for e in events],
                             ["STATE_CHANGE", "STATE_CHANGE"])
            self.assertEqual(events[-1]["detail"]["reason"], "superseded")

    def test_the_pending_listing_asks_the_state_machine(self):
        """It must not keep a second, hand-maintained list of approvable states."""
        import importlib.util

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(repo_root, "scripts", "show_pending_decision.py")
        spec = importlib.util.spec_from_file_location("_pending", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertTrue(module.awaits_approval(ExecutionState.PROPOSED))
        self.assertTrue(module.awaits_approval(ExecutionState.REAPPROVAL_REQUIRED))
        for state in (ExecutionState.CANCELLED, ExecutionState.EXPIRED,
                      ExecutionState.REJECTED, ExecutionState.FILLED,
                      ExecutionState.SUBMITTED, ExecutionState.APPROVED,
                      ExecutionState.REEVALUATION_REQUIRED):
            with self.subTest(state=state):
                self.assertFalse(module.awaits_approval(state))


class LivePathRegressionTests(unittest.TestCase):
    """The full armed matrix: everything that must still refuse to execute.

    These run with all three switches OPEN, which is the only configuration in
    which a submission is conceivable at all. The point of each case is that
    opening the switches buys exactly one thing — the right to be checked — and
    that every other control still stands on its own.

    Nothing here approves, mints a ticket, or submits: ``preflight`` is a pure
    function over a snapshot, and the shipped config is never touched.
    """

    def setUp(self):
        self.config = live_config()
        self.state = make_state(make_config(), month=month())
        self.decision = equity_decision()
        self.approval = approve(self.decision)

    def pre(self, decision=_UNSET, approval=_UNSET, snap=_UNSET, **kwargs):
        """Pass approval=None or snap=None explicitly to test their absence.

        The sentinel matters: an earlier version of this helper treated None as
        "use the default", which silently turned the two most important tests in
        this class — no approval, and no broker — into happy-path runs that
        passed for the wrong reason.
        """
        return preflight(
            self.decision if decision is _UNSET else decision,
            self.approval if approval is _UNSET else approval,
            self.config, self.state,
            snapshot() if snap is _UNSET else snap,
            now=NOW, **kwargs)

    def codes(self, result):
        return [code for code, _ in result.blockers]

    # --- the switches, individually ---------------------------------------

    def test_the_armed_happy_path_is_the_only_one_that_clears(self):
        result = self.pre()
        self.assertTrue(result.ok, result.blockers)
        self.assertEqual(result.next_state, ExecutionState.PRE_EXECUTION_VALIDATED)

    def test_dry_run_cannot_create_a_live_submission_ticket(self):
        self.config = live_config(execution_mode="DRY_RUN")
        result = self.pre()
        self.assertFalse(result.ok)
        self.assertIn("EXECUTION_MODE_DRY_RUN", self.codes(result))
        self.assertNotEqual(result.next_state, ExecutionState.PRE_EXECUTION_VALIDATED)

    def test_agent_disabled_blocks_submission(self):
        self.config = live_config(agent_enabled=False)
        self.assertIn("AGENT_DISABLED", self.codes(self.pre()))

    def test_live_trading_false_blocks_submission(self):
        self.config = live_config(live_trading=False)
        self.assertIn("LIVE_TRADING_DISABLED", self.codes(self.pre()))

    def test_autonomous_mode_is_refused_outright(self):
        self.config = live_config(execution_mode="AUTONOMOUS")
        self.assertIn("AUTONOMOUS_NOT_IMPLEMENTED", self.codes(self.pre()))

    # --- approval --------------------------------------------------------

    def test_armed_switches_still_cannot_submit_without_approval(self):
        """The whole point of APPROVAL_REQUIRED."""
        result = self.pre(approval=None)
        self.assertFalse(result.ok)
        self.assertIn("NOT_APPROVED", self.codes(result))

    def test_an_approval_for_a_different_decision_id_cannot_advance_this_one(self):
        other = approve(equity_decision(decision_id="dec_someone_else"))
        result = self.pre(approval=other)
        self.assertFalse(result.ok)

    def test_an_approval_for_a_different_asset_cannot_advance_this_one(self):
        mismatched = equity_decision(ticker="MU", security_name="Micron")
        result = self.pre(decision=mismatched)
        self.assertFalse(result.ok, "approval is bound to the asset")

    def test_an_amount_above_the_approved_maximum_is_refused(self):
        bigger = equity_decision(amount="20.00")
        bigger["decision_id"] = self.decision["decision_id"]
        result = self.pre(decision=bigger)
        self.assertFalse(result.ok)

    def test_even_a_smaller_amount_requires_its_own_approval(self):
        """Stricter than "at most the approved amount", and deliberately so.

        The approval fingerprint binds ``proposed_amount_usd``, so changing the
        amount at all — downward included — produces a different decision that
        the existing approval does not cover. The approval's "maximum" wording
        bounds what a *fill* may cost, not what a re-priced decision may claim.
        """
        smaller = equity_decision(amount="5.00")
        smaller["decision_id"] = self.decision["decision_id"]
        smaller["month"] = month()
        result = self.pre(decision=smaller)
        self.assertFalse(result.ok, "a changed amount needs re-approval")

    def test_a_policy_change_after_approval_invalidates_it(self):
        stale = approve(self.decision)
        stale.policy_fingerprint = "0" * 64
        result = self.pre(approval=stale)
        self.assertFalse(result.ok)
        self.assertTrue(any("POLICY" in c or "FINGERPRINT" in c
                            for c in self.codes(result)), self.codes(result))

    def test_an_expired_approval_cannot_advance(self):
        result = self.pre(now=None) if False else preflight(
            self.decision, self.approval, self.config, self.state, snapshot(),
            now=NOW + timedelta(hours=48))
        self.assertFalse(result.ok)
        self.assertIn("APPROVAL_EXPIRED", [c for c, _ in result.blockers])

    # --- market and account conditions ------------------------------------

    def test_a_stale_quote_fails(self):
        old = snapshot(quote_timestamp=NOW - timedelta(seconds=3600))
        result = self.pre(snap=old)
        self.assertFalse(result.ok)
        self.assertIn("STALE_QUOTE", self.codes(result))

    def test_excess_price_drift_fails(self):
        moved = snapshot(quote_price_usd=Decimal("500.00"))   # +36% from 366.67
        result = self.pre(snap=moved)
        self.assertFalse(result.ok)
        self.assertIn("PRICE_MOVED_BEYOND_TOLERANCE", self.codes(result))

    def test_insufficient_settled_cash_fails(self):
        broke = snapshot(cash_usd=Decimal("2.00"))
        result = self.pre(snap=broke)
        self.assertFalse(result.ok)
        self.assertTrue(any("CASH" in c for c in self.codes(result)),
                        self.codes(result))

    def test_buying_power_cannot_substitute_for_settled_cash(self):
        """The margin the account may borrow is not money it may spend."""
        margin = snapshot(cash_usd=Decimal("2.00"),
                          buying_power_usd=Decimal("1000.00"))
        result = self.pre(snap=margin)
        self.assertFalse(result.ok, "buying power must not fund the order")

    def test_an_over_budget_amount_fails(self):
        spent = make_state(make_config(), month=month(), committed="20.00")
        result = preflight(self.decision, self.approval, self.config, spent,
                           snapshot(), now=NOW)
        self.assertFalse(result.ok)

    def test_missing_tradability_fails_closed(self):
        unknown = snapshot(tradable=None)
        result = self.pre(snap=unknown)
        self.assertFalse(result.ok)

    def test_false_tradability_fails(self):
        halted = snapshot(tradable=False)
        result = self.pre(snap=halted)
        self.assertFalse(result.ok)
        self.assertTrue(any("TRADAB" in c or "UNTRADABLE" in c
                            for c in self.codes(result)), self.codes(result))

    def test_fractional_ineligibility_fails(self):
        whole = snapshot(fractional_tradable=False)
        self.assertFalse(self.pre(snap=whole).ok)

    def test_an_unreadable_broker_fails_closed(self):
        result = self.pre(snap=None)
        self.assertFalse(result.ok)
        self.assertIn("BROKER_UNREADABLE", self.codes(result))

    # --- replay and idempotency -------------------------------------------

    def test_a_duplicate_decision_id_cannot_be_replayed(self):
        acted = make_state(make_config(), month=month(),
                           acted=[self.decision["decision_id"]])
        result = preflight(self.decision, self.approval, self.config, acted,
                           snapshot(), now=NOW)
        self.assertFalse(result.ok)
        self.assertIn("DUPLICATE_DECISION_ID", [c for c, _ in result.blockers])

    def test_an_already_submitted_decision_cannot_submit_again(self):
        result = self.pre(execution_state=ExecutionState.SUBMITTED)
        self.assertFalse(result.ok)
        self.assertIn("ALREADY_SUBMITTED", self.codes(result))

    def test_a_terminal_decision_cannot_advance(self):
        for state in (ExecutionState.FILLED, ExecutionState.CANCELLED,
                      ExecutionState.REJECTED, ExecutionState.EXPIRED):
            with self.subTest(state=state):
                result = self.pre(execution_state=state)
                self.assertFalse(result.ok)
                self.assertIn("TERMINAL_STATE", self.codes(result))

    # --- the crash-safety state -------------------------------------------

    def test_submission_uncertainty_never_permits_a_blind_retry(self):
        """Not-found is not proof nothing happened."""
        result = self.pre(execution_state=ExecutionState.SUBMISSION_UNCERTAIN)
        self.assertFalse(result.ok)
        self.assertIn("RECONCILIATION_REQUIRED", self.codes(result))
        message = dict(result.blockers)["RECONCILIATION_REQUIRED"]
        self.assertIn("Never retry blindly", message)

    def test_uncertainty_is_not_cleared_by_arming_harder(self):
        for mode in ("APPROVAL_REQUIRED", "AUTONOMOUS"):
            with self.subTest(mode=mode):
                self.config = live_config(execution_mode=mode)
                result = self.pre(execution_state=ExecutionState.SUBMISSION_UNCERTAIN)
                self.assertFalse(result.ok)

    # --- prohibitions survive arming --------------------------------------

    def test_selling_options_margin_shorting_transfers_stay_impossible(self):
        for field, value in (("action", "sell"), ("side", "sell"),
                             ("asset_class", "OPTION"), ("asset_type", "option"),
                             ("decision", "SELL")):
            with self.subTest(field=field, value=value):
                bad = equity_decision(**{field: value})
                result = self.pre(decision=bad)
                self.assertFalse(result.ok, "%s=%s must never execute" % (field, value))
