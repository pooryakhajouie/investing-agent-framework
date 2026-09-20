"""Stage 5: submission tickets, the bridge, and full mocked lifecycles.

Every broker interaction is a synthetic dict or a mock submitter. No Robinhood
tool is called. `DisabledSubmitter` is what ships.
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

from src.execution import (  # noqa: E402
    ExecutionDisabled, ExecutionState, preflight,
)
from src.execution_store import ExecutionRecord, read_audit, transition  # noqa: E402
from src.submission import (  # noqa: E402
    HANDOFF_PATH,
    TICKET_TTL_SECONDS,
    BrokerResponse,
    DisabledSubmitter,
    ManualHandoffSubmitter,
    SubmissionError,
    SubmissionHandoffRequired,
    SubmissionTicket,
    Submitter,
    TicketConsumed,
    create_ticket,
    ingest_response,
    load_tickets,
    normalize_broker_response,
    reconcile_submission,
    save_tickets,
    submit_once,
    verify_ticket,
)
from tests.test_execution import (  # noqa: E402
    NOW, approve, crypto_dec, crypto_snapshot, equity_decision, live_config,
    month, run, snapshot,
)
from tests.helpers import make_config, make_state  # noqa: E402


# --------------------------------------------------------------------------
# Mock submitters — the only things that ever "reach" a broker in tests
# --------------------------------------------------------------------------


class MockSubmitter(Submitter):
    name = "mock"

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = 0

    def submit(self, ticket):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.response or {
            "id": "brk_order_1", "state": "confirmed",
            "symbol": ticket.asset, "ref_id": ticket.ref_id,
        }


class CrashingSubmitter(Submitter):
    """Simulates the broker receiving the order, then the process dying."""

    name = "crashing"

    def __init__(self):
        self.calls = 0
        self.order_reached_broker = False

    def submit(self, ticket):
        self.calls += 1
        self.order_reached_broker = True   # the broker DID get it
        raise KeyboardInterrupt("process killed immediately after the call")


def broker_order(ref_id, amount, state="filled", executed=None, order_id="brk_order_1",
                 symbol="ISRG", day=20):
    raw = {
        "id": order_id, "symbol": symbol, "side": "buy", "state": state,
        "created_at": "%s-%02dT15:00:05Z" % (month(), day),
        "dollar_based_amount": {"amount": amount, "currency_code": "USD"},
        "ref_id": ref_id,
        "cumulative_quantity": "0", "average_price": None,
    }
    if executed:
        raw["cumulative_quantity"], raw["average_price"] = executed
    elif state == "filled":
        raw["cumulative_quantity"], raw["average_price"] = "1", amount
    return raw


class TicketTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tickets_path = os.path.join(self.tmp, "tickets.json")
        self.audit_path = os.path.join(self.tmp, "audit.jsonl")
        self.config = live_config()

    def mint(self, decision=None, snap=None, amount="10.00"):
        decision = decision or equity_decision(amount)
        snap = snap or snapshot()
        approval = approve(decision)
        result = preflight(decision, approval, self.config,
                           make_state(make_config(), month=month()), snap, now=NOW)
        self.assertTrue(result.ok, result.blockers)
        ticket = create_ticket(decision, approval, result, snap, "ACCT", now=NOW)
        return decision, approval, ticket

    def validated_record(self, decision_id):
        record = ExecutionRecord(decision_id=decision_id)
        transition(record, ExecutionState.APPROVED, audit_path=None,
                   approval_id="apr_test")
        transition(record, ExecutionState.PRE_EXECUTION_VALIDATED, audit_path=None)
        return record


# ==========================================================================


class TicketCreationTests(TicketTestCase):
    def test_a_ticket_freezes_everything_that_matters(self):
        decision, approval, ticket = self.mint()
        self.assertEqual(ticket.decision_id, decision["decision_id"])
        self.assertEqual(ticket.approval_id, approval.approval_id)
        self.assertEqual(ticket.amount_usd, Decimal("10.00"))
        self.assertEqual(ticket.asset, "ISRG")
        self.assertEqual(ticket.quote_price_usd, Decimal("366.67"))
        self.assertTrue(ticket.decision_fingerprint)
        self.assertTrue(ticket.policy_fingerprint)
        self.assertTrue(ticket.ref_id)
        self.assertTrue(ticket.ticket_fingerprint)
        self.assertIsNone(ticket.consumed_at)
        self.assertEqual(ticket.order_params["dollar_amount"], "10.00")

    def test_a_ticket_cannot_be_minted_when_preflight_fails(self):
        decision = equity_decision("10.00")
        approval = approve(decision)
        result = preflight(decision, None, self.config,
                           make_state(make_config(), month=month()), snapshot(), now=NOW)
        self.assertFalse(result.ok)
        with self.assertRaises(SubmissionError):
            create_ticket(decision, approval, result, snapshot(), "ACCT", now=NOW)

    def test_a_ticket_requires_an_established_cash_balance(self):
        decision = equity_decision("10.00")
        approval = approve(decision)
        snap = snapshot()
        result = preflight(decision, approval, self.config,
                           make_state(make_config(), month=month()), snap, now=NOW)
        snap.cash_usd = None
        with self.assertRaises(SubmissionError):
            create_ticket(decision, approval, result, snap, "ACCT", now=NOW)

    def test_a_ticket_expires_in_five_minutes(self):
        _, _, ticket = self.mint()
        self.assertEqual(TICKET_TTL_SECONDS, 300)
        self.assertEqual(ticket.expires_at, "2026-09-20T15:05:00Z")

    def test_tampering_with_a_ticket_breaks_its_fingerprint(self):
        _, _, ticket = self.mint()
        raw = ticket.to_dict()
        raw["amount_usd"] = "25.00"
        with self.assertRaises(SubmissionError):
            SubmissionTicket.from_dict(raw)

    def test_tampering_with_the_order_params_breaks_the_fingerprint(self):
        _, _, ticket = self.mint()
        raw = ticket.to_dict()
        raw["order_params"]["dollar_amount"] = "25.00"
        with self.assertRaises(SubmissionError):
            SubmissionTicket.from_dict(raw)

    def test_a_ticket_round_trips_through_the_store(self):
        _, _, ticket = self.mint()
        save_tickets({ticket.ticket_id: ticket}, self.tickets_path)
        back = load_tickets(self.tickets_path)
        self.assertEqual(back[ticket.ticket_id].ticket_fingerprint, ticket.ticket_fingerprint)

    def test_a_corrupt_ticket_store_fails_closed(self):
        with open(self.tickets_path, "w") as handle:
            handle.write("{not json")
        with self.assertRaises(SubmissionError):
            load_tickets(self.tickets_path)


class TicketVerificationTests(TicketTestCase):
    def test_a_fresh_ticket_verifies_when_all_switches_are_open(self):
        decision, approval, ticket = self.mint()
        self.assertEqual(verify_ticket(ticket, decision, approval, self.config, now=NOW), [])

    def test_the_shipped_config_blocks_every_ticket(self):
        from src.state import load_config

        decision, approval, ticket = self.mint()
        codes = [c for c, _ in verify_ticket(ticket, decision, approval, load_config(), now=NOW)]
        self.assertIn("AGENT_DISABLED", codes)
        self.assertIn("EXECUTION_MODE_DRY_RUN", codes)

    def test_a_consumed_ticket_is_refused(self):
        decision, approval, ticket = self.mint()
        ticket.consumed_at = "2026-09-20T15:00:10Z"
        codes = [c for c, _ in verify_ticket(ticket, decision, approval, self.config, now=NOW)]
        self.assertIn("TICKET_ALREADY_CONSUMED", codes)

    def test_an_expired_ticket_is_refused(self):
        decision, approval, ticket = self.mint()
        later = NOW + timedelta(seconds=TICKET_TTL_SECONDS + 1)
        codes = [c for c, _ in verify_ticket(ticket, decision, approval, self.config, now=later)]
        self.assertIn("TICKET_EXPIRED", codes)

    def test_a_changed_decision_invalidates_the_ticket(self):
        decision, approval, ticket = self.mint()
        changed = dict(decision, proposed_amount_usd="15.00")
        codes = [c for c, _ in verify_ticket(ticket, changed, approval, self.config, now=NOW)]
        self.assertIn("DECISION_MODIFIED", codes)

    def test_a_changed_approval_invalidates_the_ticket(self):
        decision, approval, ticket = self.mint()
        other = approve(equity_decision("10.00", decision_id="dec_other"))
        codes = [c for c, _ in verify_ticket(ticket, decision, other, self.config, now=NOW)]
        self.assertIn("APPROVAL_MISMATCH", codes)

    def test_a_revoked_approval_invalidates_the_ticket(self):
        decision, _, ticket = self.mint()
        codes = [c for c, _ in verify_ticket(ticket, decision, None, self.config, now=NOW)]
        self.assertIn("NOT_APPROVED", codes)

    def test_a_tampered_ref_id_is_caught(self):
        decision, approval, ticket = self.mint()
        ticket.ref_id = "00000000-0000-0000-0000-000000000000"
        ticket.ticket_fingerprint = ticket.compute_fingerprint()
        codes = [c for c, _ in verify_ticket(ticket, decision, approval, self.config, now=NOW)]
        self.assertIn("REF_ID_MISMATCH", codes)

    def test_a_sell_ticket_is_refused(self):
        decision, approval, ticket = self.mint()
        ticket.order_params["side"] = "sell"
        ticket.ticket_fingerprint = ticket.compute_fingerprint()
        codes = [c for c, _ in verify_ticket(ticket, decision, approval, self.config, now=NOW)]
        self.assertIn("NOT_A_BUY", codes)


class SubmitOnceTests(TicketTestCase):
    def test_the_shipped_submitter_refuses_before_any_call(self):
        decision, _, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        with self.assertRaises(ExecutionDisabled):
            submit_once(ticket, record, DisabledSubmitter(), {}, self.tickets_path,
                        self.audit_path, now=NOW)
        # the write-ahead still happened first
        self.assertEqual(record.state, ExecutionState.SUBMISSION_UNCERTAIN)

    def test_the_write_ahead_lands_before_the_submitter_is_called(self):
        decision, _, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        seen = {}

        class Observer(Submitter):
            name = "observer"

            def submit(self, tkt):
                seen["state_at_call_time"] = record.state
                seen["ticket_consumed"] = tkt.is_consumed
                seen["persisted"] = os.path.exists(self.tickets_path_ref)
                return {"id": "o1", "state": "confirmed", "ref_id": tkt.ref_id}

        observer = Observer()
        observer.tickets_path_ref = self.tickets_path
        submit_once(ticket, record, observer, {}, self.tickets_path, self.audit_path, now=NOW)
        self.assertEqual(seen["state_at_call_time"], ExecutionState.SUBMISSION_UNCERTAIN)
        self.assertTrue(seen["ticket_consumed"])
        self.assertTrue(seen["persisted"])

    def test_a_ticket_can_only_be_used_once(self):
        decision, _, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        mock = MockSubmitter()
        submit_once(ticket, record, mock, {}, self.tickets_path, self.audit_path, now=NOW)
        self.assertEqual(mock.calls, 1)
        with self.assertRaises(TicketConsumed):
            submit_once(ticket, record, mock, {}, self.tickets_path, self.audit_path, now=NOW)
        self.assertEqual(mock.calls, 1)

    def test_submission_is_refused_from_any_state_but_pre_execution_validated(self):
        decision, _, ticket = self.mint()
        for state in (ExecutionState.PROPOSED, ExecutionState.APPROVED,
                      ExecutionState.SUBMITTED, ExecutionState.FILLED):
            with self.subTest(state=state):
                record = ExecutionRecord(decision_id=decision["decision_id"], state=state)
                fresh = self.mint()[2]
                with self.assertRaises(SubmissionError):
                    submit_once(fresh, record, MockSubmitter(), {}, self.tickets_path,
                                self.audit_path, now=NOW)

    def test_a_broker_failure_leaves_the_record_uncertain_and_does_not_retry(self):
        decision, _, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        mock = MockSubmitter(raises=RuntimeError("connection reset"))
        with self.assertRaises(RuntimeError):
            submit_once(ticket, record, mock, {}, self.tickets_path, self.audit_path, now=NOW)
        self.assertEqual(record.state, ExecutionState.SUBMISSION_UNCERTAIN)
        self.assertEqual(mock.calls, 1)
        events = [e["event"] for e in read_audit(self.audit_path)]
        self.assertIn("SUBMIT_INTENT", events)
        self.assertIn("FAILURE", events)

    def test_the_submit_intent_is_audited_before_the_call(self):
        decision, _, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        submit_once(ticket, record, MockSubmitter(), {}, self.tickets_path,
                    self.audit_path, now=NOW)
        events = read_audit(self.audit_path)
        intent = next(e for e in events if e["event"] == "SUBMIT_INTENT")
        self.assertEqual(intent["detail"]["ref_id"], ticket.ref_id)
        self.assertIn("BEFORE any broker call", intent["detail"]["note"])


class ManualHandoffTests(TicketTestCase):
    def test_the_handoff_writes_the_params_and_stops(self):
        decision, _, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        handoff_file = os.path.join(self.tmp, "handoff.json")
        with self.assertRaises(SubmissionHandoffRequired):
            submit_once(ticket, record, ManualHandoffSubmitter(handoff_file), {},
                        self.tickets_path, self.audit_path, now=NOW)
        self.assertEqual(record.state, ExecutionState.SUBMISSION_UNCERTAIN)
        with open(handoff_file, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["ref_id"], ticket.ref_id)
        self.assertEqual(payload["order_params"]["dollar_amount"], "10.00")
        self.assertIn("EXACTLY ONE MCP call", payload["instruction"])


class BrokerResponseTests(TicketTestCase):
    def setUp(self):
        super().setUp()
        self.decision, self.approval, self.ticket = self.mint()

    def test_a_normal_response_parses(self):
        response = normalize_broker_response(
            {"id": "brk1", "state": "confirmed", "ref_id": self.ticket.ref_id}, self.ticket
        )
        self.assertEqual(response.order_id, "brk1")
        self.assertEqual(response.state, "confirmed")

    def test_an_mcp_envelope_parses(self):
        response = normalize_broker_response(
            {"data": {"order": {"id": "brk2", "state": "filled"}}}, self.ticket
        )
        self.assertEqual(response.order_id, "brk2")

    def test_a_missing_response_fails_closed(self):
        with self.assertRaises(SubmissionError):
            normalize_broker_response(None, self.ticket)

    def test_a_response_without_an_order_id_fails_closed(self):
        with self.assertRaises(SubmissionError):
            normalize_broker_response({"state": "confirmed"}, self.ticket)

    def test_an_unknown_state_fails_closed(self):
        with self.assertRaises(SubmissionError):
            normalize_broker_response({"id": "b", "state": "teleported"}, self.ticket)

    def test_a_mismatched_ref_id_fails_closed(self):
        with self.assertRaises(SubmissionError):
            normalize_broker_response(
                {"id": "b", "state": "filled", "ref_id": "someone-elses-key"}, self.ticket
            )

    def test_ingest_requires_the_uncertain_state(self):
        record = ExecutionRecord(decision_id=self.decision["decision_id"],
                                 state=ExecutionState.APPROVED)
        response = normalize_broker_response({"id": "b", "state": "confirmed"}, self.ticket)
        with self.assertRaises(SubmissionError):
            ingest_response(record, response, audit_path=None)


class FillReconciliationTests(TicketTestCase):
    def setUp(self):
        super().setUp()
        self.decision, self.approval, self.ticket = self.mint()
        self.record = ExecutionRecord(decision_id=self.decision["decision_id"],
                                      state=ExecutionState.SUBMITTED,
                                      broker_order_id="brk_order_1")

    def rec(self, orders):
        return reconcile_submission(self.ticket, self.record,
                                    {"data": {"orders": orders}}, "EQUITY")

    def test_a_full_fill_is_recognised(self):
        result = self.rec([broker_order(self.ticket.ref_id, "10.00", "filled",
                                        executed=("0.027", "366.67"))])
        self.assertTrue(result.found)
        self.assertEqual(result.next_state, ExecutionState.FILLED)
        self.assertEqual(result.filled_usd, Decimal("9.90"))

    def test_a_partial_fill_is_recognised(self):
        result = self.rec([broker_order(self.ticket.ref_id, "10.00", "partially_filled",
                                        executed=("0.010", "366.67"))])
        self.assertEqual(result.next_state, ExecutionState.PARTIALLY_FILLED)
        self.assertEqual(result.filled_usd, Decimal("3.67"))

    def test_an_open_order_produces_no_state_change(self):
        result = self.rec([broker_order(self.ticket.ref_id, "10.00", "confirmed")])
        self.assertTrue(result.found)
        self.assertIsNone(result.next_state)
        self.assertTrue(any("still open" in n for n in result.notes))

    def test_a_rejection_is_recognised(self):
        result = self.rec([broker_order(self.ticket.ref_id, "10.00", "rejected")])
        self.assertEqual(result.next_state, ExecutionState.REJECTED)

    def test_a_cancel_after_partial_fill_still_counts_the_fill(self):
        result = self.rec([broker_order(self.ticket.ref_id, "10.00", "cancelled",
                                        executed=("0.010", "366.67"))])
        self.assertEqual(result.next_state, ExecutionState.CANCELLED)
        self.assertEqual(result.filled_usd, Decimal("3.67"))
        self.assertTrue(any("still spent" in n for n in result.notes))

    def test_a_not_found_order_is_never_read_as_nothing_happened(self):
        result = self.rec([])
        self.assertFalse(result.found)
        self.assertIsNone(result.next_state)
        self.assertTrue(any("does NOT mean the order was not placed" in n
                            for n in result.notes))
        self.assertTrue(any("never resubmit" in n for n in result.notes))

    def test_matching_falls_back_to_the_broker_order_id(self):
        order = broker_order("some-other-ref", "10.00", "filled", executed=("0.027", "366.67"))
        order["id"] = "brk_order_1"
        result = self.rec([order])
        self.assertTrue(result.found)
        self.assertEqual(result.next_state, ExecutionState.FILLED)

    def test_two_matching_orders_halt_for_investigation(self):
        result = self.rec([
            broker_order(self.ticket.ref_id, "10.00", "filled", order_id="a",
                         executed=("0.027", "366.67")),
            broker_order(self.ticket.ref_id, "10.00", "filled", order_id="b",
                         executed=("0.027", "366.67")),
        ])
        self.assertIsNone(result.next_state)
        self.assertTrue(any("should be impossible" in n for n in result.notes))

    def test_a_malformed_orders_payload_fails_closed(self):
        with self.assertRaises(SubmissionError):
            reconcile_submission(self.ticket, self.record, {"nonsense": 1}, "EQUITY")


class FullLifecycleTests(TicketTestCase):
    """PROPOSED -> APPROVED -> PREFLIGHT -> TICKET -> WRITE-AHEAD -> MOCK
    SUBMISSION -> RECONCILIATION -> FILLED, for both asset classes."""

    def lifecycle(self, decision, snap, filled_amount, asset_class):
        approval = approve(decision)
        record = ExecutionRecord(decision_id=decision["decision_id"])
        self.assertEqual(record.state, ExecutionState.PROPOSED)

        transition(record, ExecutionState.APPROVED, audit_path=self.audit_path,
                   approval_id=approval.approval_id)

        result = preflight(decision, approval, self.config,
                           make_state(make_config(), month=month()), snap, now=NOW)
        self.assertTrue(result.ok, result.blockers)
        transition(record, ExecutionState.PRE_EXECUTION_VALIDATED, audit_path=self.audit_path)

        ticket = create_ticket(decision, approval, result, snap, "ACCT", now=NOW)
        self.assertEqual(verify_ticket(ticket, decision, approval, self.config, now=NOW), [])

        mock = MockSubmitter({"id": "brk_lc", "state": "filled", "ref_id": ticket.ref_id})
        raw = submit_once(ticket, record, mock, {}, self.tickets_path, self.audit_path, now=NOW)
        self.assertEqual(record.state, ExecutionState.SUBMISSION_UNCERTAIN)

        response = normalize_broker_response(raw, ticket)
        ingest_response(record, response, audit_path=self.audit_path)
        self.assertEqual(record.state, ExecutionState.SUBMITTED)

        orders = {"data": {"orders": [broker_order(
            ticket.ref_id, filled_amount, "filled", order_id="brk_lc",
            symbol=ticket.asset, executed=(str(Decimal(filled_amount) / ticket.quote_price_usd),
                                           str(ticket.quote_price_usd)))]}}
        fills = reconcile_submission(ticket, record, orders, asset_class)
        self.assertEqual(fills.next_state, ExecutionState.FILLED)
        transition(record, ExecutionState.FILLED, audit_path=self.audit_path)

        self.assertEqual(record.state, ExecutionState.FILLED)
        self.assertEqual(mock.calls, 1)
        states = [h["to"] for h in record.history]
        self.assertEqual(states, ["APPROVED", "PRE_EXECUTION_VALIDATED",
                                  "SUBMISSION_UNCERTAIN", "SUBMITTED", "FILLED"])
        return record, ticket

    def test_equity_lifecycle_ends_filled(self):
        self.lifecycle(equity_decision("10.00"), snapshot(), "10.00", "EQUITY")

    def test_crypto_lifecycle_ends_filled(self):
        decision = crypto_dec("10.00")
        snap = crypto_snapshot(cash_usd=Decimal("100.00"),
                               unsettled_funds_usd=Decimal("0.00"),
                               account_type="limited_margin")
        record, ticket = self.lifecycle(decision, snap, "10.00", "CRYPTO")
        self.assertEqual(ticket.order_params["type"], "limit")

    def test_the_audit_log_reconstructs_the_whole_story(self):
        self.lifecycle(equity_decision("10.00"), snapshot(), "10.00", "EQUITY")
        events = [e["event"] for e in read_audit(self.audit_path)]
        self.assertIn("SUBMIT_INTENT", events)
        self.assertIn("SUBMISSION_RESULT", events)
        self.assertEqual(events.count("SUBMIT_INTENT"), 1)


class CrashRecoveryTests(TicketTestCase):
    """A crash immediately after the broker call must reconcile, never resubmit."""

    def test_a_crash_after_submission_leaves_an_uncertain_durable_record(self):
        decision, approval, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        crasher = CrashingSubmitter()

        with self.assertRaises(KeyboardInterrupt):
            submit_once(ticket, record, crasher, {}, self.tickets_path,
                        self.audit_path, now=NOW)

        # the order DID reach the broker, and the process died
        self.assertTrue(crasher.order_reached_broker)
        self.assertEqual(record.state, ExecutionState.SUBMISSION_UNCERTAIN)

        # durable: the ticket is consumed on disk, and the intent was fsynced
        persisted = load_tickets(self.tickets_path)
        self.assertTrue(persisted[ticket.ticket_id].is_consumed)
        self.assertIn("SUBMIT_INTENT", [e["event"] for e in read_audit(self.audit_path)])

    def test_after_restart_the_executor_refuses_to_act_and_demands_reconciliation(self):
        decision, approval, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        crasher = CrashingSubmitter()
        with self.assertRaises(KeyboardInterrupt):
            submit_once(ticket, record, crasher, {}, self.tickets_path,
                        self.audit_path, now=NOW)

        # --- restart: reload from disk and try again ---
        reloaded_tickets = load_tickets(self.tickets_path)
        reloaded_ticket = reloaded_tickets[ticket.ticket_id]
        restarted = ExecutionRecord(decision_id=decision["decision_id"],
                                    state=ExecutionState.SUBMISSION_UNCERTAIN)

        result = run(decision, approval, config=self.config,
                     exec_state=ExecutionState.SUBMISSION_UNCERTAIN)
        self.assertFalse(result.ok)
        self.assertIn("RECONCILIATION_REQUIRED", result.codes)
        self.assertNotIn("ALREADY_SUBMITTED", result.codes)

        codes = [c for c, _ in verify_ticket(reloaded_ticket, decision, approval,
                                             self.config, now=NOW)]
        self.assertIn("TICKET_ALREADY_CONSUMED", codes)

        with self.assertRaises(TicketConsumed):
            submit_once(reloaded_ticket, restarted, MockSubmitter(), reloaded_tickets,
                        self.tickets_path, self.audit_path, now=NOW)

    def test_reconciliation_after_a_crash_finds_the_order_and_settles_it(self):
        decision, approval, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        with self.assertRaises(KeyboardInterrupt):
            submit_once(ticket, record, CrashingSubmitter(), {}, self.tickets_path,
                        self.audit_path, now=NOW)

        restarted = ExecutionRecord(decision_id=decision["decision_id"],
                                    state=ExecutionState.SUBMISSION_UNCERTAIN)
        orders = {"data": {"orders": [broker_order(ticket.ref_id, "10.00", "filled",
                                                   executed=("0.027", "366.67"))]}}
        fills = reconcile_submission(ticket, restarted, orders, "EQUITY")
        self.assertTrue(fills.found)
        self.assertEqual(fills.next_state, ExecutionState.FILLED)

        transition(restarted, ExecutionState.SUBMITTED, audit_path=None,
                   broker_order_id=fills.broker_order_id)
        transition(restarted, ExecutionState.FILLED, audit_path=None)
        self.assertEqual(restarted.state, ExecutionState.FILLED)

    def test_a_crash_where_the_broker_never_got_it_still_never_auto_resubmits(self):
        decision, approval, ticket = self.mint()
        record = self.validated_record(decision["decision_id"])
        with self.assertRaises(KeyboardInterrupt):
            submit_once(ticket, record, CrashingSubmitter(), {}, self.tickets_path,
                        self.audit_path, now=NOW)

        restarted = ExecutionRecord(decision_id=decision["decision_id"],
                                    state=ExecutionState.SUBMISSION_UNCERTAIN)
        fills = reconcile_submission(ticket, restarted, {"data": {"orders": []}}, "EQUITY")
        self.assertFalse(fills.found)
        self.assertIsNone(fills.next_state)
        self.assertEqual(restarted.state, ExecutionState.SUBMISSION_UNCERTAIN)
        # a NEW ticket would be required, and only after a human decides
        with self.assertRaises(TicketConsumed):
            submit_once(ticket, restarted, MockSubmitter(), {}, self.tickets_path,
                        self.audit_path, now=NOW)

    def test_the_ref_id_would_deduplicate_even_if_a_resubmission_happened(self):
        decision, _, ticket_one = self.mint()
        ticket_two = self.mint(decision=decision)[2]
        self.assertNotEqual(ticket_one.ticket_id, ticket_two.ticket_id)
        self.assertEqual(ticket_one.ref_id, ticket_two.ref_id)


class ShippedStateTests(unittest.TestCase):
    def test_the_default_submitter_never_submits(self):
        with self.assertRaises(ExecutionDisabled):
            DisabledSubmitter().submit(
                SubmissionTicket(
                    ticket_id="t", decision_id="d", approval_id="a",
                    decision_fingerprint="f", policy_fingerprint="p", asset="ISRG",
                    asset_class="EQUITY", asset_type="us_common_stock",
                    amount_usd=Decimal("1.00"), month="2026-09", order_params={},
                    ref_id="r", quote_price_usd=Decimal("1"), quote_timestamp="",
                    settled_cash_usd=Decimal("1"), created_at="", expires_at="",
                )
            )

    def test_the_submission_module_cannot_reach_a_broker(self):
        import ast

        import src.submission as module

        with open(module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        banned = {"requests", "httpx", "urllib", "socket", "http", "subprocess", "aiohttp"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], banned)


if __name__ == "__main__":
    unittest.main()
