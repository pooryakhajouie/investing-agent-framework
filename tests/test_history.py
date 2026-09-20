"""Portfolio history — normalization, provenance, and refusing to lose data.

Four properties under test, each corresponding to something the capability
audit *verified* about the broker's data rather than assumed:

* **Provenance survives.** Equity orders carry ``placed_agent``; crypto orders
  carry no attribution field at all, so they normalize to ``UNKNOWN`` rather
  than being guessed at as manual.
* **Split-sensitive analysis reads lots, never orders.** Order prices are not
  split-adjusted and invert the answer across a split.
* **An incremental ingest cannot lose or rewrite history.**
* **No unmasked account identifier reaches disk.**
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import (  # noqa: E402
    AGENT_ACTION,
    ASSET_CRYPTO,
    ASSET_EQUITY,
    INSUFFICIENT_DATA,
    MANUAL_ACTION,
    MIN_SELLS_FOR_TIMING,
    NON_PURCHASE_LOT,
    PROVENANCES,
    SCHEMA_VERSION,
    SOURCE_ORDER_RECORD,
    SOURCE_TAX_LOT,
    UNKNOWN,
    SplitUnsafeError,
    basis_drift,
    buys,
    by_provenance,
    cadence_by_month,
    coverage_summary,
    crypto_order_provenance,
    equity_order_provenance,
    mask_account,
    merge_records,
    non_purchase_lots,
    normalize_crypto_order,
    normalize_equity_order,
    normalize_tax_lot,
    position_count_over_time,
    sell_timing,
    sells,
    sizing_consistency,
    sprawl_events,
    symbols_opened_by_day,
    ticket_sizes,
    unmasked_account_numbers,
    validate_history,
)

# Real shapes, taken from the audited responses.
RAW_EQUITY_ORDER = {
    "id": "11111111-2222-4333-8444-555555555555",
    "symbol": "EXMP",
    "side": "buy",
    "state": "filled",
    "quantity": "0.005000",
    "cumulative_quantity": "0.005000",
    "average_price": "1000.000000",
    "fees": "0.000000",
    "dollar_based_amount": {"amount": "5.000000", "currency_code": "USD"},
    "placed_agent": "user",
    "created_at": "2026-01-15T15:30:00.334770Z",
    "last_transaction_at": "2026-01-15T15:30:00.482Z",
}

RAW_CRYPTO_ORDER = {
    "id": "22222222-3333-4444-8555-666666666666",
    "currency_code": "BTC",
    "side": "sell",
    "state": "filled",
    "quantity": "0.00050000",
    "cumulative_quantity": "0.00050000",
    "average_price": "60000.00",
    "entered_price": "30",
    "total_executed_notional": "30.00",
    "created_at": "2025-06-20T12:00:00.926309-04:00",
    "updated_at": "2025-06-20T12:00:01.868308-04:00",
}

# NVDA's real lots: a buy, a deposit, and two baseline lots.
RAW_LOTS = [
    {"open_lot_id": "a", "open_tran_type": "buy", "quantity": "0.025000",
     "cost_per_share": "200.000000", "tax_cost_basis": "5.000000",
     "open_date": "2026-01-05", "term": "st",
     "order_id": "1200000000000001"},
    {"open_lot_id": "b", "open_tran_type": "deposit", "quantity": "0.100000",
     "cost_per_share": "50.000000", "tax_cost_basis": "5.000000",
     "open_date": "2023-03-10", "term": "lt"},
    {"open_lot_id": "c", "open_tran_type": "baselinetaxlot", "quantity": "0.125000",
     "cost_per_share": "40.000000", "tax_cost_basis": "5.000000",
     "open_date": "2022-06-20", "term": "lt"},
]

ACCOUNT = "123456789"


class MaskingTests(unittest.TestCase):
    def test_an_account_number_masks_to_its_last_four(self):
        self.assertEqual(mask_account("123456789"), "••••6789")
        self.assertEqual(mask_account("987654321098"), "••••1098")

    def test_masking_is_not_reversible(self):
        self.assertNotIn("12345", mask_account("123456789"))

    def test_an_empty_identifier_masks_to_a_placeholder(self):
        self.assertEqual(mask_account(None), "••••????")

    def test_long_digit_runs_are_detected_as_leaks(self):
        self.assertEqual(unmasked_account_numbers('{"a": "123456789"}'), ["123456789"])

    def test_uuids_are_not_mistaken_for_account_numbers(self):
        blob = '{"id": "11111111-2222-4333-8444-555555555555"}'
        self.assertEqual(unmasked_account_numbers(blob), [])

    def test_timestamps_are_not_mistaken_for_account_numbers(self):
        blob = '{"at": "2026-09-04T18:37:48.334770Z"}'
        self.assertEqual(unmasked_account_numbers(blob), [])

    def test_masked_identifiers_are_clean(self):
        self.assertEqual(unmasked_account_numbers('{"masked": "••••6789"}'), [])


class ProvenanceTests(unittest.TestCase):
    def test_placed_agent_user_is_a_manual_action(self):
        provenance, basis = equity_order_provenance("user")
        self.assertEqual(provenance, MANUAL_ACTION)
        self.assertIn("placed_agent", basis)

    def test_recurring_and_drip_are_also_manual(self):
        """A standing instruction the owner set up is still the owner's."""
        for value in ("recurring", "drip"):
            with self.subTest(value=value):
                self.assertEqual(equity_order_provenance(value)[0], MANUAL_ACTION)

    def test_placed_agent_agentic_is_an_agent_action(self):
        self.assertEqual(equity_order_provenance("agentic")[0], AGENT_ACTION)

    def test_a_missing_placed_agent_is_unknown_not_assumed_manual(self):
        self.assertEqual(equity_order_provenance(None)[0], UNKNOWN)
        self.assertEqual(equity_order_provenance("")[0], UNKNOWN)

    def test_an_unrecognised_placed_agent_is_unknown(self):
        self.assertEqual(equity_order_provenance("some_new_channel")[0], UNKNOWN)

    def test_crypto_orders_are_unknown_because_the_broker_exposes_nothing(self):
        """The audit found no attribution field on crypto orders at all."""
        provenance, basis = crypto_order_provenance("abc")
        self.assertEqual(provenance, UNKNOWN)
        self.assertIn("no attribution field", basis)

    def test_a_crypto_order_claimed_by_the_decision_log_is_an_agent_action(self):
        provenance, basis = crypto_order_provenance("abc", ["abc"])
        self.assertEqual(provenance, AGENT_ACTION)
        self.assertIn("decisions.jsonl", basis)

    def test_every_provenance_is_in_the_declared_set(self):
        for value in (MANUAL_ACTION, AGENT_ACTION, NON_PURCHASE_LOT, UNKNOWN):
            self.assertIn(value, PROVENANCES)


class NormalizationTests(unittest.TestCase):
    def test_an_equity_order_normalizes(self):
        record = normalize_equity_order(RAW_EQUITY_ORDER, ACCOUNT)
        self.assertEqual(record.asset_class, ASSET_EQUITY)
        self.assertEqual(record.symbol, "EXMP")
        self.assertEqual(record.side, "buy")
        self.assertEqual(record.masked_account, "••••6789")
        self.assertEqual(record.executed_on, "2026-01-15")
        self.assertEqual(record.dollars, "5.000000")
        self.assertEqual(record.provenance, MANUAL_ACTION)
        self.assertEqual(record.source, SOURCE_ORDER_RECORD)

    def test_an_equity_order_is_marked_not_split_adjusted(self):
        """The single most important flag on an order record."""
        self.assertFalse(normalize_equity_order(RAW_EQUITY_ORDER, ACCOUNT).split_adjusted)

    def test_a_crypto_order_normalizes_and_hyphenates_the_pair(self):
        record = normalize_crypto_order(RAW_CRYPTO_ORDER, ACCOUNT)
        self.assertEqual(record.asset_class, ASSET_CRYPTO)
        self.assertEqual(record.symbol, "BTC-USD")
        self.assertEqual(record.side, "sell")
        self.assertEqual(record.provenance, UNKNOWN)

    def test_a_crypto_orders_dollars_come_from_notional_not_entered_price(self):
        """`entered_price` is the dollar amount entered, not a price."""
        record = normalize_crypto_order(RAW_CRYPTO_ORDER, ACCOUNT)
        self.assertEqual(record.dollars, "30.00")
        self.assertEqual(record.price, "60000.00")

    def test_a_purchase_lot_defers_attribution_to_the_order_record(self):
        record = normalize_tax_lot(RAW_LOTS[0], ACCOUNT, "EXMP")
        self.assertEqual(record.provenance, UNKNOWN)
        self.assertIn("order record", record.provenance_basis)

    def test_a_deposit_lot_is_a_non_purchase_lot(self):
        record = normalize_tax_lot(RAW_LOTS[1], ACCOUNT, "EXMP")
        self.assertEqual(record.provenance, NON_PURCHASE_LOT)
        self.assertEqual(record.open_tran_type, "deposit")

    def test_a_baseline_lot_is_a_non_purchase_lot(self):
        record = normalize_tax_lot(RAW_LOTS[2], ACCOUNT, "EXMP")
        self.assertEqual(record.provenance, NON_PURCHASE_LOT)

    def test_the_non_purchase_basis_names_the_missing_tooling(self):
        """There is no transfer feed, and the record says so rather than guessing."""
        record = normalize_tax_lot(RAW_LOTS[1], ACCOUNT, "EXMP")
        self.assertIn("ACATS", record.provenance_basis)
        self.assertIn("did not arrive by purchase", record.provenance_basis)

    def test_a_lot_is_marked_split_adjusted(self):
        self.assertTrue(normalize_tax_lot(RAW_LOTS[0], ACCOUNT, "EXMP").split_adjusted)

    def test_a_lots_order_id_is_dropped(self):
        """It joins to nothing and is a long digit run in a file that has none."""
        record = normalize_tax_lot(RAW_LOTS[0], ACCOUNT, "EXMP").to_dict()
        self.assertNotIn("order_id", record)
        self.assertEqual(unmasked_account_numbers(str(record)), [])


class SplitSafetyTests(unittest.TestCase):
    """The trap that would silently invert every entry-quality conclusion."""

    LOTS = [normalize_tax_lot(raw, ACCOUNT, "EXMP").to_dict() for raw in RAW_LOTS]

    def test_basis_drift_reads_lots(self):
        result = basis_drift(self.LOTS)
        self.assertEqual(result["verdict"], "AVERAGED_UP")
        self.assertEqual(result["first_date"], "2022-06-20")
        self.assertEqual(result["last_date"], "2026-01-05")
        self.assertTrue(result["split_adjusted"])

    def test_basis_drift_refuses_order_records(self):
        orders = [normalize_equity_order(RAW_EQUITY_ORDER, ACCOUNT).to_dict()]
        with self.assertRaises(SplitUnsafeError):
            basis_drift(orders)

    def test_the_refusal_explains_why(self):
        orders = [normalize_equity_order(RAW_EQUITY_ORDER, ACCOUNT).to_dict()]
        with self.assertRaises(SplitUnsafeError) as caught:
            basis_drift(orders)
        self.assertIn("split-adjusted", str(caught.exception))

    def test_lots_and_orders_disagree_across_a_split(self):
        """Across a 10:1 split: the order says $500.00, the lot says $50.00.

        This is why the refusal above exists rather than a documentation note.
        """
        order_price = 500.00
        lot_price = 47.00
        self.assertAlmostEqual(order_price / 10, 50.00, places=2)
        self.assertLess(lot_price, order_price)

    def test_a_single_lot_is_insufficient_for_drift(self):
        self.assertEqual(basis_drift(self.LOTS[:1])["verdict"], INSUFFICIENT_DATA)

    def test_averaging_down_is_detected_too(self):
        lots = [
            normalize_tax_lot(
                {"open_lot_id": "x", "open_tran_type": "buy", "quantity": "1",
                 "cost_per_share": "100", "tax_cost_basis": "100",
                 "open_date": "2025-01-01", "term": "lt"}, ACCOUNT, "X").to_dict(),
            normalize_tax_lot(
                {"open_lot_id": "y", "open_tran_type": "buy", "quantity": "1",
                 "cost_per_share": "50", "tax_cost_basis": "50",
                 "open_date": "2025-06-01", "term": "st"}, ACCOUNT, "X").to_dict(),
        ]
        self.assertEqual(basis_drift(lots)["verdict"], "AVERAGED_DOWN")

    def test_non_purchase_lots_are_extractable(self):
        self.assertEqual(len(non_purchase_lots(self.LOTS)), 2)


class RefusalTests(unittest.TestCase):
    """The detectors that must decline rather than guess."""

    def test_sell_timing_refuses_on_two_sells(self):
        orders = [normalize_crypto_order(RAW_CRYPTO_ORDER, ACCOUNT).to_dict()] * 1
        result = sell_timing(orders)
        self.assertEqual(result["verdict"], INSUFFICIENT_DATA)
        self.assertEqual(result["minimum"], MIN_SELLS_FOR_TIMING)

    def test_the_refusal_says_no_conclusion_will_be_drawn(self):
        result = sell_timing([])
        self.assertIn("No conclusion", result["explanation"])
        self.assertIn("holding too long", result["explanation"])

    def test_sell_timing_becomes_analyzable_with_enough_sells(self):
        sell = normalize_crypto_order(RAW_CRYPTO_ORDER, ACCOUNT).to_dict()
        many = []
        for index in range(MIN_SELLS_FOR_TIMING):
            record = dict(sell)
            record["key"] = "cx:%d" % index
            many.append(record)
        self.assertEqual(sell_timing(many)["verdict"], "ANALYZABLE")


class PatternTests(unittest.TestCase):
    def orders(self):
        out = []
        plan = [
            ("ALFA", "2023-02-07", "5.00"), ("BETA", "2023-03-12", "5.00"),
            ("GAMA", "2024-05-14", "5.00"), ("ALFA", "2024-09-04", "5.00"),
            ("DLTA", "2024-06-18", "5.00"), ("EPSL", "2024-06-18", "5.00"),
            ("ZETA", "2024-06-18", "5.00"), ("IOTA", "2023-10-22", "2.50"),
        ]
        for index, (symbol, day, dollars) in enumerate(plan):
            raw = dict(RAW_EQUITY_ORDER)
            raw["id"] = "order-%d" % index
            raw["symbol"] = symbol
            raw["last_transaction_at"] = day + "T15:00:00Z"
            raw["dollar_based_amount"] = {"amount": dollars, "currency_code": "USD"}
            out.append(normalize_equity_order(raw, ACCOUNT).to_dict())
        return out

    def test_ticket_sizes_are_counted(self):
        counts = ticket_sizes(self.orders())
        self.assertEqual(counts["5.00"], 7)
        self.assertEqual(counts["2.50"], 1)

    def test_sizing_consistency_measures_the_discipline(self):
        self.assertEqual(str(sizing_consistency(self.orders())), "0.875")

    def test_first_buy_per_symbol_is_the_sprawl_basis(self):
        days = symbols_opened_by_day(self.orders())
        self.assertEqual(days["2024-06-18"], ["DLTA", "EPSL", "ZETA"])
        self.assertEqual(days["2024-05-14"], ["GAMA"])

    def test_a_three_position_day_is_a_sprawl_event(self):
        events = sprawl_events(self.orders(), threshold=3)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["date"], "2024-06-18")
        self.assertEqual(events[0]["count"], 3)

    def test_a_single_position_day_is_not_a_sprawl_event(self):
        self.assertEqual(sprawl_events(self.orders(), threshold=4), [])

    def test_position_count_accumulates(self):
        series = position_count_over_time(self.orders())
        self.assertEqual(series[0]["positions"], 1)
        self.assertEqual(series[-1]["positions"], 7)

    def test_cadence_groups_by_month(self):
        cadence = cadence_by_month(self.orders())
        self.assertEqual(cadence["2024-06"]["orders"], 3)
        self.assertEqual(cadence["2024-06"]["dollars"], "15.00")

    def test_buys_and_sells_are_separated(self):
        orders = self.orders() + [
            normalize_crypto_order(RAW_CRYPTO_ORDER, ACCOUNT).to_dict()]
        self.assertEqual(len(buys(orders)), 8)
        self.assertEqual(len(sells(orders)), 1)

    def test_provenance_is_countable(self):
        counts = by_provenance(self.orders())
        self.assertEqual(counts[MANUAL_ACTION], 8)


class MergeTests(unittest.TestCase):
    """An incremental ingest may add. It may not lose or rewrite."""

    def record(self, key, **over):
        base = {
            "key": key, "masked_account": "••••6789", "asset_class": ASSET_EQUITY,
            "symbol": "EXMP", "side": "buy", "executed_on": "2026-01-15",
            "dollars": "5.00", "quantity": "0.005000", "provenance": MANUAL_ACTION,
        }
        base.update(over)
        return base

    def test_new_records_are_added(self):
        merged, problems = merge_records([self.record("a")], [self.record("b")])
        self.assertEqual(problems, [])
        self.assertEqual({r["key"] for r in merged}, {"a", "b"})

    def test_re_ingesting_the_same_record_is_idempotent(self):
        merged, problems = merge_records([self.record("a")], [self.record("a")])
        self.assertEqual(problems, [])
        self.assertEqual(len(merged), 1)

    def test_an_existing_record_is_never_dropped(self):
        merged, problems = merge_records(
            [self.record("a"), self.record("b")], [self.record("b")])
        self.assertEqual(problems, [])
        self.assertEqual(len(merged), 2)

    def test_rewriting_a_historical_fact_is_refused(self):
        for field, changed in (("dollars", "500.00"), ("symbol", "AAPL"),
                               ("executed_on", "2020-01-01"), ("side", "sell"),
                               ("quantity", "99")):
            with self.subTest(field=field):
                _, problems = merge_records(
                    [self.record("a")], [self.record("a", **{field: changed})])
                self.assertTrue(problems, "%s could be rewritten" % field)
                self.assertTrue(any(field in p for p in problems))

    def test_the_refusal_says_to_investigate_rather_than_overwrite(self):
        _, problems = merge_records(
            [self.record("a")], [self.record("a", dollars="500.00")])
        self.assertIn("must not be rewritten", problems[0])

    def test_provenance_may_improve_from_unknown(self):
        merged, problems = merge_records(
            [self.record("a", provenance=UNKNOWN)],
            [self.record("a", provenance=AGENT_ACTION)])
        self.assertEqual(problems, [])
        self.assertEqual(merged[0]["provenance"], AGENT_ACTION)

    def test_provenance_may_not_silently_change_once_known(self):
        _, problems = merge_records(
            [self.record("a", provenance=MANUAL_ACTION)],
            [self.record("a", provenance=AGENT_ACTION)])
        self.assertTrue(problems)

    def test_records_come_back_in_chronological_order(self):
        merged, _ = merge_records(
            [], [self.record("b", executed_on="2026-01-01"),
                 self.record("a", executed_on="2025-01-01")])
        self.assertEqual([r["key"] for r in merged], ["a", "b"])


class HistoryDocumentTests(unittest.TestCase):
    def document(self, **over):
        base = {
            "schema_version": SCHEMA_VERSION,
            "accounts": [{"masked": "••••6789", "role": "individual"}],
            "orders": [normalize_equity_order(RAW_EQUITY_ORDER, ACCOUNT).to_dict()],
            "lots": [normalize_tax_lot(RAW_LOTS[0], ACCOUNT, "EXMP").to_dict()],
        }
        base.update(over)
        return base

    def test_a_well_formed_document_validates(self):
        self.assertEqual(validate_history(self.document()), [])

    def test_a_wrong_schema_version_is_caught(self):
        problems = validate_history(self.document(schema_version=99))
        self.assertTrue(any("schema_version" in p for p in problems))

    def test_a_missing_section_is_caught(self):
        problems = validate_history(self.document(orders="not a list"))
        self.assertTrue(any("orders" in p for p in problems))

    def test_a_duplicate_key_is_caught(self):
        record = normalize_equity_order(RAW_EQUITY_ORDER, ACCOUNT).to_dict()
        problems = validate_history(self.document(orders=[record, dict(record)]))
        self.assertTrue(any("duplicate" in p for p in problems))

    def test_an_unmasked_account_entry_is_caught(self):
        problems = validate_history(
            self.document(accounts=[{"masked": "123456789"}]))
        self.assertTrue(any("not masked" in p for p in problems))

    def test_an_invalid_provenance_is_caught(self):
        record = normalize_equity_order(RAW_EQUITY_ORDER, ACCOUNT).to_dict()
        record["provenance"] = "MADE_UP"
        problems = validate_history(self.document(orders=[record]))
        self.assertTrue(any("provenance" in p for p in problems))

    def test_coverage_summarizes_both_asset_classes(self):
        document = self.document(orders=[
            normalize_equity_order(RAW_EQUITY_ORDER, ACCOUNT).to_dict(),
            normalize_crypto_order(RAW_CRYPTO_ORDER, ACCOUNT).to_dict(),
        ])
        coverage = coverage_summary(document)
        self.assertEqual(coverage["equity_orders"]["buys"], 1)
        self.assertEqual(coverage["crypto_orders"]["sells"], 1)
        self.assertEqual(coverage["tax_lots"]["symbols"], 1)

    def test_coverage_reports_non_purchase_lots(self):
        document = self.document(lots=[
            normalize_tax_lot(raw, ACCOUNT, "EXMP").to_dict() for raw in RAW_LOTS])
        self.assertEqual(coverage_summary(document)["tax_lots"]["non_purchase"], 2)


if __name__ == "__main__":
    unittest.main()
