"""The ingest script — end to end, including its refusals.

``state/portfolio_history.json`` is personal financial history, and the whole
point of these tests is the behaviour when something goes wrong: a corrupted
file, a re-ingest that would drop records, a broker response that contradicts
what is already stored, or a payload that would write an account number to
disk. In every one of those cases the script must refuse and leave the existing
file untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "ingest_history.py")

RAW_EQUITY = {
    "id": "11111111-2222-4333-8444-555555555555",
    "symbol": "EXMP", "side": "buy", "state": "filled",
    "cumulative_quantity": "0.005000", "average_price": "1000.000000",
    "dollar_based_amount": {"amount": "5.000000", "currency_code": "USD"},
    "placed_agent": "user",
    "last_transaction_at": "2026-01-15T15:30:00.482Z",
}
RAW_CRYPTO = {
    "id": "22222222-3333-4444-8555-666666666666",
    "currency_code": "BTC", "side": "sell", "state": "filled",
    "cumulative_quantity": "0.00050000",
    # 18 decimal places — a real shape from the audited data, and the value
    # that first tripped the account-number guard as a false positive.
    "average_price": "60000.987654321098765432",
    "total_executed_notional": "30.00",
    "created_at": "2025-06-20T12:00:00.926309-04:00",
}
RAW_LOT = {
    "open_lot_id": "33333333-4444-4555-8666-777777777777",
    "open_tran_type": "buy", "quantity": "0.025000",
    "cost_per_share": "200.000000", "tax_cost_basis": "5.000000",
    "open_date": "2026-01-05", "term": "st",
    # A numeric 16-digit lot order_id: must be dropped, not stored.
    "order_id": "1200000000000001",
}


def staging(**over) -> dict:
    document = {
        "accounts": [
            {"account_number": "123456789", "role": "individual", "agentic": False},
        ],
        "equity_orders": [{"account_number": "123456789", "orders": [RAW_EQUITY]}],
        "crypto_orders": [{"account_number": "123456789", "orders": [RAW_CRYPTO]}],
        "tax_lots": [{"account_number": "123456789", "symbol": "EXMP",
                      "lots": [RAW_LOT]}],
    }
    document.update(over)
    return document


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.history = os.path.join(self.dir, "portfolio_history.json")
        self.staging = os.path.join(self.dir, "staging.json")
        self.write_staging(staging())

    def write_staging(self, document):
        with open(self.staging, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

    def run_ingest(self, *args, staging_path=None):
        return subprocess.run(
            [sys.executable, SCRIPT,
             "--staging", staging_path or self.staging,
             "--history", self.history] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO_ROOT)

    def stored(self):
        with open(self.history, "r", encoding="utf-8") as handle:
            return json.load(handle)

    # --- the happy path --------------------------------------------------

    def test_a_full_ingest_stores_orders_and_lots(self):
        result = self.run_ingest("--full")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        document = self.stored()
        self.assertEqual(len(document["orders"]), 2)
        self.assertEqual(len(document["lots"]), 1)
        self.assertEqual(document["ingest_count"], 1)

    def test_account_identifiers_are_stored_masked(self):
        self.run_ingest("--full")
        self.assertEqual(self.stored()["accounts"][0]["masked"], "••••6789")
        for order in self.stored()["orders"]:
            self.assertEqual(order["masked_account"], "••••6789")

    def test_the_file_on_disk_holds_no_account_number_in_any_encoding(self):
        self.run_ingest("--full")
        with open(self.history, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertIn("••••6789", raw)
        self.assertNotIn("123456789", raw)
        self.assertNotIn("\\u2022", raw)

    def test_an_18_decimal_price_is_not_mistaken_for_an_account_number(self):
        """The false positive that first blocked a legitimate ingest."""
        result = self.run_ingest("--full")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn("60000.987654321098765432", json.dumps(self.stored()))

    def test_the_digest_is_never_scanned_as_an_identifier(self):
        """A SHA-256 can contain nine consecutive digits by chance.

        Scanning it made the ingest fail at random, which is worse than not
        scanning a hash of the body at all.
        """
        self.run_ingest("--full")
        digest = self.stored()["digest"]
        self.assertEqual(len(digest), 64)
        # Re-ingesting must keep succeeding whatever the digest happens to be.
        for _ in range(3):
            self.assertEqual(self.run_ingest().returncode, 0)

    def test_a_tax_lots_numeric_order_id_is_not_stored(self):
        self.run_ingest("--full")
        self.assertNotIn("1200000000000001", json.dumps(self.stored()))

    def test_provenance_is_preserved_per_asset_class(self):
        self.run_ingest("--full")
        orders = {o["asset_class"]: o["provenance"] for o in self.stored()["orders"]}
        self.assertEqual(orders["EQUITY"], "MANUAL_ACTION")
        self.assertEqual(orders["CRYPTO"], "UNKNOWN")

    # --- incremental ------------------------------------------------------

    def test_re_ingesting_the_same_data_is_idempotent(self):
        self.run_ingest("--full")
        self.assertEqual(self.run_ingest().returncode, 0)
        document = self.stored()
        self.assertEqual(len(document["orders"]), 2)
        self.assertEqual(document["ingest_count"], 2)

    def test_an_incremental_ingest_adds_without_losing(self):
        self.run_ingest("--full")
        newer = dict(RAW_EQUITY)
        newer["id"] = "aaaaaaaa-1111-2222-3333-444444444444"
        newer["symbol"] = "GOOGL"
        self.write_staging(staging(
            equity_orders=[{"account_number": "123456789", "orders": [newer]}],
            crypto_orders=[], tax_lots=[]))
        self.assertEqual(self.run_ingest().returncode, 0)
        symbols = {o["symbol"] for o in self.stored()["orders"]}
        self.assertEqual(symbols, {"EXMP", "BTC-USD", "GOOGL"})

    def test_a_second_full_ingest_is_refused(self):
        self.run_ingest("--full")
        result = self.run_ingest("--full")
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing --full", result.stderr.decode())

    def test_a_dry_run_writes_nothing(self):
        self.run_ingest("--full")
        before = self.stored()
        newer = dict(RAW_EQUITY)
        newer["id"] = "bbbbbbbb-1111-2222-3333-444444444444"
        self.write_staging(staging(
            equity_orders=[{"account_number": "123456789", "orders": [newer]}],
            crypto_orders=[], tax_lots=[]))
        result = self.run_ingest("--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.stored(), before)

    # --- the refusals -----------------------------------------------------

    def test_a_rewritten_historical_fact_is_refused(self):
        self.run_ingest("--full")
        before = self.stored()
        tampered = dict(RAW_EQUITY)
        tampered["dollar_based_amount"] = {"amount": "500.00", "currency_code": "USD"}
        self.write_staging(staging(
            equity_orders=[{"account_number": "123456789", "orders": [tampered]}],
            crypto_orders=[], tax_lots=[]))
        result = self.run_ingest()
        self.assertEqual(result.returncode, 1)
        self.assertIn("must not be rewritten", result.stderr.decode())
        self.assertEqual(self.stored(), before, "the file was modified anyway")

    def test_a_corrupted_history_is_not_merged_into(self):
        self.run_ingest("--full")
        document = self.stored()
        document["orders"][0]["dollars"] = "999.99"   # edited outside the script
        with open(self.history, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        result = self.run_ingest()
        self.assertEqual(result.returncode, 1)
        self.assertIn("integrity digest", result.stderr.decode())

    def test_unreadable_json_is_not_overwritten(self):
        with open(self.history, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        result = self.run_ingest("--full")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to overwrite", result.stderr.decode())
        with open(self.history, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{ this is not json")

    def test_an_unmasked_account_number_in_a_payload_is_refused(self):
        """A leak anywhere in the document blocks the write."""
        self.write_staging(staging(notes=["account 123456789 is the individual one"]))
        result = self.run_ingest("--full")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unmasked account-shaped identifier", result.stderr.decode())
        self.assertFalse(os.path.exists(self.history))

    def test_a_backup_is_written_before_replacement(self):
        self.run_ingest("--full")
        self.assertEqual(self.run_ingest().returncode, 0)
        self.assertTrue(os.path.exists(self.history + ".bak"))

    # --- reporting --------------------------------------------------------

    def test_summary_reports_without_changing_anything(self):
        self.run_ingest("--full")
        before = self.stored()
        result = subprocess.run(
            [sys.executable, SCRIPT, "--summary", "--history", self.history],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0)
        output = result.stdout.decode()
        self.assertIn("PORTFOLIO HISTORY", output)
        self.assertIn("never authorization", output)
        self.assertEqual(self.stored(), before)

    def test_the_summary_masks_accounts(self):
        self.run_ingest("--full")
        result = subprocess.run(
            [sys.executable, SCRIPT, "--summary", "--history", self.history],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=REPO_ROOT)
        self.assertIn("••••6789", result.stdout.decode())
        self.assertNotIn("123456789", result.stdout.decode())

    def test_the_script_calls_no_broker_tool(self):
        with open(SCRIPT, encoding="utf-8") as handle:
            body = handle.read()
        for tool in ("place_equity_order", "place_crypto_order", "approve_decision",
                     "submit_approved", "review_equity_order"):
            with self.subTest(tool=tool):
                self.assertNotIn(tool, body)


class ShippedHistoryTests(unittest.TestCase):
    """The real ingested file, when it exists on this machine."""

    HISTORY = os.path.join(REPO_ROOT, "state", "portfolio_history.json")

    def setUp(self):
        if not os.path.exists(self.HISTORY):
            self.skipTest("no local portfolio history ingested yet")
        with open(self.HISTORY, encoding="utf-8") as handle:
            self.document = json.load(handle)

    def test_it_validates(self):
        from src.history import validate_history

        self.assertEqual(validate_history(self.document), [])

    def test_it_carries_no_unmasked_account_identifier(self):
        from src.history import unmasked_account_numbers

        payload = {k: v for k, v in self.document.items() if k != "digest"}
        self.assertEqual(unmasked_account_numbers(json.dumps(payload)), [])

    def test_every_account_is_masked(self):
        for account in self.document.get("accounts") or []:
            with self.subTest(account=account):
                self.assertTrue(str(account["masked"]).startswith("••••"))

    def test_it_passes_its_own_integrity_digest(self):
        import hashlib

        payload = {k: v for k, v in self.document.items() if k != "digest"}
        expected = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(self.document["digest"], expected)

    def test_the_realized_pnl_conflict_is_recorded_as_an_anomaly(self):
        blob = " ".join(self.document.get("anomalies") or [])
        self.assertIn("get_pnl_trade_history", blob)
        self.assertIn("authoritative", blob)

    def test_the_split_adjustment_finding_is_recorded(self):
        blob = " ".join(self.document.get("anomalies") or [])
        self.assertIn("split-adjusted", blob)

    def test_crypto_orders_are_not_attributed_to_a_human_by_assumption(self):
        from src.history import ASSET_CRYPTO, MANUAL_ACTION

        for order in self.document["orders"]:
            if order["asset_class"] == ASSET_CRYPTO:
                with self.subTest(key=order["key"]):
                    self.assertNotEqual(order["provenance"], MANUAL_ACTION)


if __name__ == "__main__":
    unittest.main()
