"""Broker reconciliation tests — synthetic order data only.

Nothing here calls Robinhood. Every order payload is fabricated to match the
shape `get_equity_orders` / `get_crypto_orders` actually return.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.reconciliation import (  # noqa: E402
    OPEN_STATES,
    Reconciliation,
    ReconciliationError,
    assert_order_within_authorization,
    extract_orders,
    normalize_order,
    reconcile,
)
from tests.helpers import make_config, make_state  # noqa: E402

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
MONTH = "2026-09"


def order(order_id, amount, state="filled", side="buy", day=2, executed=None,
          symbol="VTI", **extra):
    """A synthetic Robinhood equity order."""
    raw = {
        "id": order_id,
        "symbol": symbol,
        "side": side,
        "state": state,
        "type": "market",
        "created_at": "2026-09-%02dT15:00:00Z" % day,
        "dollar_based_amount": {"amount": amount, "currency_code": "USD"},
        "placed_agent": "agentic",
    }
    if executed is not None:
        raw["cumulative_quantity"] = executed[0]
        raw["average_price"] = executed[1]
    else:
        raw["cumulative_quantity"] = "0"
        raw["average_price"] = None
        if state == "filled":
            raw["cumulative_quantity"] = "1"
            raw["average_price"] = amount
    raw.update(extra)
    return raw


def envelope(orders):
    return {"data": {"orders": orders}}


class WorkedExampleTests(unittest.TestCase):
    """The exact example from INVESTMENT_POLICY: $25 authorized, $10 done, $5 pending."""

    def test_ten_filled_plus_five_pending_leaves_ten(self):
        config = make_config()
        state = make_state(config, month=MONTH, committed="15.00", acted=["d1", "d2"])
        payload = envelope([
            order("o1", "10.00", state="filled", executed=("0.02", "500.00")),
            order("o2", "5.00", state="confirmed", day=4),
        ])
        result = reconcile(config, state, payload, now=NOW)
        self.assertEqual(result.broker_filled_usd, Decimal("10.00"))
        self.assertEqual(result.broker_pending_usd, Decimal("5.00"))
        self.assertEqual(result.reconciled_committed_usd, Decimal("15.00"))
        self.assertEqual(result.safe_remaining_usd, Decimal("10.00"))
        self.assertEqual(result.max_additional_order_usd, Decimal("10.00"))
        self.assertFalse(result.blocking)

    def test_an_eleven_dollar_order_is_refused_at_that_point(self):
        config = make_config()
        state = make_state(config, month=MONTH, committed="15.00")
        payload = envelope([
            order("o1", "10.00", state="filled", executed=("0.02", "500.00")),
            order("o2", "5.00", state="confirmed", day=4),
        ])
        result = reconcile(config, state, payload, now=NOW)
        assert_order_within_authorization(result, Decimal("10.00"))  # must not raise
        with self.assertRaises(ReconciliationError):
            assert_order_within_authorization(result, Decimal("10.01"))


class ConservatismTests(unittest.TestCase):
    """Every ambiguity must resolve toward spending less."""

    def setUp(self):
        self.config = make_config()

    def test_a_manual_purchase_the_local_ledger_never_saw_still_counts(self):
        """The crash / manual-buy case: local says $0, broker says $20."""
        state = make_state(self.config, month=MONTH, committed="0.00")
        payload = envelope([order("o1", "20.00", state="filled", executed=("0.04", "500.00"))])
        result = reconcile(self.config, state, payload, now=NOW)
        self.assertEqual(result.local_committed_usd, Decimal("0.00"))
        self.assertEqual(result.reconciled_committed_usd, Decimal("20.00"))
        self.assertEqual(result.max_additional_order_usd, Decimal("5.00"))
        self.assertTrue(any("manual purchase" in d for d in result.discrepancies))

    def test_a_stale_local_month_does_not_grant_fresh_authorization(self):
        """Local state stuck in August must not hide September spending."""
        state = make_state(self.config, month="2026-08", committed="0.00")
        payload = envelope([order("o1", "25.00", state="filled", executed=("0.05", "500.00"))])
        result = reconcile(self.config, state, payload, now=NOW)
        self.assertEqual(result.max_additional_order_usd, Decimal("0.00"))
        self.assertTrue(any("local budget state is for 2026-08" in d for d in result.discrepancies))

    def test_local_ahead_of_broker_uses_the_local_figure(self):
        state = make_state(self.config, month=MONTH, committed="20.00")
        payload = envelope([order("o1", "5.00", state="filled", executed=("0.01", "500.00"))])
        result = reconcile(self.config, state, payload, now=NOW)
        self.assertEqual(result.reconciled_committed_usd, Decimal("20.00"))
        self.assertEqual(result.max_additional_order_usd, Decimal("5.00"))

    def test_a_partially_filled_open_order_reserves_its_full_notional(self):
        """$25 requested, $6 filled so far, still open -> the whole $25 is tied up."""
        state = make_state(self.config, month=MONTH, committed="0.00")
        payload = envelope([
            order("o1", "25.00", state="partially_filled", executed=("0.012", "500.00")),
        ])
        result = reconcile(self.config, state, payload, now=NOW)
        self.assertEqual(result.broker_pending_usd, Decimal("25.00"))
        self.assertEqual(result.max_additional_order_usd, Decimal("0.00"))

    def test_a_cancelled_order_still_counts_what_actually_executed(self):
        state = make_state(self.config, month=MONTH, committed="0.00")
        payload = envelope([
            order("o1", "25.00", state="cancelled", executed=("0.02", "500.00")),
        ])
        result = reconcile(self.config, state, payload, now=NOW)
        self.assertEqual(result.broker_filled_usd, Decimal("10.00"))
        self.assertEqual(result.max_additional_order_usd, Decimal("15.00"))
        self.assertTrue(any("later cancelled" in d for d in result.discrepancies))

    def test_a_fully_cancelled_order_consumes_nothing(self):
        state = make_state(self.config, month=MONTH, committed="0.00")
        payload = envelope([order("o1", "25.00", state="cancelled")])
        result = reconcile(self.config, state, payload, now=NOW)
        self.assertEqual(result.max_additional_order_usd, Decimal("25.00"))

    def test_overspend_blocks_all_further_purchases(self):
        state = make_state(self.config, month=MONTH, committed="0.00")
        payload = envelope([order("o1", "30.00", state="filled", executed=("0.06", "500.00"))])
        result = reconcile(self.config, state, payload, now=NOW)
        self.assertTrue(result.blocking)
        self.assertEqual(result.max_additional_order_usd, Decimal("0.00"))
        with self.assertRaises(ReconciliationError):
            assert_order_within_authorization(result, Decimal("0.01"))

    def test_safe_remaining_never_exceeds_the_configured_authorization(self):
        state = make_state(self.config, month=MONTH, committed="0.00")
        state.authorized_budget_usd = Decimal("500.00")  # tampered local state
        result = reconcile(self.config, state, envelope([]), now=NOW)
        self.assertEqual(result.authorized_usd, Decimal("25.00"))
        self.assertEqual(result.max_additional_order_usd, Decimal("25.00"))
        self.assertTrue(any("above the configured" in d for d in result.discrepancies))

    def test_crypto_and_equity_orders_share_one_authorization(self):
        state = make_state(self.config, month=MONTH, committed="0.00")
        eq = envelope([order("e1", "15.00", state="filled", executed=("0.03", "500.00"))])
        cr = {"data": {"results": [
            order("c1", "8.00", state="filled", symbol="BTC-USD",
                  executed=("0.0001", "80000.00"), day=6),
        ]}}
        result = reconcile(self.config, state, eq, crypto_orders=cr, now=NOW)
        self.assertEqual(result.reconciled_committed_usd, Decimal("23.00"))
        self.assertEqual(result.max_additional_order_usd, Decimal("2.00"))

    def test_orders_from_a_previous_month_are_excluded(self):
        state = make_state(self.config, month=MONTH, committed="0.00")
        stale = order("old", "25.00", state="filled", executed=("0.05", "500.00"))
        stale["created_at"] = "2026-08-15T15:00:00Z"
        result = reconcile(self.config, state, envelope([stale]), now=NOW)
        self.assertEqual(result.max_additional_order_usd, Decimal("25.00"))
        self.assertEqual(result.orders_considered, 1)
        self.assertEqual(len(result.orders_this_month), 0)


class FailClosedTests(unittest.TestCase):
    """Unreadable broker data must never read as 'nothing was bought'."""

    def setUp(self):
        self.config = make_config()
        self.state = make_state(self.config, month=MONTH, committed="0.00")

    def test_missing_equity_payload_raises(self):
        with self.assertRaises(ReconciliationError):
            reconcile(self.config, self.state, None, now=NOW)

    def test_unparseable_payload_raises(self):
        for payload in ({"unexpected": True}, "orders", 42):
            with self.subTest(payload=payload):
                with self.assertRaises(ReconciliationError):
                    reconcile(self.config, self.state, payload, now=NOW)

    def test_an_unknown_order_state_raises(self):
        payload = envelope([order("o1", "5.00", state="teleported")])
        with self.assertRaises(ReconciliationError):
            reconcile(self.config, self.state, payload, now=NOW)

    def test_a_missing_order_id_raises(self):
        bad = order("x", "5.00")
        del bad["id"]
        with self.assertRaises(ReconciliationError):
            reconcile(self.config, self.state, envelope([bad]), now=NOW)

    def test_a_missing_or_malformed_timestamp_raises(self):
        for stamp in (None, "", "yesterday", "09/2026"):
            with self.subTest(stamp=stamp):
                bad = order("o1", "5.00")
                bad["created_at"] = stamp
                with self.assertRaises(ReconciliationError):
                    reconcile(self.config, self.state, envelope([bad]), now=NOW)

    def test_an_unparseable_amount_raises(self):
        bad = order("o1", "five dollars")
        with self.assertRaises(ReconciliationError):
            reconcile(self.config, self.state, envelope([bad]), now=NOW)

    def test_a_non_object_order_raises(self):
        with self.assertRaises(ReconciliationError):
            reconcile(self.config, self.state, envelope(["not an order"]), now=NOW)

    def test_empty_order_list_is_fine_and_is_not_an_error(self):
        result = reconcile(self.config, self.state, envelope([]), now=NOW)
        self.assertEqual(result.max_additional_order_usd, Decimal("25.00"))
        self.assertFalse(result.blocking)


class ReplayAndSellTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_duplicate_broker_order_ids_block(self):
        state = make_state(self.config, month=MONTH, committed="0.00")
        payload = envelope([order("dup", "5.00"), order("dup", "5.00", day=3)])
        result = reconcile(self.config, state, payload, now=NOW)
        self.assertTrue(result.blocking)
        self.assertTrue(any("duplicate broker order id" in d for d in result.discrepancies))

    def test_duplicate_local_decision_ids_block(self):
        state = make_state(self.config, month=MONTH, committed="0.00")
        result = reconcile(
            self.config, state, envelope([]), now=NOW, acted_decision_ids=["a", "a"]
        )
        self.assertTrue(result.blocking)

    def test_a_sell_in_the_agentic_account_blocks_and_is_never_a_credit(self):
        state = make_state(self.config, month=MONTH, committed="0.00")
        payload = envelope([
            order("s1", "50.00", state="filled", side="sell", executed=("0.1", "500.00")),
        ])
        result = reconcile(self.config, state, payload, now=NOW)
        self.assertTrue(result.blocking)
        self.assertEqual(result.max_additional_order_usd, Decimal("0.00"))
        self.assertTrue(any("selling is forbidden" in d for d in result.discrepancies))


class NormalizeTests(unittest.TestCase):
    def test_quantity_times_price_is_used_when_no_dollar_amount(self):
        raw = {
            "id": "o1", "symbol": "ISRG", "side": "buy", "state": "filled",
            "created_at": "2026-09-04T15:00:00Z",
            "quantity": "0.068", "price": "366.67",
            "cumulative_quantity": "0.068", "average_price": "366.67",
        }
        record = normalize_order(raw)
        self.assertEqual(record.notional_executed, Decimal("24.93"))

    def test_every_open_state_reserves_capital(self):
        for state_name in sorted(OPEN_STATES):
            with self.subTest(state=state_name):
                record = normalize_order(order("o1", "7.00", state=state_name))
                self.assertTrue(record.is_open)
                self.assertEqual(record.committed_usd, Decimal("7.00"))

    def test_extract_handles_all_three_payload_shapes(self):
        rows = [order("o1", "5.00")]
        for payload in (rows, {"orders": rows}, {"data": {"orders": rows}}):
            with self.subTest(shape=type(payload).__name__):
                self.assertEqual(len(extract_orders(payload)), 1)


class NoExecutionPathTests(unittest.TestCase):
    def test_reconciliation_module_contains_no_order_submission(self):
        import src.reconciliation as module

        with open(module.__file__, encoding="utf-8") as handle:
            content = handle.read()
        for needle in ("place_equity_order", "place_crypto_order", "review_equity_order",
                       "preview_crypto_order", "submit_order"):
            self.assertNotIn(needle, content)


if __name__ == "__main__":
    unittest.main()
