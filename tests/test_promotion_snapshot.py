"""The promotion snapshot, and the one PROPOSED decision it makes possible.

Two gaps closed here, both found on 2026-09-17:

* the runner handed promotion the **capital gate's** snapshot, which carries
  settled cash and nothing else by design. Promotion needs a refreshed quote per
  asset, tradability and the broker's own order history, so it could never have
  minted a decision from that file — for any payload, on any day;
* a snapshot carries one quote, so a multi-leg plan re-checked every leg against
  the first leg's price.

The properties under test are the re-checks themselves: each one must *fail* on
its own, so that "it minted a decision" means every check actually ran and
passed rather than being skipped for want of data.
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.decision_schema import buy_leg_example  # noqa: E402
from src.promotion import (  # noqa: E402
    MAX_PROMOTION_SNAPSHOT_AGE_SECONDS,
    asset_block,
    backfill_quote_timestamps,
    promotion_refresh_problems,
    snapshot_from_dict,
    snapshot_problems,
)
from tests.helpers import make_config, make_state  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = datetime(2026, 9, 17, 15, 30, tzinfo=timezone.utc)

#: What the capital gate writes, verbatim in shape. It is a valid JSON object
#: with a real cash figure, which is exactly why it is dangerous here.
CAPITAL_GATE_SNAPSHOT = {
    "as_of": "2026-09-17T15:30:00Z",
    "account_is_agentic": True,
    "account_masked": "••••0002",
    "account_type": "limited_margin",
    "cash_usd": "25.00",
    "unsettled_funds_usd": "0.00",
    "buying_power_usd": "25.00",
}


def promotion_snapshot(**over):
    """A complete promotion snapshot for a single SNDK leg."""
    snapshot = {
        "schema_version": 1,
        "as_of": "2026-09-17T15:30:00Z",
        "account_is_agentic": True,
        "account_masked": "••••0002",
        "account_type": "limited_margin",
        "cash_usd": "25.00",
        "unsettled_funds_usd": "0.00",
        "buying_power_usd": "25.00",
        "equity_orders": [],
        "crypto_orders": [],
        "assets": {
            "SNDK": {
                "quote_price_usd": "1609.19",
                "quote_timestamp": "2026-09-17T15:29:30Z",
                "tradable": True,
                "fractional_tradable": True,
                "account_type_tradable": True,
            },
        },
        "read_errors": [],
    }
    snapshot.update(over)
    return snapshot


class SnapshotShapeTests(unittest.TestCase):
    def test_the_capital_gate_snapshot_is_not_a_promotion_snapshot(self):
        problems = snapshot_problems(CAPITAL_GATE_SNAPSHOT)
        self.assertTrue(problems)
        self.assertTrue(any("equity_orders" in p for p in problems), problems)
        self.assertTrue(any("per-asset" in p for p in problems), problems)

    def test_a_complete_promotion_snapshot_is_accepted(self):
        self.assertEqual(snapshot_problems(promotion_snapshot()), [])

    def test_each_leg_gets_its_own_quote(self):
        """One snapshot, two legs, two prices — never the first leg's twice."""
        raw = promotion_snapshot()
        raw["assets"]["BTC-USD"] = {
            "quote_price_usd": "80000.00",
            "quote_timestamp": "2026-09-17T15:29:50Z",
            "tradable": True,
            "fractional_tradable": True,
            "account_type_tradable": True,
            "crypto_pair_halted": False,
            "crypto_min_order_size": "1.00",
        }
        sndk = snapshot_from_dict(raw, NOW, symbol="SNDK")
        btc = snapshot_from_dict(raw, NOW, symbol="BTC-USD")
        self.assertEqual(str(sndk.quote_price_usd), "1609.19")
        self.assertEqual(str(btc.quote_price_usd), "80000.00")
        self.assertIsNone(sndk.crypto_min_order_size)
        self.assertEqual(str(btc.crypto_min_order_size), "1.00")

    def test_a_symbol_the_snapshot_does_not_carry_stays_empty(self):
        """Absent data must never be silently borrowed from another asset."""
        self.assertEqual(asset_block(promotion_snapshot(), "MU"), {})
        missing = snapshot_from_dict(promotion_snapshot(), NOW, symbol="MU")
        self.assertIsNone(missing.quote_price_usd)
        self.assertIsNone(missing.tradable)

    def test_account_level_facts_are_shared_across_legs(self):
        for symbol in ("SNDK", "MU"):
            snapshot = snapshot_from_dict(promotion_snapshot(), NOW, symbol=symbol)
            self.assertEqual(str(snapshot.cash_usd), "25.00")
            self.assertEqual(snapshot.equity_orders, [])


class RefreshVerificationTests(unittest.TestCase):
    """A gather that wrote nothing must not pass as a gather."""

    def stamp(self, seconds_ago=0):
        moment = NOW - timedelta(seconds=seconds_ago)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_a_complete_fresh_gather_passes(self):
        raw = promotion_snapshot(as_of=self.stamp(5))
        self.assertEqual(
            promotion_refresh_problems(raw, 1000.0, 999.0, ["SNDK"], now=NOW), [])

    def test_a_snapshot_predating_the_invocation_is_a_leftover(self):
        raw = promotion_snapshot(as_of=self.stamp(5))
        problems = promotion_refresh_problems(raw, 900.0, 999.0, ["SNDK"], now=NOW)
        self.assertTrue(any("predates this invocation" in p for p in problems),
                        problems)

    def test_a_stale_gather_is_refused(self):
        raw = promotion_snapshot(
            as_of=self.stamp(MAX_PROMOTION_SNAPSHOT_AGE_SECONDS + 60))
        problems = promotion_refresh_problems(raw, 1000.0, 999.0, ["SNDK"], now=NOW)
        self.assertTrue(any("past the" in p for p in problems), problems)

    def test_a_missing_symbol_is_refused(self):
        raw = promotion_snapshot(as_of=self.stamp(5))
        problems = promotion_refresh_problems(
            raw, 1000.0, 999.0, ["SNDK", "BTC-USD"], now=NOW)
        self.assertTrue(any("no block for BTC-USD" in p for p in problems),
                        problems)

    def test_the_capital_gate_snapshot_never_passes_the_gather_check(self):
        raw = dict(CAPITAL_GATE_SNAPSHOT, as_of=self.stamp(5))
        problems = promotion_refresh_problems(raw, 1000.0, 999.0, ["SNDK"], now=NOW)
        self.assertTrue(problems)

    def test_nothing_written_is_refused(self):
        self.assertEqual(
            promotion_refresh_problems(None, None, 999.0, ["SNDK"], now=NOW),
            ["no snapshot was written"])


class UndatedQuotesAreStampedConservatively(unittest.TestCase):
    """The broker's quote payload does not always carry a timestamp.

    An undated quote fails closed as STALE_QUOTE, which is the right default and
    the wrong outcome for a quote this invocation demonstrably just read. The
    script supplies what it observed rather than asking the model to assert it —
    from the moment the gather *started*, so the stamp can only make a quote look
    older than it is.
    """

    def test_an_undated_quote_is_stamped_from_the_gather_start(self):
        raw = promotion_snapshot()
        del raw["assets"]["SNDK"]["quote_timestamp"]
        started = NOW - timedelta(seconds=90)
        self.assertEqual(backfill_quote_timestamps(raw, started), ["SNDK"])
        self.assertEqual(raw["assets"]["SNDK"]["quote_timestamp"],
                         "2026-09-17T15:28:30Z")

    def test_the_stamp_is_never_newer_than_the_gather(self):
        """It may only age a quote, never freshen one."""
        raw = promotion_snapshot()
        del raw["assets"]["SNDK"]["quote_timestamp"]
        started = NOW - timedelta(seconds=240)
        backfill_quote_timestamps(raw, started)
        stamped = datetime.strptime(raw["assets"]["SNDK"]["quote_timestamp"],
                                    "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertLessEqual(stamped, NOW)
        self.assertEqual(stamped, started)

    def test_a_dated_quote_is_left_alone(self):
        raw = promotion_snapshot()
        original = raw["assets"]["SNDK"]["quote_timestamp"]
        self.assertEqual(backfill_quote_timestamps(raw, NOW), [])
        self.assertEqual(raw["assets"]["SNDK"]["quote_timestamp"], original)

    def test_an_asset_with_no_quote_at_all_is_not_invented(self):
        """A missing price must stay a blocker, not acquire a timestamp."""
        raw = promotion_snapshot()
        del raw["assets"]["SNDK"]["quote_price_usd"]
        del raw["assets"]["SNDK"]["quote_timestamp"]
        self.assertEqual(backfill_quote_timestamps(raw, NOW), [])
        self.assertNotIn("quote_timestamp", raw["assets"]["SNDK"])

    def test_a_stamped_snapshot_then_passes_verification(self):
        raw = promotion_snapshot(as_of=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
        del raw["assets"]["SNDK"]["quote_timestamp"]
        self.assertTrue(
            promotion_refresh_problems(raw, 1000.0, 999.0, ["SNDK"], now=NOW))
        backfill_quote_timestamps(raw, NOW - timedelta(seconds=30))
        self.assertEqual(
            promotion_refresh_problems(raw, 1000.0, 999.0, ["SNDK"], now=NOW), [])

    def test_the_gather_script_performs_the_backfill(self):
        with open(os.path.join(REPO_ROOT, "scripts",
                               "refresh_promotion_snapshot.sh"),
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("backfill_quote_timestamps", body)
        self.assertIn("started_at", body)


class PromotionMintsExactlyOneProposedDecision(unittest.TestCase):
    """The whole deterministic path, end to end, writing nothing real.

    `log_decision` and `save_last_evaluation` are captured rather than executed:
    the repository's own `logs/decisions.jsonl` is an append-only audit record
    and a test may not add to it.
    """

    def setUp(self):
        path = os.path.join(REPO_ROOT, "scripts", "promote_latest_recommendation.py")
        spec = importlib.util.spec_from_file_location("_promote_e2e", path)
        self.promote = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.promote)

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        reports = os.path.join(self.tmp, "reports")
        os.makedirs(os.path.join(reports, "recommendations"))

        from tests.test_reporting import VALID_DIGEST

        head = VALID_DIGEST.index("> **ACTION:")
        digest = (
            VALID_DIGEST[:head]
            + "> **ACTION: BUY $10.00 SNDK**\n>\n"
              "> - **Remaining monthly authorization:** $25.00 before, $15.00 after\n"
              "> - **Confidence:** MEDIUM\n"
              "> - **Why:** The contracted revenue floor is verified in a filing.\n"
              "> - **Human approval required:** Yes — nothing here is approved or "
              "submitted.\n\n"
            + VALID_DIGEST[VALID_DIGEST.index("## Status"):]
        ).replace("DECISION: WAIT", "DECISION: SINGLE_BUY")
        digest = digest.replace("reports/2026-09-08_1030.md",
                                "reports/2026-09-17_1030.md")

        self.digest_path = os.path.join(reports, "latest.md")
        with open(self.digest_path, "w", encoding="utf-8") as handle:
            handle.write(digest)
        with open(os.path.join(reports, "2026-09-17_1030.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("# audit record\n\nDECISION: SINGLE_BUY\n")

        self.reports = reports
        self.promote.REPORTS_DIR = reports
        self.promote.RECOMMENDATIONS_DIR = os.path.join(reports, "recommendations")
        self.promote.DIGEST_PATH = self.digest_path

        self.config = make_config()
        self.state = make_state(self.config)
        self.logged = []
        self.last_evaluations = []

        self.promote.load_config = lambda *a, **k: self.config
        self.promote.load_budget_state = lambda *a, **k: self.state
        self.promote.load_approvals = lambda *a, **k: {}
        self.promote.load_executions = lambda *a, **k: {}
        self.promote.get_approval = lambda *a, **k: None
        self.promote.load_crypto_universe = lambda *a, **k: None
        self.promote.utc_now = lambda: NOW
        self.promote.log_decision = self.logged.append
        self.promote.save_last_evaluation = (
            lambda payload, *a, **k: self.last_evaluations.append(payload))

    # -- fixtures ----------------------------------------------------------

    def recommendation(self, **leg_over):
        leg = buy_leg_example(NOW)
        leg.update(leg_over)
        return {
            "schema_version": 1,
            "generated_at": "2026-09-17T15:29:00Z",
            "source_report": "reports/2026-09-17_1030.md",
            "plan_type": "SINGLE_BUY",
            "legs": [leg],
        }

    def run_promotion(self, recommendation=None, snapshot=None, extra=()):
        argv = []
        if recommendation is not None:
            path = os.path.join(self.reports, "recommendations", "2026-09-17_1030.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(recommendation, handle)
            argv += ["--recommendation", path]
        if snapshot is not None:
            path = os.path.join(self.tmp, "promotion_snapshot.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle)
            argv += ["--snapshot", path]
        argv += ["--digest", self.digest_path]
        argv += list(extra)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.promote.main(argv)
        return code, out.getvalue(), err.getvalue()

    # -- the happy path ----------------------------------------------------

    def test_a_valid_buy_and_a_complete_snapshot_mint_one_proposed_decision(self):
        code, out, err = self.run_promotion(self.recommendation(),
                                            promotion_snapshot())
        self.assertEqual(code, 0, err or out)
        self.assertEqual(len(self.logged), 1, "exactly one decision")
        record = self.logged[0]
        self.assertEqual(record["decision"], "BUY")
        self.assertEqual(record["ticker"], "SNDK")
        self.assertTrue(str(record["decision_id"]).startswith("dec_"))
        self.assertIn("PROMOTED", out)
        self.assertIn("PROPOSED — not approved, not submitted", out)

    def test_it_creates_no_approval_and_no_execution_authority(self):
        code, out, err = self.run_promotion(self.recommendation(),
                                            promotion_snapshot())
        self.assertEqual(code, 0, err or out)
        record = self.logged[0]
        self.assertNotIn("approved", record)
        self.assertNotIn("approval", record)
        self.assertIs(record.get("executable", False), False)
        self.assertNotEqual(record.get("execution_status"), "EXECUTED")
        self.assertIn("Nothing is approved", out)
        self.assertIn("Execution remains disabled", out)
        # The switches were verified closed rather than ignored.
        self.assertIn("AGENT_DISABLED", out)
        self.assertIn("EXECUTION_MODE_DRY_RUN", out)
        self.assertIn("LIVE_TRADING_DISABLED", out)

    def test_a_dry_run_mints_nothing_at_all(self):
        code, out, err = self.run_promotion(self.recommendation(),
                                            promotion_snapshot(),
                                            extra=("--dry-run",))
        self.assertEqual(code, 0, err or out)
        self.assertEqual(self.logged, [])
        self.assertIn("Nothing was written", out)

    # -- each re-check must be able to fail on its own ---------------------

    def test_a_stale_quote_blocks_promotion(self):
        snapshot = promotion_snapshot()
        snapshot["assets"]["SNDK"]["quote_timestamp"] = "2026-09-17T14:00:00Z"
        code, out, err = self.run_promotion(self.recommendation(), snapshot)
        self.assertEqual(code, 1)
        self.assertEqual(self.logged, [])
        self.assertIn("STALE_QUOTE", out)

    def test_a_quote_beyond_the_slippage_tolerance_blocks_promotion(self):
        snapshot = promotion_snapshot()
        snapshot["assets"]["SNDK"]["quote_price_usd"] = "1900.00"
        code, out, err = self.run_promotion(self.recommendation(), snapshot)
        self.assertEqual(code, 1)
        self.assertEqual(self.logged, [])

    def test_missing_tradability_blocks_promotion(self):
        snapshot = promotion_snapshot()
        del snapshot["assets"]["SNDK"]["tradable"]
        code, out, err = self.run_promotion(self.recommendation(), snapshot)
        self.assertEqual(code, 1)
        self.assertEqual(self.logged, [])
        self.assertIn("TRADABILITY_UNKNOWN", out)

    def test_an_untradable_asset_blocks_promotion(self):
        snapshot = promotion_snapshot()
        snapshot["assets"]["SNDK"]["tradable"] = False
        code, out, err = self.run_promotion(self.recommendation(), snapshot)
        self.assertEqual(code, 1)
        self.assertEqual(self.logged, [])

    def test_unreadable_orders_block_reconciliation(self):
        """A failed order read must never look like an account with no orders."""
        snapshot = promotion_snapshot()
        snapshot["equity_orders"] = None
        code, out, err = self.run_promotion(self.recommendation(), snapshot)
        self.assertEqual(code, 2, err or out)
        self.assertEqual(self.logged, [])
        self.assertIn("not a promotion snapshot", err)

    def test_a_pending_agentic_order_this_month_blocks_promotion(self):
        """Dollars already committed at the broker are not available again."""
        snapshot = promotion_snapshot()
        snapshot["equity_orders"] = [{
            "id": "ord_1",
            "symbol": "SNDK",
            "side": "buy",
            "state": "queued",
            "created_at": "2026-09-16T15:30:00Z",
            "quantity": "0.01",
            "price": "1609.19",
            "total_notional": {"amount": "20.00", "currency": "USD"},
            "placed_agent": "AGENT",
        }]
        code, out, err = self.run_promotion(self.recommendation(), snapshot)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.logged, [])

    def test_the_capital_gate_snapshot_is_refused_outright(self):
        """The 2026-09-17 accident: the wrong file, confidently passed in."""
        code, out, err = self.run_promotion(self.recommendation(),
                                            CAPITAL_GATE_SNAPSHOT)
        self.assertEqual(code, 2)
        self.assertEqual(self.logged, [])
        self.assertIn("is not a promotion snapshot", err)
        self.assertIn("refresh_promotion_snapshot.sh", err)

    def test_no_snapshot_at_all_fails_closed(self):
        code, out, err = self.run_promotion(self.recommendation(), None)
        self.assertEqual(code, 1)
        self.assertEqual(self.logged, [])

    def test_an_ad_hoc_payload_is_blocked_by_the_guardrails(self):
        """The 2026-09-17 payload shape, through the whole path."""
        leg = buy_leg_example(NOW)
        sprawl = leg.pop("portfolio_sprawl_assessment")
        sprawl["positions_held"] = sprawl.pop("position_count")
        leg["portfolio_sprawl_assessment"] = sprawl
        code, out, err = self.run_promotion(self.recommendation(**leg),
                                            promotion_snapshot())
        self.assertEqual(code, 1)
        self.assertEqual(self.logged, [])
        self.assertIn("MISSING_SPRAWL_ASSESSMENT", out)

    # -- WAIT ---------------------------------------------------------------

    def test_a_wait_digest_promotes_nothing_and_is_not_an_error(self):
        from tests.test_reporting import VALID_DIGEST

        with open(self.digest_path, "w", encoding="utf-8") as handle:
            handle.write(VALID_DIGEST)
        code, out, err = self.run_promotion(None, promotion_snapshot())
        self.assertEqual(code, 0, err or out)
        self.assertEqual(self.logged, [])
        self.assertEqual(self.last_evaluations, [])
        self.assertIn("Nothing to promote", out)
        self.assertIn("normal, frequent outcome", out)

    def test_a_wait_with_a_stray_payload_still_promotes_nothing(self):
        from tests.test_reporting import VALID_DIGEST

        with open(self.digest_path, "w", encoding="utf-8") as handle:
            handle.write(VALID_DIGEST)
        code, out, err = self.run_promotion(self.recommendation(),
                                            promotion_snapshot())
        self.assertEqual(code, 0, err or out)
        self.assertEqual(self.logged, [])


class ScheduledRunStillCannotPromoteItself(unittest.TestCase):
    """The safety boundary the snapshot work must not have moved."""

    def setUp(self):
        path = os.path.join(REPO_ROOT, "scripts", "promote_latest_recommendation.py")
        spec = importlib.util.spec_from_file_location("_promote_marker", path)
        self.promote = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.promote)

    def run_with(self, **env):
        from src.scheduling import SCHEDULED_POST_RUN_MARKER, SCHEDULED_RUN_MARKER

        previous = {}
        for key in (SCHEDULED_RUN_MARKER, SCHEDULED_POST_RUN_MARKER):
            previous[key] = os.environ.get(key)
            os.environ.pop(key, None)
        for key, value in env.items():
            os.environ[key] = value
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.promote.main(["--dry-run"])
            return code, out.getvalue(), err.getvalue()
        finally:
            for key, value in previous.items():
                os.environ.pop(key, None)
                if value is not None:
                    os.environ[key] = value

    def test_the_model_process_of_a_scheduled_run_is_refused(self):
        from src.scheduling import SCHEDULED_RUN_MARKER

        code, out, err = self.run_with(**{SCHEDULED_RUN_MARKER: "1"})
        self.assertEqual(code, 4)
        self.assertIn("a model may not do for its own recommendation",
                      err.replace("\n", " "))

    def test_both_markers_at_once_is_still_the_model(self):
        from src.scheduling import SCHEDULED_POST_RUN_MARKER, SCHEDULED_RUN_MARKER

        code, out, err = self.run_with(**{SCHEDULED_RUN_MARKER: "1",
                                          SCHEDULED_POST_RUN_MARKER: "1"})
        self.assertEqual(code, 4)


class TheGatherIsNarrowAndReadOnly(unittest.TestCase):
    """The new broker access must not be a new capability."""

    def setUp(self):
        with open(os.path.join(REPO_ROOT, "scripts",
                               "refresh_promotion_snapshot.sh"),
                  encoding="utf-8") as handle:
            self.body = handle.read()

    def test_no_order_tool_is_allowed(self):
        from src.scheduling import ORDER_TOOLS

        allowed = self.body[self.body.index('ALLOWED="'):
                            self.body.index('DISALLOWED="')]
        for tool in ORDER_TOOLS:
            self.assertNotIn(tool, allowed, "%s is reachable by the gather" % tool)

    def test_every_order_tool_is_explicitly_denied(self):
        from src.scheduling import ORDER_TOOLS

        disallowed = self.body[self.body.index('DISALLOWED="'):]
        for tool in ORDER_TOOLS:
            self.assertIn(tool, disallowed, "%s is not denied" % tool)

    def test_it_arms_its_own_write_scope_and_not_another(self):
        from src.scheduling import (
            PROMOTION_SNAPSHOT_MARKER,
            SCHEDULED_RUN_MARKER,
            SNAPSHOT_REFRESH_MARKER,
        )

        self.assertIn("export %s=1" % PROMOTION_SNAPSHOT_MARKER, self.body)
        self.assertNotIn("export %s=1" % SNAPSHOT_REFRESH_MARKER, self.body)
        self.assertNotIn("export %s=1" % SCHEDULED_RUN_MARKER, self.body)

    def test_it_approves_nothing_and_submits_nothing(self):
        # The order tools appear once each, in the deny list, and nowhere else.
        for forbidden in ("approve_decision", "submit_approved",
                          "execute_approved", "state/approvals.json"):
            self.assertNotIn(forbidden, self.body)
        self.assertEqual(self.body.count("place_equity_order"), 1)

    def test_the_three_write_scopes_are_disjoint(self):
        from src.scheduling import (
            PROMOTION_SNAPSHOT_PATH,
            WRITABLE_DURING_PROMOTION_SNAPSHOT,
            WRITABLE_DURING_SCHEDULED_RUN,
            WRITABLE_DURING_SNAPSHOT_REFRESH,
            is_writable_during_promotion_snapshot,
            is_writable_during_scheduled_run,
            is_writable_during_snapshot_refresh,
        )

        self.assertTrue(is_writable_during_promotion_snapshot(PROMOTION_SNAPSHOT_PATH))
        self.assertFalse(is_writable_during_snapshot_refresh(PROMOTION_SNAPSHOT_PATH))
        self.assertFalse(is_writable_during_scheduled_run(PROMOTION_SNAPSHOT_PATH))
        for path in WRITABLE_DURING_SNAPSHOT_REFRESH:
            self.assertFalse(is_writable_during_promotion_snapshot(path))
        for path in WRITABLE_DURING_SCHEDULED_RUN:
            self.assertFalse(is_writable_during_promotion_snapshot(path))
        self.assertEqual(WRITABLE_DURING_PROMOTION_SNAPSHOT,
                         (PROMOTION_SNAPSHOT_PATH,))

    def test_the_write_guard_enforces_that_scope(self):
        with open(os.path.join(REPO_ROOT, "scripts", "scheduled_write_guard.py"),
                  encoding="utf-8") as handle:
            guard = handle.read()
        self.assertIn("PROMOTION_SNAPSHOT_MARKER", guard)
        self.assertIn("is_writable_during_promotion_snapshot", guard)

    def test_the_runner_gathers_it_before_promoting(self):
        with open(os.path.join(REPO_ROOT, "scripts", "scheduled_evaluation.sh"),
                  encoding="utf-8") as handle:
            runner = handle.read()
        gather = runner.index("refresh_promotion_snapshot.sh")
        promote = runner.index("promote_latest_recommendation.py")
        self.assertLess(gather, promote,
                        "the snapshot must be gathered before promotion runs")
        self.assertIn("PROMOTION_SNAPSHOT", runner)

    def test_the_capital_gate_refresh_is_untouched(self):
        """The cheap morning check must not have grown tools."""
        with open(os.path.join(REPO_ROOT, "scripts", "refresh_broker_snapshot.sh"),
                  encoding="utf-8") as handle:
            gate = handle.read()
        allowed = gate[gate.index('ALLOWED="'):gate.index('DISALLOWED="')]
        self.assertIn("get_accounts", allowed)
        self.assertIn("get_portfolio", allowed)
        for widened in ("get_equity_quotes", "get_crypto_quotes",
                        "get_equity_orders", "get_equity_tradability"):
            self.assertNotIn(widened, allowed,
                             "the capital gate must stay two account tools")


if __name__ == "__main__":
    unittest.main()
