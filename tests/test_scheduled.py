"""Stage 7 — scheduled report-only safety.

The property under test: a scheduled, unattended run has strictly *less*
authority than an interactive one. It may recommend anything and change nothing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.scheduling import (  # noqa: E402
    DISCOVERY_REPORT_SECTIONS,
    validate_discovery_report,
    BROKER_SNAPSHOT_PATH,
    SCHEDULED_POST_RUN_MARKER,
    SCHEDULED_RUN_MARKER,
    SNAPSHOT_REFRESH_MARKER,
    WRITABLE_DURING_SNAPSHOT_REFRESH,
    is_writable_during_snapshot_refresh,
    WRITABLE_DURING_SCHEDULED_RUN,
    archive_inventory,
    archive_losses,
    digest_tree,
    is_writable_during_scheduled_run,
    repo_tree,
    run_digest,
    split_digest,
    unauthorized_writes,
    FORBIDDEN_SCRIPTS,
    FORBIDDEN_STATE_WRITES,
    IMMUTABLE_DURING_SCHEDULED_RUN,
    MUTATING_TOOLS,
    ORDER_TOOLS,
    REQUIRED_REPORT_SECTIONS,
    check_approval_script_denied,
    check_denied_tools,
    check_no_pending_submission,
    check_switches,
    extract_decision,
    file_digest,
    missing_report_sections,
    parse_usage,
    postflight_safety,
    preflight_safety,
    report_filename,
    report_path,
    surface_changes,
    surface_digest,
)
from src.state import load_config  # noqa: E402
from tests.helpers import make_config  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def real_settings():
    with open(os.path.join(REPO_ROOT, ".claude", "settings.json")) as handle:
        return json.load(handle)


def denying(*tools):
    """A settings object that denies exactly the named tools."""
    return {"permissions": {"deny": ["mcp__robinhood-trading__" + t for t in tools]}}


class ShippedSchedulerSafetyTests(unittest.TestCase):
    """The repository as shipped must satisfy every report-only invariant."""

    def test_the_shipped_config_passes_preflight(self):
        check = preflight_safety(load_config())
        self.assertTrue(check.ok, check.violations)

    def test_every_order_tool_is_denied_in_the_real_settings(self):
        violations, _ = check_denied_tools(real_settings())
        self.assertEqual(violations, [])

    def test_the_approval_script_is_denied_in_the_real_settings(self):
        violations, _ = check_approval_script_denied(real_settings())
        self.assertEqual(violations, [])

    def test_no_unresolved_submission_in_the_repository(self):
        violations, _ = check_no_pending_submission(REPO_ROOT)
        self.assertEqual(violations, [])

    def test_the_immutable_surface_files_all_exist(self):
        digest = surface_digest(REPO_ROOT)
        for rel in IMMUTABLE_DURING_SCHEDULED_RUN:
            with self.subTest(rel=rel):
                self.assertIsNotNone(digest[rel], "%s is missing" % rel)

    def test_the_runner_never_invokes_a_forbidden_script(self):
        runner = os.path.join(REPO_ROOT, "scripts", "scheduled_evaluation.sh")
        with open(runner) as handle:
            body = handle.read()
        for script in FORBIDDEN_SCRIPTS:
            with self.subTest(script=script):
                # The runner may name a script only to forbid it, never to run it.
                self.assertNotIn("python3 scripts/" + script, body)
                self.assertNotIn("./scripts/" + script, body)

    def test_the_runner_disallows_every_order_tool(self):
        runner = os.path.join(REPO_ROOT, "scripts", "scheduled_evaluation.sh")
        with open(runner) as handle:
            body = handle.read()
        start = body.index("DISALLOWED_TOOLS=")
        disallowed = body[start:body.index("\n\n", start)]
        for tool in ORDER_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, disallowed)

    def test_the_runner_does_not_allow_any_order_tool(self):
        runner = os.path.join(REPO_ROOT, "scripts", "scheduled_evaluation.sh")
        with open(runner) as handle:
            body = handle.read()
        start = body.index("ALLOWED_TOOLS=")
        allowed = body[start:body.index("\n\n", start)]
        for tool in ORDER_TOOLS + MUTATING_TOOLS:
            with self.subTest(tool=tool):
                self.assertNotIn(tool, allowed)

    def test_the_prompt_forbids_approval_and_execution(self):
        path = os.path.join(REPO_ROOT, "prompts", "scheduled_evaluation.md")
        with open(path) as handle:
            body = handle.read().lower()
        for phrase in ("report only", "never approve", "never enable execution",
                       "never submit", "never call an order tool"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)

    def test_the_plists_do_not_run_at_load(self):
        import plistlib
        for name in ("com.robinhood-agent.evaluation.plist",
                     "com.robinhood-agent.evaluation-pm.plist"):
            with self.subTest(name=name):
                path = os.path.join(REPO_ROOT, "scheduler", name)
                with open(path, "rb") as handle:
                    data = plistlib.loads(handle.read())
                self.assertFalse(data["RunAtLoad"])

    def test_the_plists_are_weekdays_only(self):
        import plistlib
        for name, hour, minute in (
            ("com.robinhood-agent.evaluation.plist", 10, 30),
            ("com.robinhood-agent.evaluation-pm.plist", 14, 0),
        ):
            with self.subTest(name=name):
                path = os.path.join(REPO_ROOT, "scheduler", name)
                with open(path, "rb") as handle:
                    data = plistlib.loads(handle.read())
                intervals = data["StartCalendarInterval"]
                self.assertEqual(len(intervals), 5)
                self.assertEqual(sorted(i["Weekday"] for i in intervals), [1, 2, 3, 4, 5])
                self.assertEqual({i["Hour"] for i in intervals}, {hour})
                self.assertEqual({i["Minute"] for i in intervals}, {minute})

    def test_the_plists_pin_the_chicago_timezone(self):
        import plistlib
        for name in ("com.robinhood-agent.evaluation.plist",
                     "com.robinhood-agent.evaluation-pm.plist"):
            with self.subTest(name=name):
                path = os.path.join(REPO_ROOT, "scheduler", name)
                with open(path, "rb") as handle:
                    data = plistlib.loads(handle.read())
                self.assertEqual(data["EnvironmentVariables"]["TZ"], "America/Chicago")


class SwitchTests(unittest.TestCase):
    def test_open_switches_block_a_scheduled_run(self):
        for field in ("agent_enabled", "live_trading"):
            with self.subTest(field=field):
                violations, _ = check_switches(make_config(**{field: True}))
                self.assertTrue(violations)

    def test_non_dry_run_mode_blocks_a_scheduled_run(self):
        for mode in ("APPROVAL_REQUIRED", "AUTONOMOUS", "nonsense"):
            with self.subTest(mode=mode):
                violations, _ = check_switches(make_config(execution_mode=mode))
                self.assertTrue(violations)
                self.assertIn("DRY_RUN", violations[0])

    def test_all_closed_switches_pass(self):
        violations, checks = check_switches(make_config())
        self.assertEqual(violations, [])
        self.assertEqual(len(checks), 3)

    def test_approval_required_mode_is_refused_even_though_execution_allows_it(self):
        # APPROVAL_REQUIRED is a legitimate interactive mode. It is still not a
        # legitimate *scheduled* mode: nobody is present to approve.
        check = preflight_safety(
            make_config(execution_mode="APPROVAL_REQUIRED"), settings=real_settings()
        )
        self.assertFalse(check.ok)


class DeniedToolTests(unittest.TestCase):
    def test_a_missing_order_tool_denial_is_caught(self):
        settings = denying(*[t for t in ORDER_TOOLS if t != "place_equity_order"],
                           *MUTATING_TOOLS)
        violations, _ = check_denied_tools(settings)
        self.assertTrue(any("place_equity_order" in v for v in violations))

    def test_a_missing_mutating_tool_denial_is_caught(self):
        settings = denying(*ORDER_TOOLS,
                           *[t for t in MUTATING_TOOLS if t != "create_alert"])
        violations, _ = check_denied_tools(settings)
        self.assertTrue(any("create_alert" in v for v in violations))

    def test_unreadable_settings_fail_closed(self):
        violations, _ = check_denied_tools("not a dict")
        self.assertTrue(violations)

    def test_malformed_deny_list_fails_closed(self):
        violations, _ = check_denied_tools({"permissions": {"deny": "everything"}})
        self.assertTrue(violations)

    def test_empty_settings_fail_closed(self):
        violations, _ = check_denied_tools({})
        self.assertTrue(violations)

    def test_approval_script_must_be_denied(self):
        violations, _ = check_approval_script_denied(denying(*ORDER_TOOLS))
        self.assertTrue(violations)


class PostflightTests(unittest.TestCase):
    """A run that mutated the safety surface fails, whatever its report said."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "state"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_an_unchanged_surface_passes(self):
        digest = surface_digest(REPO_ROOT)
        check = postflight_safety(digest, dict(digest), repo_root=self.tmp)
        self.assertTrue(check.ok, check.violations)

    def test_a_changed_config_is_caught(self):
        before = surface_digest(REPO_ROOT)
        after = dict(before)
        after["config.json"] = "deadbeef"
        check = postflight_safety(before, after, repo_root=self.tmp)
        self.assertFalse(check.ok)
        self.assertIn("config.json", check.violations[0])

    def test_changed_guardrails_are_caught(self):
        before = surface_digest(REPO_ROOT)
        after = dict(before)
        after["src/guardrails.py"] = "deadbeef"
        check = postflight_safety(before, after, repo_root=self.tmp)
        self.assertFalse(check.ok)

    def test_a_deleted_settings_file_is_caught(self):
        before = surface_digest(REPO_ROOT)
        after = dict(before)
        after[".claude/settings.json"] = None
        check = postflight_safety(before, after, repo_root=self.tmp)
        self.assertFalse(check.ok)

    def test_a_created_approval_is_caught(self):
        digest = surface_digest(REPO_ROOT)
        with open(os.path.join(self.tmp, "state", "approvals.json"), "w") as handle:
            json.dump({"dec_1": {"max_amount_usd": "25.00"}}, handle)
        check = postflight_safety(digest, dict(digest), repo_root=self.tmp)
        self.assertFalse(check.ok)
        self.assertTrue(any("approval" in v for v in check.violations))

    def test_an_empty_approvals_file_is_tolerated(self):
        digest = surface_digest(REPO_ROOT)
        with open(os.path.join(self.tmp, "state", "approvals.json"), "w") as handle:
            json.dump({}, handle)
        check = postflight_safety(digest, dict(digest), repo_root=self.tmp)
        self.assertTrue(check.ok, check.violations)

    def test_a_created_submission_handoff_is_caught(self):
        digest = surface_digest(REPO_ROOT)
        with open(os.path.join(self.tmp, "state", "pending_submission.json"), "w") as f:
            json.dump({"ref_id": "x"}, f)
        check = postflight_safety(digest, dict(digest), repo_root=self.tmp)
        self.assertFalse(check.ok)
        self.assertTrue(any("pending_submission" in v for v in check.violations))

    def test_surface_changes_lists_every_moved_file(self):
        before = {"a": "1", "b": "2", "c": "3"}
        after = {"a": "1", "b": "changed", "c": None}
        self.assertEqual(surface_changes(before, after), ["b", "c"])

    def test_every_forbidden_state_write_is_watched(self):
        # approvals and pending_submission are checked directly; config and the
        # guardrails are covered by the digest.
        for rel in FORBIDDEN_STATE_WRITES:
            with self.subTest(rel=rel):
                self.assertTrue(
                    rel in ("state/approvals.json", "state/pending_submission.json")
                    or rel in ("state/executions.json", "state/budget.json")
                )


class PreflightBlocksOnUncertainSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "state"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_an_unresolved_submission_stops_the_scheduler(self):
        with open(os.path.join(self.tmp, "state", "pending_submission.json"), "w") as f:
            json.dump({"ref_id": "x"}, f)
        check = preflight_safety(make_config(), settings=real_settings(),
                                 repo_root=self.tmp)
        self.assertFalse(check.ok)
        self.assertTrue(any("pending_submission" in v for v in check.violations))


class ReportShapeTests(unittest.TestCase):
    def test_report_filename_format(self):
        stamp = datetime(2026, 9, 8, 10, 30)
        self.assertEqual(report_filename(stamp), "2026-09-08_1030.md")
        self.assertTrue(report_path(stamp).endswith("reports/2026-09-08_1030.md"))

    def test_afternoon_slot_gets_a_distinct_filename(self):
        self.assertNotEqual(
            report_filename(datetime(2026, 9, 8, 10, 30)),
            report_filename(datetime(2026, 9, 8, 14, 0)),
        )

    def test_decision_is_extracted(self):
        for value in ("WAIT", "SINGLE_BUY", "SPLIT_BUY_PLAN"):
            with self.subTest(value=value):
                self.assertEqual(
                    extract_decision("## Decision\n\nDECISION: %s\n\ntext" % value), value
                )

    def test_a_missing_decision_line_returns_none(self):
        self.assertIsNone(extract_decision("## Decision\n\nProbably wait.\n"))

    def test_an_invalid_decision_value_is_not_accepted(self):
        self.assertIsNone(extract_decision("DECISION: BUY_EVERYTHING\n"))

    def test_missing_sections_are_reported(self):
        missing = missing_report_sections("# Report\n\n## Decision\nDECISION: WAIT\n")
        self.assertIn("Portfolio & budget status", missing)
        self.assertNotIn("Decision", missing)

    def test_a_complete_report_reports_no_missing_sections(self):
        body = "\n".join("## " + s for s in REQUIRED_REPORT_SECTIONS)
        self.assertEqual(missing_report_sections(body), [])


class UsageTests(unittest.TestCase):
    def test_usage_is_parsed_from_a_result_object(self):
        record = parse_usage(
            {"total_cost_usd": 0.0432, "num_turns": 11, "duration_ms": 61000,
             "session_id": "abc", "usage": {"input_tokens": 51234, "output_tokens": 2311}},
            started_at="2026-09-08T15:30:00Z", finished_at="2026-09-08T15:31:01Z",
        )
        self.assertTrue(record.available)
        self.assertAlmostEqual(record.total_cost_usd, 0.0432)
        self.assertEqual(record.input_tokens, 51234)
        self.assertEqual(record.output_tokens, 2311)
        self.assertEqual(record.num_turns, 11)
        self.assertAlmostEqual(record.duration_seconds, 61.0)

    def test_usage_is_parsed_from_a_json_string(self):
        record = parse_usage('{"total_cost_usd": 0.01, "num_turns": 2}')
        self.assertTrue(record.available)
        self.assertAlmostEqual(record.total_cost_usd, 0.01)

    def test_usage_is_parsed_from_a_stream_of_events(self):
        record = parse_usage([
            {"type": "assistant"},
            {"type": "result", "total_cost_usd": 0.02, "num_turns": 4},
        ])
        self.assertTrue(record.available)
        self.assertAlmostEqual(record.total_cost_usd, 0.02)

    def test_unavailable_usage_is_recorded_as_unavailable_not_guessed(self):
        for payload in (None, "", "not json", {}, []):
            with self.subTest(payload=payload):
                record = parse_usage(payload)
                self.assertFalse(record.available)
                self.assertIsNone(record.total_cost_usd)

    def test_extra_fields_are_carried_through(self):
        record = parse_usage({}, started_at="a", finished_at="b",
                             decision="WAIT", exit_code=0, report="reports/x.md")
        self.assertEqual(record.decision, "WAIT")
        self.assertEqual(record.exit_code, 0)
        self.assertEqual(record.report, "reports/x.md")

    def test_a_cost_is_never_invented_when_the_cli_is_silent(self):
        record = parse_usage({"num_turns": 3})
        self.assertIsNone(record.total_cost_usd)
        self.assertTrue(record.available)  # turns were reported, cost was not


if __name__ == "__main__":
    unittest.main()


class PolicyDocumentTests(unittest.TestCase):
    """Stage 7 policy text must stay in step with what the code enforces."""

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_all_three_outcomes_are_documented_everywhere(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md",
                    "prompts/scheduled_evaluation.md", "prompts/evaluate_market.md"):
            body = self.read(rel)
            for outcome in ("WAIT", "SINGLE_BUY", "SPLIT_BUY_PLAN"):
                with self.subTest(rel=rel, outcome=outcome):
                    self.assertIn(outcome, body)

    def test_the_documented_leg_range_matches_the_code(self):
        from src.models import MAX_SPLIT_LEGS, MIN_SPLIT_LEGS

        self.assertEqual((MIN_SPLIT_LEGS, MAX_SPLIT_LEGS), (2, 5))
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md",
                    "prompts/scheduled_evaluation.md"):
            with self.subTest(rel=rel):
                self.assertIn("2–5", self.read(rel))  # "2–5", en dash

    def test_splitting_is_documented_as_never_forced(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md",
                    "prompts/scheduled_evaluation.md"):
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                self.assertIn("never", body)
                self.assertTrue(
                    "never required" in body or "never be forced" in body,
                    "%s must say splitting is never required/forced" % rel,
                )

    def test_every_capital_use_bucket_is_documented(self):
        from src.models import WAIT_CANDIDATE_BUCKETS

        claude = self.read("CLAUDE.md")
        policy = self.read("INVESTMENT_POLICY.md")
        for name, _aliases in WAIT_CANDIDATE_BUCKETS:
            with self.subTest(bucket=name):
                self.assertIn(name, claude)
                self.assertIn(name, policy)

    def test_the_five_way_comparison_is_in_the_scheduled_policy(self):
        import re as _re

        body = self.read("prompts/scheduled_evaluation.md").lower()
        body = _re.sub(r"\s+", " ", body.replace("*", "").replace("`", ""))
        for phrase in ("adding to an existing equity or etf position",
                       "opening a new equity or etf position",
                       "adding to an existing crypto position",
                       "opening a new eligible crypto position",
                       "preserving some or all of the budget as cash"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)

    def test_crypto_parity_rules_are_documented(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md",
                    "prompts/scheduled_evaluation.md"):
            body = self.read(rel).lower()
            with self.subTest(rel=rel):
                # not an afterthought, researched with the crypto framework
                self.assertIn("afterthought", body)
                self.assertIn("discovery_policy.md", body)
                # the two temptations, named
                self.assertIn("unrealized loss", body)
                self.assertIn("average down", body)
                self.assertIn("appreciation", body)
                # volatility and concentration
                self.assertIn("volatilit", body)
                self.assertIn("concentration", body)

    def test_the_four_way_ceiling_is_documented(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md",
                    "prompts/scheduled_evaluation.md"):
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                for word in ("executed", "pending", "approved", "proposed"):
                    self.assertIn(word, body)

    def test_per_leg_independence_is_documented(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md"):
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                for word in ("decision_id", "fingerprint", "approval",
                             "submission ticket", "reconciliation", "replay"):
                    self.assertIn(word, body)

    def test_scheduled_policy_forbids_approval_and_execution_of_both_classes(self):
        body = self.read("prompts/scheduled_evaluation.md").lower()
        self.assertIn("report only", body)
        self.assertIn("never approve", body)
        self.assertIn("execution is disabled", body)

    def test_the_retired_bare_buy_outcome_is_gone_from_the_output_formats(self):
        for rel in ("CLAUDE.md", "prompts/evaluate_market.md"):
            with self.subTest(rel=rel):
                self.assertNotIn("DECISION: BUY\n", self.read(rel))

    def test_documents_are_versioned_to_stage_7(self):
        self.assertIn("Version 7", self.read("CLAUDE.md"))
        self.assertIn("Version 7", self.read("INVESTMENT_POLICY.md"))


class Stage7CorrectionTests(unittest.TestCase):
    """The narrow corrections applied after the first Stage 7 policy pass."""

    POLICY_DOCS = ("CLAUDE.md", "INVESTMENT_POLICY.md",
                   "prompts/scheduled_evaluation.md", "prompts/evaluate_market.md")

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def prose(self, rel):
        """Lowercased text with markdown emphasis and line wrapping removed.

        Policy prose is wrapped at 80 columns and studded with ``**bold**``, so
        a naive substring check is brittle in a way that has nothing to do with
        what the document actually says.
        """
        import re as _re

        body = self.read(rel).lower()
        body = body.replace("*", "").replace("`", "")
        return _re.sub(r"\s+", " ", body)

    # --- 1. existing position spans ALL accounts ------------------------
    def test_existing_position_is_defined_across_all_accounts(self):
        for rel in self.POLICY_DOCS:
            with self.subTest(rel=rel):
                body = self.prose(rel)
                self.assertIn("all accounts", body)
                self.assertIn("execution destination", body)

    def test_an_empty_agentic_account_is_not_grounds_for_not_applicable(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md",
                    "prompts/scheduled_evaluation.md"):
            with self.subTest(rel=rel):
                self.assertIn("not not_applicable merely because", self.prose(rel))

    def test_no_document_says_a_bucket_is_inapplicable_because_agentic_is_empty(self):
        """The misleading example from the first pass must be gone."""
        for rel in self.POLICY_DOCS:
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                self.assertNotIn("when the agentic account holds no crypto,", body)
                self.assertNotIn("no crypto held yet", body)

    # --- 2. the existing-equity bucket includes ETFs --------------------
    def test_existing_equity_bucket_is_documented_as_covering_etfs(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md"):
            with self.subTest(rel=rel):
                body = self.prose(rel)
                self.assertIn("existing equity or etf position", body)
                self.assertIn("eligible etfs", body)

    def test_the_internal_field_name_was_not_renamed(self):
        """Compatibility: the aliases still map onto the same four buckets."""
        from src.models import WAIT_CANDIDATE_BUCKETS

        names = [name for name, _ in WAIT_CANDIDATE_BUCKETS]
        self.assertIn("best_existing_equity_candidate", names)
        aliases = dict(WAIT_CANDIDATE_BUCKETS)
        self.assertIn("best_existing_position_candidate",
                      aliases["best_existing_equity_candidate"])

    # --- 3. calibrated drawdown language --------------------------------
    def test_drawdowns_are_not_described_as_normal(self):
        for rel in self.POLICY_DOCS + ("DISCOVERY_POLICY.md",):
            with self.subTest(rel=rel):
                body = self.prose(rel)
                self.assertNotIn("70–90% are normal", body)
                self.assertNotIn("70–90% drawdowns are normal", body)
                self.assertNotIn("drawdowns of 70–90% are normal", body)

    def test_drawdowns_are_described_as_a_plausible_risk(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md", "DISCOVERY_POLICY.md",
                    "prompts/scheduled_evaluation.md"):
            with self.subTest(rel=rel):
                body = self.prose(rel)
                self.assertIn("have historically occurred", body)
                self.assertIn("plausible risk", body)

    # --- 4. structural hygiene ------------------------------------------
    def test_no_duplicate_headings_within_a_document(self):
        for rel in self.POLICY_DOCS:
            with self.subTest(rel=rel):
                seen, dupes = set(), []
                in_fence = False
                for line in self.read(rel).split("\n"):
                    if line.strip().startswith("```"):
                        in_fence = not in_fence
                    if in_fence or not line.startswith("#"):
                        continue
                    if line in seen:
                        dupes.append(line)
                    seen.add(line)
                self.assertEqual(dupes, [], "duplicate headings in %s" % rel)

    def test_numbered_section_headings_use_a_consistent_level(self):
        """A numbered section and its lettered siblings must be the same depth."""
        import re
        from collections import defaultdict

        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md"):
            with self.subTest(rel=rel):
                depths = defaultdict(set)
                for line in self.read(rel).split("\n"):
                    m = re.match(r"^(#{2,4})\s+(\d+)[a-z]*\.", line)
                    if m:
                        depths[m.group(2)].add(len(m.group(1)))
                for number, levels in depths.items():
                    self.assertEqual(
                        len(levels), 1,
                        "%s: section %s mixes heading levels %s"
                        % (rel, number, sorted(levels)),
                    )

    def test_section_cross_references_resolve(self):
        import re

        def headings(rel):
            out = set()
            for line in self.read(rel).split("\n"):
                m = re.match(r"^#{2,4}\s+(\d+[a-z]*)\.", line)
                if m:
                    out.add(m.group(1))
            return out

        known = {rel: headings(rel) for rel in
                 ("CLAUDE.md", "INVESTMENT_POLICY.md", "DISCOVERY_POLICY.md")}
        for rel in self.POLICY_DOCS:
            lines = self.read(rel).split("\n")
            for n, line in enumerate(lines):
                context = line + " " + (lines[n + 1] if n + 1 < len(lines) else "")
                for m in re.finditer(r"§(\d+[a-z]*)", line):
                    if "INVESTMENT_POLICY.md" in context:
                        doc = "INVESTMENT_POLICY.md"
                    elif "DISCOVERY_POLICY.md" in context:
                        doc = "DISCOVERY_POLICY.md"
                    elif rel in known:
                        doc = rel
                    else:
                        continue
                    with self.subTest(rel=rel, line=n + 1, ref=m.group(1)):
                        self.assertIn(m.group(1), known[doc],
                                      "%s:%d references %s §%s, which does not exist"
                                      % (rel, n + 1, doc, m.group(1)))

    def test_prompt_step_numbers_are_sequential_and_unique(self):
        import re

        for rel in ("prompts/evaluate_market.md", "prompts/scheduled_evaluation.md"):
            with self.subTest(rel=rel):
                steps = [m.group(1) for m in re.finditer(
                    r"^## Step (\d+)[a-z]* —", self.read(rel), re.M)]
                numbers = [int(s) for s in steps]
                self.assertEqual(numbers, sorted(numbers),
                                 "%s step numbers are out of order: %s" % (rel, numbers))
                # each major step appears once (sub-steps like 8a share the number)
                majors = [n for n, s in zip(numbers, re.findall(
                    r"^## Step (\d+[a-z]*) —", self.read(rel), re.M)) if s.isdigit()]
                self.assertEqual(len(majors), len(set(majors)),
                                 "%s repeats a step number: %s" % (rel, majors))

    def test_the_plan_validation_path_is_documented_in_the_interactive_prompt(self):
        body = self.read("prompts/evaluate_market.md")
        self.assertIn("validate_plan.py", body)
        self.assertIn("SPLIT_BUY_PLAN", body)


class LaunchdEnvironmentIndependenceTests(unittest.TestCase):
    """The installed job must not depend on anything a shell did.

    The bug this covers: `run-now` succeeded from an interactive Terminal that
    had already sourced nvm, while the scheduled job died on
    `claude: not found`. Nothing here may resolve an executable by bare name.
    """

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    PLISTS = (
        "scheduler/com.robinhood-agent.evaluation.plist",
        "scheduler/com.robinhood-agent.evaluation-pm.plist",
    )

    def test_the_plists_declare_the_resolved_runtime(self):
        import plistlib
        for rel in self.PLISTS:
            with self.subTest(rel=rel):
                with open(os.path.join(REPO_ROOT, rel), "rb") as handle:
                    env = plistlib.loads(handle.read())["EnvironmentVariables"]
                self.assertEqual(env["CLAUDE_BIN"], "__CLAUDE_BIN__")
                self.assertEqual(env["NODE_BIN"], "__NODE_BIN__")
                self.assertEqual(env["PATH"], "__PATH__")

    def test_no_plist_hard_codes_a_home_relative_path(self):
        """A literal ~ or $HOME in a plist is not expanded by launchd."""
        for rel in self.PLISTS:
            with self.subTest(rel=rel):
                body = self.read(rel)
                for line in body.split("\n"):
                    if line.strip().startswith("<string>"):
                        self.assertNotIn("~/", line)
                        self.assertNotIn("$HOME", line)

    def test_the_installer_no_longer_hard_codes_a_path(self):
        """The old bug: a guessed PATH that happened to omit the real claude."""
        body = self.read("scheduler/install.sh")
        self.assertNotIn("s|__PATH__|", body)
        self.assertIn("resolve_launchd_runtime.py", body)

    def test_the_installer_renders_through_the_resolver(self):
        body = self.read("scheduler/install.sh")
        self.assertIn("--render", body)
        self.assertIn("--output", body)

    def test_the_installer_refuses_to_enable_on_a_resolution_failure(self):
        body = self.read("scheduler/install.sh")
        self.assertIn("refusing to continue", body)
        self.assertIn("CLAUDE_BIN=/absolute/path/to/claude", body)

    def test_the_installer_verifies_before_it_bootstraps(self):
        """enable must test the rendered job before launchctl ever sees it."""
        body = self.read("scheduler/install.sh")
        verify_at = body.index("verify_plist \"$STAGED\"")
        bootstrap_at = body.index("launchctl bootstrap")
        self.assertLess(verify_at, bootstrap_at)

    def test_the_installer_exposes_a_verify_action_that_installs_nothing(self):
        body = self.read("scheduler/install.sh")
        self.assertIn("verify)", body)
        self.assertIn("{enable|disable|status|verify|run-now}", body)
        # The verify branch must not load anything into launchd.
        verify_branch = body[body.index("  verify)"):body.index("  run-now)")]
        for forbidden in ("launchctl bootstrap", "launchctl enable", "launchctl kickstart"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, verify_branch)

    def test_verification_runs_the_rendered_job_under_a_stripped_environment(self):
        """`env -i` is the point: it discards the calling shell entirely."""
        body = self.read("scheduler/install.sh")
        self.assertIn("env -i", body)
        self.assertIn("--check-runtime", body)
        self.assertIn("--env-args", body)
        self.assertIn("--program-args", body)

    def test_the_runner_invokes_claude_by_absolute_path(self):
        body = self.read("scripts/scheduled_evaluation.sh")
        self.assertIn('"$CLAUDE_BIN" -p "$PROMPT"', body)
        # ...and rejects a relative CLAUDE_BIN rather than trusting PATH.
        self.assertIn("is not an absolute path", body)

    def test_the_runner_fails_with_a_diagnosis_not_just_an_error(self):
        body = self.read("scripts/scheduled_evaluation.sh")
        self.assertIn("launchd reads no .zshrc", body)
        self.assertIn("install.sh verify", body)

    def test_the_runner_pins_node_only_when_the_plist_supplies_one(self):
        body = self.read("scripts/scheduled_evaluation.sh")
        self.assertIn('NODE_BIN="${NODE_BIN:-}"', body)
        self.assertIn('dirname "$NODE_BIN"', body)

    def test_the_runner_has_a_check_runtime_mode_that_writes_no_report(self):
        body = self.read("scripts/scheduled_evaluation.sh")
        self.assertIn("--check-runtime)", body)
        # The check-runtime exit must precede the Claude invocation.
        self.assertLess(
            body.index('if [ "$CHECK_RUNTIME" -eq 1 ]; then'),
            body.index('"$CLAUDE_BIN" -p "$PROMPT"'),
        )

    def test_the_resolution_module_takes_no_subprocess_dependency(self):
        """src/ may not import subprocess; the probe lives in scripts/."""
        import ast
        path = os.path.join(REPO_ROOT, "src", "launchd.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), "launchd.py")
        banned = {"subprocess", "socket", "urllib", "http", "requests", "asyncio"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], banned)

    def test_the_resolver_script_runs_only_a_version_check(self):
        body = self.read("scripts/resolve_launchd_runtime.py")
        self.assertIn('"--version"', body)
        for tool in ORDER_TOOLS:
            with self.subTest(tool=tool):
                self.assertNotIn(tool, body)

    def test_the_scheduling_surface_is_itself_immutable_during_a_run(self):
        """A run may not rewrite when or how it runs."""
        for rel in (
            "src/scheduling.py",
            "src/launchd.py",
            "src/market_calendar.py",
            "prompts/scheduled_evaluation.md",
            "scripts/scheduled_evaluation.sh",
            "scheduler/install.sh",
        ) + self.PLISTS:
            with self.subTest(rel=rel):
                self.assertIn(rel, IMMUTABLE_DURING_SCHEDULED_RUN)


class ExternalResearchPolicyTests(unittest.TestCase):
    """Robinhood is authoritative for the account, not for the network."""

    DOCS = ("CLAUDE.md", "INVESTMENT_POLICY.md", "DISCOVERY_POLICY.md",
            "prompts/scheduled_evaluation.md", "prompts/evaluate_market.md")

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_read_only_web_research_is_permitted_for_crypto(self):
        for rel in self.DOCS:
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                self.assertTrue(
                    "webfetch" in body or "read-only web research" in body,
                    "%s does not permit read-only external research" % rel,
                )

    def test_robinhood_authority_is_scoped_in_every_document(self):
        for rel in self.DOCS:
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                for subject in ("cost basis", "tradability"):
                    self.assertIn(subject, body)

    def test_the_broker_scope_excuse_is_named_and_rejected(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md", "DISCOVERY_POLICY.md",
                    "prompts/scheduled_evaluation.md"):
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                self.assertIn("research_incomplete", body)
                self.assertIn("does not expose", body)

    def test_a_real_obstacle_remains_a_valid_waiver(self):
        """The escape hatch must stay documented, or honesty gets punished."""
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md", "DISCOVERY_POLICY.md",
                    "prompts/scheduled_evaluation.md"):
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                self.assertIn("obstacle", body)

    def test_the_runner_allows_the_read_only_web_tools(self):
        body = self.read("scripts/scheduled_evaluation.sh")
        start = body.index("ALLOWED_TOOLS=")
        allowed = body[start:body.index("\n\n", start)]
        for tool in ("WebSearch", "WebFetch"):
            with self.subTest(tool=tool):
                self.assertIn(tool, allowed)

    def test_the_crypto_source_types_are_documented_where_they_are_enforced(self):
        from src.models import CRYPTO_PRIMARY_SOURCE_TYPES

        body = self.read("DISCOVERY_POLICY.md")
        for source_type in CRYPTO_PRIMARY_SOURCE_TYPES:
            with self.subTest(source_type=source_type):
                self.assertIn(source_type, body)

    def test_equity_primary_sources_are_unchanged(self):
        from src.models import PRIMARY_SOURCE_TYPES

        self.assertEqual(
            PRIMARY_SOURCE_TYPES,
            frozenset({"SEC_FILING", "COMPANY_IR", "EARNINGS_RELEASE",
                       "OFFICIAL_FINANCIALS"}),
        )


class CalendarBoundaryPolicyTests(unittest.TestCase):
    """The month boundary must be documented everywhere it is expected."""

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    DOCS = ("CLAUDE.md", "INVESTMENT_POLICY.md",
            "prompts/scheduled_evaluation.md", "prompts/evaluate_market.md")

    def test_the_final_tradable_opportunity_is_defined(self):
        for rel in self.DOCS:
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                self.assertIn("final tradable opportunity", body)

    def test_the_expiry_consequence_is_stated(self):
        for rel in self.DOCS:
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                self.assertIn("expire unused", body)

    def test_the_worked_mu_case_is_recorded(self):
        for rel in self.DOCS:
            with self.subTest(rel=rel):
                body = self.read(rel)
                self.assertIn("2026-09-30", body)
                self.assertIn("2026-10-01", body)

    def test_crypto_and_equity_boundaries_are_distinguished(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md",
                    "prompts/scheduled_evaluation.md"):
            with self.subTest(rel=rel):
                body = self.read(rel).lower()
                self.assertIn("closing bell", body)

    def test_the_payload_fields_are_documented(self):
        for rel in ("CLAUDE.md", "INVESTMENT_POLICY.md",
                    "prompts/evaluate_market.md"):
            with self.subTest(rel=rel):
                body = self.read(rel)
                self.assertIn("events_considered", body)
                self.assertIn("authorization_expiry_acknowledged", body)

    def test_the_scheduled_prompt_requires_the_statement_in_the_report(self):
        body = self.read("prompts/scheduled_evaluation.md").lower()
        self.assertIn("actionable", body)
        self.assertIn("next month's authorization", body)


class ThreeTierOutputTests(unittest.TestCase):
    """A short digest is safe only because the depth is written down elsewhere."""

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_the_runner_writes_a_digest_separate_from_the_audit_record(self):
        body = self.read("scripts/scheduled_evaluation.sh")
        self.assertIn('DIGEST_PATH="$REPORTS_DIR/latest.md"', body)
        self.assertIn('REPORT_PATH="$REPORTS_DIR/${STAMP}.md"', body)
        self.assertIn('RESEARCH_DIR="$REPO_ROOT/research"', body)

    def test_the_runner_no_longer_copies_the_report_over_latest(self):
        """The regression that made the daily report 4,800 words."""
        for rel in ("scripts/scheduled_evaluation.sh", "scripts/record_scheduled_run.py"):
            with self.subTest(rel=rel):
                body = self.read(rel)
                self.assertNotIn("shutil.copyfile", body)
                self.assertNotIn("cp \"$REPORT_PATH\"", body)

    def test_the_runner_hands_all_three_paths_to_the_recorder(self):
        body = self.read("scripts/scheduled_evaluation.sh")
        recorder = body[body.index("record_scheduled_run.py"):]
        for flag in ("--report", "--digest", "--research-dir"):
            with self.subTest(flag=flag):
                self.assertIn(flag, recorder)

    def test_the_prompt_names_all_three_destinations(self):
        body = self.read("prompts/scheduled_evaluation.md")
        self.assertIn("$REPORT_PATH", body)
        self.assertIn("$DIGEST_PATH", body)
        self.assertIn("research/<SYMBOL>.md", body)

    def test_the_prompt_states_the_word_target(self):
        from src.reporting import CONCISE_WORD_TARGET

        body = self.read("prompts/scheduled_evaluation.md")
        low, high = CONCISE_WORD_TARGET
        self.assertIn("%d-1,%d words" % (low, high - 1000), body)

    def test_the_prompt_requires_every_digest_section(self):
        from src.reporting import CONCISE_REPORT_SECTIONS

        body = self.read("prompts/scheduled_evaluation.md")
        for section in CONCISE_REPORT_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(section, body)

    def test_the_prompt_lists_what_the_digest_must_leave_out(self):
        body = self.read("prompts/scheduled_evaluation.md").lower()
        for excluded in ("full holdings tables", "watchlist tables",
                         "research packages", "rejection narratives"):
            with self.subTest(excluded=excluded):
                self.assertIn(excluded, body)

    def test_the_prompt_says_shortening_the_report_is_not_shortening_the_work(self):
        body = self.read("prompts/scheduled_evaluation.md")
        self.assertIn("Shortening the report never means shortening the analysis", body)

    def test_the_prompt_forbids_dropping_a_required_element_to_fit(self):
        body = self.read("prompts/scheduled_evaluation.md")
        self.assertIn("cut *detail*, not *elements*", body)

    def test_the_prompt_tells_the_run_to_read_research_notes_first(self):
        body = self.read("prompts/scheduled_evaluation.md")
        self.assertIn("research/INDEX.md", body)
        self.assertIn("This is where prior research lives", body)

    def test_the_prompt_forbids_deleting_or_gutting_a_note(self):
        body = self.read("prompts/scheduled_evaluation.md")
        self.assertIn("Never delete a note", body)

    def test_the_prompt_says_a_note_classification_is_not_authorization(self):
        body = self.read("prompts/scheduled_evaluation.md")
        self.assertIn("reasoning label, never authorization", body.replace("**", ""))

    def test_the_recorder_validates_the_digest(self):
        body = self.read("scripts/record_scheduled_run.py")
        self.assertIn("validate_concise_report", body)
        self.assertIn("byte copy of the audit record", body)

    def test_the_recorder_never_invokes_a_forbidden_script(self):
        body = self.read("scripts/record_scheduled_run.py")
        for script in FORBIDDEN_SCRIPTS:
            with self.subTest(script=script):
                self.assertNotIn(script, body)


class ArchiveLossTests(unittest.TestCase):
    """A run may add to the archive and update a note. It may never lose one."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, "reports"))
        os.makedirs(os.path.join(self.root, "research"))

    def write(self, rel, words):
        path = os.path.join(self.root, rel)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(" ".join("word%d" % n for n in range(words)))

    def test_an_unchanged_archive_passes(self):
        self.write("research/GOOGL.md", 500)
        before = archive_inventory(self.root)
        self.assertEqual(archive_losses(before, archive_inventory(self.root)), [])

    def test_a_deleted_research_note_is_caught(self):
        self.write("research/GOOGL.md", 500)
        before = archive_inventory(self.root)
        os.remove(os.path.join(self.root, "research", "GOOGL.md"))
        losses = archive_losses(before, archive_inventory(self.root))
        self.assertEqual(losses, ["research/GOOGL.md was deleted"])

    def test_a_deleted_audit_record_is_caught(self):
        self.write("reports/2026-01-16_0930.md", 4000)
        before = archive_inventory(self.root)
        os.remove(os.path.join(self.root, "reports", "2026-01-16_0930.md"))
        self.assertTrue(archive_losses(before, archive_inventory(self.root)))

    def test_a_gutted_note_is_caught(self):
        self.write("research/BTC-USD.md", 900)
        before = archive_inventory(self.root)
        self.write("research/BTC-USD.md", 50)
        losses = archive_losses(before, archive_inventory(self.root))
        self.assertEqual(len(losses), 1)
        self.assertIn("shrank from 900 to 50 words", losses[0])

    def test_an_ordinary_edit_is_allowed(self):
        """Refreshing a note is the point of having notes."""
        self.write("research/BTC-USD.md", 900)
        before = archive_inventory(self.root)
        self.write("research/BTC-USD.md", 820)
        self.assertEqual(archive_losses(before, archive_inventory(self.root)), [])

    def test_a_growing_note_is_allowed(self):
        self.write("research/BTC-USD.md", 400)
        before = archive_inventory(self.root)
        self.write("research/BTC-USD.md", 1200)
        self.assertEqual(archive_losses(before, archive_inventory(self.root)), [])

    def test_a_new_note_is_allowed(self):
        before = archive_inventory(self.root)
        self.write("research/NEW.md", 400)
        self.assertEqual(archive_losses(before, archive_inventory(self.root)), [])

    def test_latest_md_is_not_treated_as_an_archive_file(self):
        """The digest is replaced wholesale every run, by design."""
        self.write("reports/latest.md", 1000)
        before = archive_inventory(self.root)
        self.assertNotIn("reports/latest.md", before)
        self.write("reports/latest.md", 200)
        self.assertEqual(archive_losses(before, archive_inventory(self.root)), [])

    def test_the_research_index_is_not_treated_as_an_archive_file(self):
        """It is regenerated after every run, so it legitimately changes size."""
        self.write("research/INDEX.md", 300)
        self.assertNotIn("research/INDEX.md", archive_inventory(self.root))

    def test_postflight_reports_a_lost_note_as_a_safety_failure(self):
        self.write("research/GOOGL.md", 500)
        before = run_digest(self.root)
        os.remove(os.path.join(self.root, "research", "GOOGL.md"))
        check = postflight_safety(before, run_digest(self.root), self.root)
        self.assertFalse(check.ok)
        self.assertTrue(
            any("destroyed prior research" in v for v in check.violations),
            check.violations,
        )
        self.assertIn("no_research_or_audit_record_lost", check.checks)

    def test_postflight_passes_when_the_archive_only_grows(self):
        self.write("research/GOOGL.md", 500)
        before = run_digest(self.root)
        self.write("research/SNDK.md", 600)
        check = postflight_safety(before, run_digest(self.root), self.root)
        self.assertTrue(check.ok, check.violations)

    def test_a_pre_digest_snapshot_still_checks_the_surface(self):
        """The old flat {rel: sha} shape must not silently skip the check."""
        surface, archive = split_digest({"config.json": "abc"})
        self.assertEqual(surface, {"config.json": "abc"})
        self.assertEqual(archive, {})
        check = postflight_safety({"config.json": "abc"}, {"config.json": "def"}, self.root)
        self.assertFalse(check.ok)
        self.assertTrue(any("config.json" in v for v in check.violations))

    def test_a_full_snapshot_round_trips(self):
        self.write("research/GOOGL.md", 500)
        surface, archive = split_digest(run_digest(self.root))
        self.assertIn("config.json", surface)
        self.assertIn("research/GOOGL.md", archive)


class ShippedArchiveTests(unittest.TestCase):
    """The repository as shipped must actually have the deeper tiers."""

    def test_the_archive_contains_the_detailed_audit_records(self):
        inventory = archive_inventory(REPO_ROOT)
        reports = [rel for rel in inventory if rel.startswith("reports/")]
        self.assertTrue(reports, "no audit records found")
        self.assertNotIn("reports/latest.md", inventory)

    def test_the_archive_contains_research_notes(self):
        inventory = archive_inventory(REPO_ROOT)
        notes = [rel for rel in inventory if rel.startswith("research/")]
        self.assertGreaterEqual(len(notes), 1, "no research notes found")

    def test_the_audit_records_are_longer_than_the_digest(self):
        """Proof the detail moved rather than vanished."""
        from src.reporting import word_count

        inventory = archive_inventory(REPO_ROOT)
        with open(os.path.join(REPO_ROOT, "reports", "latest.md"), encoding="utf-8") as h:
            digest_words = word_count(h.read())
        newest = max(rel for rel in inventory if rel.startswith("reports/"))
        self.assertGreater(inventory[newest]["words"], digest_words)


class WriteScopeTests(unittest.TestCase):
    """An unattended run may write three kinds of file. Nothing else."""

    def test_the_three_output_tiers_are_writable(self):
        for rel in ("reports/latest.md", "reports/2026-09-08_1030.md",
                    "research/BTC-USD.md", "research/INDEX.md",
                    "logs/scheduled/2026-09-08_1030_am.log",
                    "logs/scheduled_usage.jsonl"):
            with self.subTest(rel=rel):
                self.assertTrue(is_writable_during_scheduled_run(rel))

    def test_everything_else_in_the_repository_is_not(self):
        for rel in ("config.json", ".claude/settings.json", "CLAUDE.md",
                    "INVESTMENT_POLICY.md", "src/guardrails.py", "src/scheduling.py",
                    "state/budget.json", "state/approvals.json",
                    "state/pending_submission.json", "logs/decisions.jsonl",
                    "data/crypto_universe.json", "tests/test_scheduled.py",
                    "docs/STAGE7_MULTI_BUY.md", "scheduler/install.sh",
                    "prompts/scheduled_evaluation.md", "README.md"):
            with self.subTest(rel=rel):
                self.assertFalse(is_writable_during_scheduled_run(rel))

    def test_every_forbidden_state_write_is_outside_the_scope(self):
        for rel in FORBIDDEN_STATE_WRITES:
            with self.subTest(rel=rel):
                self.assertFalse(is_writable_during_scheduled_run(rel))

    def test_every_immutable_file_is_outside_the_scope(self):
        for rel in IMMUTABLE_DURING_SCHEDULED_RUN:
            with self.subTest(rel=rel):
                self.assertFalse(is_writable_during_scheduled_run(rel))

    def test_a_path_that_merely_starts_like_one_is_not_writable(self):
        for rel in ("reportsX/y.md", "researchy/z.md", "logs/scheduled_other.log",
                    "logs/scheduled_usage.jsonl.bak"):
            with self.subTest(rel=rel):
                self.assertFalse(is_writable_during_scheduled_run(rel))

    def test_traversal_and_empty_paths_are_not_writable(self):
        for rel in ("", "..", "../outside.txt", "../../etc/passwd", "./"):
            with self.subTest(rel=rel):
                self.assertFalse(is_writable_during_scheduled_run(rel))

    def test_a_leading_dot_slash_is_tolerated(self):
        self.assertTrue(is_writable_during_scheduled_run("./reports/latest.md"))


class UnauthorizedWriteDetectionTests(unittest.TestCase):
    """Postflight detects what the hook is meant to prevent."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel in ("reports", "research", "state", "logs/scheduled", "src"):
            os.makedirs(os.path.join(self.root, rel), exist_ok=True)
        self.write("config.json", "{}")
        self.write("state/budget.json", "{}")
        self.write("src/guardrails.py", "# code")

    def write(self, rel, body):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)

    def test_writing_only_the_permitted_paths_is_clean(self):
        before = repo_tree(self.root)
        self.write("reports/latest.md", "digest")
        self.write("reports/2026-09-08_1030.md", "audit")
        self.write("research/BTC-USD.md", "note")
        self.write("logs/scheduled/run.log", "log")
        self.write("logs/scheduled_usage.jsonl", "{}\n")
        self.assertEqual(unauthorized_writes(before, repo_tree(self.root)), [])

    def test_a_created_file_outside_the_scope_is_caught(self):
        before = repo_tree(self.root)
        self.write("notes.md", "stray")
        self.assertEqual(
            unauthorized_writes(before, repo_tree(self.root)), ["notes.md was created"]
        )

    def test_a_modified_file_outside_the_scope_is_caught(self):
        before = repo_tree(self.root)
        self.write("config.json", '{"monthly_budget_usd": "500.00"}')
        strays = unauthorized_writes(before, repo_tree(self.root))
        self.assertEqual(strays, ["config.json was modified"])

    def test_a_deleted_file_outside_the_scope_is_caught(self):
        before = repo_tree(self.root)
        os.remove(os.path.join(self.root, "src", "guardrails.py"))
        self.assertEqual(
            unauthorized_writes(before, repo_tree(self.root)),
            ["src/guardrails.py was deleted"],
        )

    def test_a_new_directory_full_of_files_is_caught(self):
        before = repo_tree(self.root)
        self.write("scratchpad_notes/a.md", "a")
        self.write("scratchpad_notes/b.md", "b")
        strays = unauthorized_writes(before, repo_tree(self.root))
        self.assertEqual(len(strays), 2)

    def test_postflight_reports_it_as_a_safety_failure(self):
        before = run_digest(self.root)
        self.write("state/budget.json", '{"committed_usd": "0.00"}')
        check = postflight_safety(before, run_digest(self.root), self.root)
        self.assertFalse(check.ok)
        self.assertTrue(
            any("outside its permitted paths" in v for v in check.violations),
            check.violations,
        )
        self.assertIn("no_writes_outside_the_permitted_paths", check.checks)

    def test_postflight_passes_when_only_outputs_changed(self):
        before = run_digest(self.root)
        self.write("reports/latest.md", "digest")
        self.write("research/GOOGL.md", "note")
        check = postflight_safety(before, run_digest(self.root), self.root)
        self.assertTrue(check.ok, check.violations)

    def test_a_pre_tree_snapshot_skips_the_check_rather_than_failing(self):
        """An in-flight run whose preflight predates this must not break."""
        surface_only = {"surface": surface_digest(self.root), "archive": {}}
        self.assertEqual(digest_tree(surface_only), {})
        check = postflight_safety(surface_only, run_digest(self.root), self.root)
        self.assertNotIn("no_writes_outside_the_permitted_paths", check.checks)
        self.assertTrue(check.ok, check.violations)

    def test_the_tree_excludes_build_and_editor_noise(self):
        self.write("__pycache__/x.pyc", "junk")
        self.write(".DS_Store", "junk")
        self.write("scratch/notes.md", "junk")
        tree = repo_tree(self.root)
        for rel in ("__pycache__/x.pyc", ".DS_Store", "scratch/notes.md"):
            with self.subTest(rel=rel):
                self.assertNotIn(rel, tree)


class WriteGuardHookTests(unittest.TestCase):
    """The PreToolUse hook that prevents an out-of-scope write."""

    GUARD = os.path.join(REPO_ROOT, "scripts", "scheduled_write_guard.py")

    def run_guard(self, payload, marker="1"):
        """Returns the permission decision, or None when the call is allowed."""
        import json as _json
        import subprocess

        env = dict(os.environ)
        if marker is None:
            env.pop("RH_AGENT_SCHEDULED_RUN", None)
        else:
            env["RH_AGENT_SCHEDULED_RUN"] = marker
        body = payload if isinstance(payload, str) else _json.dumps(payload)
        completed = subprocess.run(
            [sys.executable, self.GUARD],
            input=body.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        out = completed.stdout.decode().strip()
        if not out:
            return None
        return _json.loads(out)["hookSpecificOutput"]

    def test_the_hook_exists_and_is_executable(self):
        self.assertTrue(os.path.isfile(self.GUARD))
        self.assertTrue(os.access(self.GUARD, os.X_OK))

    def test_it_allows_the_three_output_tiers(self):
        for rel in ("reports/latest.md", "reports/2026-09-08_1030.md",
                    "research/BTC-USD.md"):
            with self.subTest(rel=rel):
                self.assertIsNone(
                    self.run_guard({"tool_name": "Write",
                                    "tool_input": {"file_path": rel}})
                )

    def test_it_denies_everything_else(self):
        for rel in ("config.json", ".claude/settings.json", "src/guardrails.py",
                    "state/approvals.json", "state/budget.json",
                    "logs/decisions.jsonl", "CLAUDE.md", "notes.md"):
            with self.subTest(rel=rel):
                decision = self.run_guard(
                    {"tool_name": "Write", "tool_input": {"file_path": rel}}
                )
                self.assertIsNotNone(decision, "%s was allowed" % rel)
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_it_denies_a_write_outside_the_repository(self):
        for rel in ("/tmp/evil.txt", "../escape.txt", "~/notes.md"):
            with self.subTest(rel=rel):
                decision = self.run_guard(
                    {"tool_name": "Write", "tool_input": {"file_path": rel}}
                )
                self.assertIsNotNone(decision, "%s was allowed" % rel)
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_traversal_through_a_permitted_directory_is_denied(self):
        decision = self.run_guard(
            {"tool_name": "Write",
             "tool_input": {"file_path": "reports/../config.json"}}
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_it_guards_edit_as_well_as_write(self):
        for tool in ("Edit", "MultiEdit", "NotebookEdit"):
            with self.subTest(tool=tool):
                decision = self.run_guard(
                    {"tool_name": tool, "tool_input": {"file_path": "config.json"}}
                )
                self.assertIsNotNone(decision)
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_it_fails_closed_on_a_malformed_call(self):
        """A denied write costs a paragraph. An unnoticed one costs the guarantee."""
        for payload in ("not json at all",
                        {"tool_name": "Write", "tool_input": {}},
                        {"tool_name": "Write"},
                        {"tool_name": "Mystery",
                         "tool_input": {"file_path": "reports/x.md"}}):
            with self.subTest(payload=str(payload)[:40]):
                decision = self.run_guard(payload)
                self.assertIsNotNone(decision)
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_it_stands_aside_when_no_scheduled_run_is_in_progress(self):
        """Interactive development in this repository must be unaffected."""
        for rel in ("config.json", "src/guardrails.py", "tests/test_scheduled.py"):
            with self.subTest(rel=rel):
                self.assertIsNone(
                    self.run_guard(
                        {"tool_name": "Write", "tool_input": {"file_path": rel}},
                        marker=None,
                    )
                )

    def test_the_denial_names_the_permitted_paths(self):
        decision = self.run_guard(
            {"tool_name": "Write", "tool_input": {"file_path": "config.json"}}
        )
        reason = decision["permissionDecisionReason"]
        for allowed in WRITABLE_DURING_SCHEDULED_RUN:
            with self.subTest(allowed=allowed):
                self.assertIn(allowed, reason)

    def test_the_guard_places_no_orders_and_approves_nothing(self):
        with open(self.GUARD, encoding="utf-8") as handle:
            body = handle.read()
        for forbidden in ORDER_TOOLS + FORBIDDEN_SCRIPTS:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)


class WriteScopeWiringTests(unittest.TestCase):
    """The hook has to be registered and armed, not merely written."""

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def settings(self):
        return json.loads(self.read(".claude/settings.json"))

    def test_the_hook_is_registered_on_the_writing_tools(self):
        entries = self.settings().get("hooks", {}).get("PreToolUse", [])
        self.assertTrue(entries, "no PreToolUse hook is registered")
        matchers = [e.get("matcher", "") for e in entries]
        joined = " ".join(matchers)
        for tool in ("Write", "Edit", "NotebookEdit"):
            with self.subTest(tool=tool):
                self.assertIn(tool, joined)

    def test_the_registered_command_points_at_the_guard(self):
        entries = self.settings()["hooks"]["PreToolUse"]
        commands = [
            h.get("command", "")
            for entry in entries for h in entry.get("hooks", [])
        ]
        self.assertTrue(
            any("scheduled_write_guard.py" in c for c in commands), commands
        )

    def test_the_runner_arms_the_guard(self):
        body = self.read("scripts/scheduled_evaluation.sh")
        self.assertIn("export %s=1" % SCHEDULED_RUN_MARKER, body)

    def test_the_marker_is_armed_before_claude_is_invoked(self):
        body = self.read("scripts/scheduled_evaluation.sh")
        self.assertLess(
            body.index("export %s=1" % SCHEDULED_RUN_MARKER),
            body.index('"$CLAUDE_BIN" -p "$PROMPT"'),
        )

    def test_the_runner_does_not_grant_edit(self):
        """The run writes whole files; it needs no in-place edit capability."""
        body = self.read("scripts/scheduled_evaluation.sh")
        start = body.index("ALLOWED_TOOLS=")
        allowed = body[start:body.index("\n\n", start)]
        self.assertNotIn("Edit", allowed)

    def test_the_existing_deny_rules_survived(self):
        deny = self.settings()["permissions"]["deny"]
        blob = "\n".join(deny)
        for tool in ORDER_TOOLS + MUTATING_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, blob)
        self.assertIn("approve_decision.py", blob)

    def test_the_shipped_settings_still_pass_the_denial_checks(self):
        violations, _ = check_denied_tools(self.settings())
        self.assertEqual(violations, [])
        violations, _ = check_approval_script_denied(self.settings())
        self.assertEqual(violations, [])


class LatestMdIsNeverACopyTests(unittest.TestCase):
    """reports/latest.md is an independently validated digest, never a copy."""

    RECORDER = os.path.join(REPO_ROOT, "scripts", "record_scheduled_run.py")

    def source(self):
        with open(self.RECORDER, encoding="utf-8") as handle:
            return handle.read()

    def test_the_recorder_imports_no_copy_machinery(self):
        import ast

        tree = ast.parse(self.source(), "record_scheduled_run.py")
        banned = {"shutil", "distutils", "filecmp"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], banned)

    def test_the_recorder_calls_no_copy_function(self):
        import ast

        tree = ast.parse(self.source(), "record_scheduled_run.py")
        banned = {"copyfile", "copy", "copy2", "copyfileobj", "copytree",
                  "rename", "link", "symlink"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                self.assertNotIn(name, banned, "recorder calls %s" % name)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, banned)

    def test_the_only_content_written_to_the_digest_is_derived(self):
        """The single write to the digest path takes derive_digest's output."""
        body = self.source()
        self.assertIn("derived = derive_digest(detail_text, detail_rel)", body)
        self.assertIn("handle.write(derived)", body)
        # ...and the audit record's text is never itself written anywhere.
        self.assertNotIn("handle.write(detail_text)", body)
        self.assertNotIn("handle.write(text)", body)

    def test_the_recorder_flags_a_digest_that_equals_the_audit_record(self):
        body = self.source()
        self.assertIn("text.strip() == detail_text.strip()", body)
        self.assertIn("byte copy of the audit record", body)

    def test_no_script_or_runner_copies_the_report_over_the_digest(self):
        for rel in ("scripts/record_scheduled_run.py",
                    "scripts/scheduled_evaluation.sh",
                    "src/scheduling.py",
                    "src/reporting.py"):
            with self.subTest(rel=rel):
                with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
                    body = handle.read()
                for pattern in ("shutil.copyfile", "copyfile(", "cp \"$REPORT_PATH\"",
                                "cp $REPORT_PATH", "tee \"$DIGEST_PATH\""):
                    self.assertNotIn(pattern, body, "%s: %s" % (rel, pattern))

    def test_a_derived_digest_is_shorter_than_its_source(self):
        from src.reporting import derive_digest, word_count

        detail = self.read_detail()
        derived = derive_digest(detail, "reports/2026-01-16_0930.md")
        self.assertLess(word_count(derived), word_count(detail))
        self.assertNotEqual(derived.strip(), detail.strip())

    def read_detail(self):
        with open(os.path.join(REPO_ROOT, "reports", "2026-01-16_0930.md"),
                  encoding="utf-8") as handle:
            return handle.read()

    def test_the_shipped_digest_is_not_a_copy_of_any_audit_record(self):
        digest = self.read_shipped("reports/latest.md").strip()
        reports = os.path.join(REPO_ROOT, "reports")
        for name in sorted(os.listdir(reports)):
            if name == "latest.md" or not name.endswith(".md"):
                continue
            with self.subTest(name=name):
                self.assertNotEqual(digest, self.read_shipped("reports/" + name).strip())

    def read_shipped(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()


class WeeklyDiscoveryContractTests(unittest.TestCase):
    """A discovery pass widens the universe and proposes nothing.

    It has strictly LESS authority than the weekday evaluation: the weekday run
    may recommend WAIT / SINGLE_BUY / SPLIT_BUY_PLAN, and this one may not even
    do that.
    """

    VALID = """# Weekly Discovery — 2026-09-12

## Scope

Read on 2026-09-12: IPO Access (48), Newly listed crypto (31), Sector ETFs
(629). Deferred to next week: the sector universes and Upcoming earnings.
Attention lists were read but everything from them carries a higher bar.

## New candidates

| Symbol | Source list | Why it earns a note |
|---|---|---|
| CRCL | IPO Access | Payments infrastructure, no household exposure |
| KLAR | IPO Access | Consumer credit, genuinely new listing |

## Screened out

- 31 newly listed crypto pairs: absent from the eligibility snapshot.
- 12 ETFs: leveraged or inverse, forbidden outright.
- 6 names: below the penny-stock floor.

## Research queued

- `research/CRCL.md` — new, classification RESEARCH_INCOMPLETE
- `research/KLAR.md` — new, classification RESEARCH_INCOMPLETE

## No purchase proposed

This pass proposes no purchase and nothing here is a recommendation. Any
candidate must still win the five-way capital-use comparison in a weekday
evaluation before a single dollar moves. Nothing was enabled or submitted.
"""

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_a_well_formed_discovery_report_passes(self):
        check = validate_discovery_report(self.VALID)
        self.assertTrue(check.ok, check.violations)
        self.assertIsNone(check.decision)

    def test_dropping_any_required_section_fails(self):
        for section in DISCOVERY_REPORT_SECTIONS:
            with self.subTest(section=section):
                lines, dropping = [], False
                for line in self.VALID.split("\n"):
                    if line.startswith("## "):
                        dropping = section.lower() in line.lower()
                    if not dropping:
                        lines.append(line)
                check = validate_discovery_report("\n".join(lines))
                self.assertFalse(check.ok, "%s could be dropped" % section)

    # --- the load-bearing property ---------------------------------------

    def test_a_decision_line_is_rejected(self):
        for verdict in ("WAIT", "SINGLE_BUY", "SPLIT_BUY_PLAN"):
            with self.subTest(verdict=verdict):
                check = validate_discovery_report(
                    self.VALID + "\n\nDECISION: %s\n" % verdict)
                self.assertFalse(check.ok)
                self.assertTrue(
                    any("must not contain" in v for v in check.violations))

    def test_a_proposed_purchase_is_rejected(self):
        check = validate_discovery_report(
            self.VALID.replace("This pass proposes no purchase",
                               "I recommend a purchase of CRCL; this pass proposes no purchase"))
        self.assertFalse(check.ok)

    def test_a_dollar_allocation_is_rejected(self):
        check = validate_discovery_report(
            self.VALID + "\n\nAllocate $10.00 to CRCL.\n")
        self.assertFalse(check.ok)
        self.assertTrue(any("dollar allocation" in v for v in check.violations))

    def test_approval_language_is_rejected(self):
        check = validate_discovery_report(
            self.VALID + "\n\nCRCL is approved for purchase.\n")
        self.assertFalse(check.ok)
        self.assertTrue(any("approval language" in v for v in check.violations))

    def test_a_decision_id_is_rejected(self):
        check = validate_discovery_report(self.VALID + "\n\ndecision_id: dec_1\n")
        self.assertFalse(check.ok)

    def test_order_submission_language_is_rejected(self):
        check = validate_discovery_report(
            self.VALID + "\n\nI submitted an order for CRCL.\n")
        self.assertFalse(check.ok)

    # --- auditability ----------------------------------------------------

    def test_an_undated_scope_is_rejected(self):
        check = validate_discovery_report(
            self.VALID.replace("Read on 2026-09-12:", "Read recently:")
                      .replace("# Weekly Discovery — 2026-09-12", "# Weekly Discovery"))
        self.assertFalse(check.ok)
        self.assertTrue(any("date what was read" in v for v in check.violations))

    def test_an_empty_candidate_section_is_rejected(self):
        start = self.VALID.index("## New candidates")
        end = self.VALID.index("## Screened out")
        broken = self.VALID[:start] + "## New candidates\n\n" + self.VALID[end:]
        check = validate_discovery_report(broken)
        self.assertFalse(check.ok)

    def test_an_explicit_none_this_week_is_accepted(self):
        start = self.VALID.index("## New candidates")
        end = self.VALID.index("## Screened out")
        variant = (self.VALID[:start]
                   + "## New candidates\n\n- None this week; nothing cleared the screen.\n\n"
                   + self.VALID[end:])
        self.assertTrue(validate_discovery_report(variant).ok)

    def test_a_missing_disclaimer_is_rejected(self):
        check = validate_discovery_report(
            self.VALID.replace("proposes no purchase and nothing here is a recommendation",
                               "found some interesting names"))
        self.assertFalse(check.ok)

    def test_the_word_ceiling_is_enforced(self):
        from src.reporting import DISCOVERY_WORD_HARD_MAX

        padded = self.VALID + "\n\n" + ("filler " * (DISCOVERY_WORD_HARD_MAX + 50))
        check = validate_discovery_report(padded)
        self.assertFalse(check.ok)
        self.assertTrue(any("ceiling" in v for v in check.violations))


class WeeklyDiscoveryWiringTests(unittest.TestCase):
    """The discovery job must be built with the same controls, not fewer."""

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    RUNNER = "scripts/weekly_discovery.sh"
    PLIST = "scheduler/com.robinhood-agent.discovery.plist"

    def test_the_runner_runs_the_report_only_preflight(self):
        body = self.read(self.RUNNER)
        self.assertIn("check_scheduled_safety.py --preflight", body)
        self.assertIn("check_scheduled_safety.py --postflight", body)

    def test_the_runner_arms_the_write_guard(self):
        self.assertIn("export %s=1" % SCHEDULED_RUN_MARKER, self.read(self.RUNNER))

    def test_the_runner_resolves_claude_by_absolute_path(self):
        body = self.read(self.RUNNER)
        self.assertIn('"$CLAUDE_BIN" -p "$PROMPT"', body)
        self.assertIn("is not an absolute path", body)

    def test_the_runner_disallows_every_order_tool(self):
        body = self.read(self.RUNNER)
        start = body.index("DISALLOWED_TOOLS=")
        disallowed = body[start:body.index("\n\n", start)]
        for tool in ORDER_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, disallowed)

    def test_the_runner_allows_no_order_or_mutating_tool(self):
        body = self.read(self.RUNNER)
        start = body.index("ALLOWED_TOOLS=")
        allowed = body[start:body.index("\n\n", start)]
        for tool in ORDER_TOOLS + MUTATING_TOOLS:
            with self.subTest(tool=tool):
                self.assertNotIn(tool, allowed)

    def test_the_runner_grants_no_decision_machinery(self):
        """Discovery has no budget to consult and no decision to validate."""
        body = self.read(self.RUNNER)
        start = body.index("ALLOWED_TOOLS=")
        allowed = body[start:body.index("\n\n", start)]
        for forbidden in ("validate_plan", "validate_decision", "check_status", "Edit"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, allowed)

    def test_the_runner_never_invokes_a_forbidden_script(self):
        body = self.read(self.RUNNER)
        for script in FORBIDDEN_SCRIPTS:
            with self.subTest(script=script):
                self.assertNotIn("python3 scripts/" + script, body)
                self.assertNotIn("./scripts/" + script, body)

    def test_the_runner_grants_the_discovery_substrate(self):
        body = self.read(self.RUNNER)
        for tool in ("get_popular_watchlists", "get_watchlist_items", "WebSearch",
                     "WebFetch", "get_equity_fundamentals"):
            with self.subTest(tool=tool):
                self.assertIn(tool, body)

    def test_the_plist_is_weekly_and_does_not_run_at_load(self):
        import plistlib
        with open(os.path.join(REPO_ROOT, self.PLIST), "rb") as handle:
            data = plistlib.loads(handle.read())
        self.assertFalse(data["RunAtLoad"])
        intervals = data["StartCalendarInterval"]
        self.assertEqual(len(intervals), 1)
        self.assertEqual(intervals[0]["Weekday"], 6)
        self.assertEqual(data["EnvironmentVariables"]["TZ"], "America/Chicago")

    def test_the_plist_carries_the_resolved_runtime_placeholders(self):
        import plistlib
        with open(os.path.join(REPO_ROOT, self.PLIST), "rb") as handle:
            env = plistlib.loads(handle.read())["EnvironmentVariables"]
        self.assertEqual(env["CLAUDE_BIN"], "__CLAUDE_BIN__")
        self.assertEqual(env["NODE_BIN"], "__NODE_BIN__")
        self.assertEqual(env["PATH"], "__PATH__")

    def test_the_plist_runs_the_discovery_runner(self):
        import plistlib
        with open(os.path.join(REPO_ROOT, self.PLIST), "rb") as handle:
            data = plistlib.loads(handle.read())
        self.assertTrue(data["ProgramArguments"][0].endswith("weekly_discovery.sh"))

    def test_the_installer_knows_the_discovery_slot(self):
        body = self.read("scheduler/install.sh")
        self.assertIn("--discovery", body)
        self.assertIn("LABEL_DISCOVERY", body)
        self.assertIn("weekly_discovery.sh", body)

    def test_the_prompt_forbids_proposing_anything(self):
        body = self.read("prompts/weekly_discovery.md")
        self.assertIn("PROPOSES NOTHING", body)
        for phrase in ("report only", "never approve", "never call an order tool"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body.lower())

    def test_the_prompt_guards_against_the_momentum_lists(self):
        body = self.read("prompts/weekly_discovery.md")
        self.assertIn("ROBINHOOD_POPULAR", body)
        self.assertIn("momentum trap", body.lower())

    def test_the_prompt_requires_research_incomplete_on_seeded_notes(self):
        self.assertIn("RESEARCH_INCOMPLETE", self.read("prompts/weekly_discovery.md"))

    def test_the_recorder_validates_that_nothing_was_proposed(self):
        body = self.read("scripts/record_discovery_run.py")
        self.assertIn("validate_discovery_report", body)

    def test_the_discovery_surface_is_immutable_during_a_run(self):
        for rel in ("prompts/weekly_discovery.md", "scripts/weekly_discovery.sh",
                    "scheduler/com.robinhood-agent.discovery.plist",
                    "src/history.py", "src/lessons.py", "scripts/ingest_history.py"):
            with self.subTest(rel=rel):
                self.assertIn(rel, IMMUTABLE_DURING_SCHEDULED_RUN)


class HistoryFileProtectionTests(unittest.TestCase):
    """state/portfolio_history.json is personal, local, and not run-writable."""

    def test_the_history_file_is_gitignored(self):
        import subprocess

        completed = subprocess.run(
            ["git", "check-ignore", "state/portfolio_history.json"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 0,
                         "state/portfolio_history.json is NOT gitignored")

    def test_its_backup_and_temp_files_are_gitignored_too(self):
        import subprocess

        for rel in ("state/portfolio_history.json.bak",
                    "state/portfolio_history.json.tmp"):
            with self.subTest(rel=rel):
                completed = subprocess.run(
                    ["git", "check-ignore", rel], cwd=REPO_ROOT,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.assertEqual(completed.returncode, 0, "%s is not gitignored" % rel)

    def test_a_scheduled_run_may_not_write_it(self):
        self.assertFalse(is_writable_during_scheduled_run("state/portfolio_history.json"))

    def test_the_discovery_outputs_are_writable(self):
        for rel in ("reports/discovery/2026-09-12.md", "research/CRCL.md",
                    "research/lessons/L-001.md", "logs/discovery/2026-09-12.log",
                    "logs/discovery_usage.jsonl"):
            with self.subTest(rel=rel):
                self.assertTrue(is_writable_during_scheduled_run(rel))

    def test_the_ingest_script_calls_no_broker_tool(self):
        with open(os.path.join(REPO_ROOT, "scripts", "ingest_history.py"),
                  encoding="utf-8") as handle:
            body = handle.read()
        for tool in ORDER_TOOLS + MUTATING_TOOLS:
            with self.subTest(tool=tool):
                self.assertNotIn(tool, body)


class PromotionWiringTests(unittest.TestCase):
    """Promotion is a human's command, and the scheduled run cannot reach it.

    The scheduled evaluation writes a machine-readable recommendation and stops.
    Turning that into a pending decision is a separate, human-invoked step, and
    the controls that keep the two apart are asserted here rather than assumed.
    """

    SCRIPT = "scripts/promote_latest_recommendation.py"
    RUNNER = "scripts/scheduled_evaluation.sh"

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    # --- out of a scheduled run's reach -----------------------------------

    def test_promotion_and_ingest_are_forbidden_to_a_scheduled_run(self):
        for script in ("promote_latest_recommendation.py", "ingest_history.py"):
            with self.subTest(script=script):
                self.assertIn(script, FORBIDDEN_SCRIPTS)

    def test_the_model_can_never_invoke_the_promotion_script(self):
        """The grant list is what bounds the model, not the file's text.

        The runner shell *does* invoke promotion, as a post-run step after the
        Claude process has exited. What must remain impossible is the model
        invoking it: its Bash access is two exact commands, and promotion is
        not one of them.
        """
        body = self.read(self.RUNNER)
        start = body.index("ALLOWED_TOOLS=")
        allowed = body[start:body.index("\n\n", start)]
        for script in FORBIDDEN_SCRIPTS:
            with self.subTest(script=script):
                self.assertNotIn(script, allowed)

    def test_discovery_never_promotes_at_all(self):
        """A research pass has no recommendation to promote, post-run or not."""
        self.assertNotIn("promote_latest_recommendation.py",
                         self.read("scripts/weekly_discovery.sh"))

    def test_promotion_runs_only_after_the_model_process_has_exited(self):
        body = self.read(self.RUNNER)
        claude = body.index('"$CLAUDE_BIN" -p "$PROMPT"')
        promote = body.index("promote_latest_recommendation.py")
        self.assertLess(claude, promote,
                        "promotion must not run while the model is alive")

    def test_promotion_is_invoked_with_the_post_run_marker_only(self):
        body = self.read(self.RUNNER)
        line = body[body.index("RH_AGENT_SCHEDULED_POST_RUN=1"):
                    body.index("promote_latest_recommendation.py")]
        self.assertIn("RH_AGENT_SCHEDULED_RUN=", line,
                      "the model marker must be cleared for the post-run step")

    def test_the_two_markers_are_distinct(self):
        self.assertNotEqual(SCHEDULED_RUN_MARKER, SCHEDULED_POST_RUN_MARKER)

    def test_promotion_refuses_the_model_marker_and_accepts_the_post_marker(self):
        import subprocess

        base = dict(os.environ)
        base.pop(SCHEDULED_POST_RUN_MARKER, None)
        model_env = dict(base, **{SCHEDULED_RUN_MARKER: "1"})
        completed = subprocess.run(
            [sys.executable, self.SCRIPT, "--dry-run"], cwd=REPO_ROOT,
            env=model_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 4)

        both = dict(model_env, **{SCHEDULED_POST_RUN_MARKER: "1"})
        completed = subprocess.run(
            [sys.executable, self.SCRIPT, "--dry-run"], cwd=REPO_ROOT,
            env=both, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 4,
                         "both markers set is the model's environment; refuse")

    def test_the_promotion_module_and_script_are_immutable_during_a_run(self):
        for rel in ("src/promotion.py", self.SCRIPT):
            with self.subTest(rel=rel):
                self.assertIn(rel, IMMUTABLE_DURING_SCHEDULED_RUN)

    def test_the_script_refuses_when_the_scheduled_marker_is_set(self):
        import subprocess

        env = dict(os.environ)
        env[SCHEDULED_RUN_MARKER] = "1"
        completed = subprocess.run(
            [sys.executable, self.SCRIPT, "--dry-run"], cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 4, completed.stderr.decode())
        combined = (completed.stdout + completed.stderr).decode().lower()
        self.assertIn("scheduled", combined)

    def test_the_script_calls_no_order_or_mutating_tool(self):
        body = self.read(self.SCRIPT)
        for tool in ORDER_TOOLS + MUTATING_TOOLS:
            with self.subTest(tool=tool):
                self.assertNotIn(tool, body)

    def code_names(self, rel):
        """Identifiers the module actually uses — not words in its prose.

        Both files discuss approval and submission at length, precisely because
        they must never perform either. A substring scan reads those sentences
        as the offence they warn against.
        """
        import ast

        names = set()
        for node in ast.walk(ast.parse(self.read(rel))):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
                names.update(alias.name for alias in node.names)
        return names

    def literals(self, rel):
        import ast

        return {node.value for node in ast.walk(ast.parse(self.read(rel)))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}

    def test_promotion_never_approves(self):
        """It mints a PROPOSED decision and stops. Approval stays a human act."""
        names = self.code_names(self.SCRIPT) | self.code_names("src/promotion.py")
        for forbidden in ("save_approval", "record_approval", "write_approval",
                          "approve", "approve_decision", "APPROVED",
                          "execute", "submit", "Submitter", "submission",
                          "place_equity_order", "place_crypto_order"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, names)

    def test_promotion_writes_no_approval_file(self):
        strings = self.literals(self.SCRIPT) | self.literals("src/promotion.py")
        for text in strings:
            with self.subTest(text=text[:40]):
                self.assertNotIn("approvals.json", text)

    def test_promotion_stops_at_proposed(self):
        self.assertIn("PROPOSED", self.code_names(self.SCRIPT) |
                      self.literals(self.SCRIPT))

    def test_promotion_reads_existing_approvals_only_to_reserve_dollars(self):
        """Reading approvals is how a sibling leg's dollars stay reserved."""
        names = self.code_names(self.SCRIPT)
        self.assertIn("load_approvals", names)
        self.assertIn("sibling_reservations_usd", names)

    def test_promotion_performs_no_broker_io_of_its_own(self):
        """The snapshot is supplied, exactly as src/execution.py requires.

        Asserted against the module's imports rather than its prose, which
        discusses the very modules it must not use.
        """
        import ast

        tree = ast.parse(self.read("src/promotion.py"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("subprocess", "socket", "requests", "urllib", "http",
                          "ftplib", "telnetlib", "asyncio"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, imported)

    # --- the payload the scheduled run leaves behind -----------------------

    def test_the_runner_writes_a_recommendation_path(self):
        body = self.read(self.RUNNER)
        self.assertIn('RECOMMENDATIONS_DIR="$REPORTS_DIR/recommendations"', body)
        self.assertIn('RECOMMENDATION_PATH="$RECOMMENDATIONS_DIR/${STAMP}.json"', body)
        self.assertIn('mkdir -p "$REPORTS_DIR" "$RECOMMENDATIONS_DIR"', body)
        self.assertIn('--recommendation "$RECOMMENDATION_PATH"', body)

    def test_the_recommendation_is_stamped_not_overwritten(self):
        """Unlike the digest, each run's payload stands on its own."""
        body = self.read(self.RUNNER)
        self.assertIn("${STAMP}.json", body)
        self.assertNotIn("recommendations/latest.json", body)

    def test_the_recorder_accepts_and_checks_the_payload(self):
        body = self.read("scripts/record_scheduled_run.py")
        self.assertIn('"--recommendation"', body)
        self.assertIn("def check_recommendation", body)
        self.assertIn("validate_recommendation", body)

    def test_a_scheduled_run_may_write_the_recommendation(self):
        self.assertTrue(
            is_writable_during_scheduled_run("reports/recommendations/2026-09-09_1156.json"))

    def test_a_scheduled_run_may_not_write_a_decision_or_an_approval(self):
        for rel in ("state/approvals.json", "state/last_evaluation.json",
                    "logs/decisions.jsonl", "state/budget.json"):
            with self.subTest(rel=rel):
                self.assertFalse(is_writable_during_scheduled_run(rel))

    # --- the prompt asks for both halves ----------------------------------

    def test_the_prompt_requires_the_action_banner(self):
        body = self.read("prompts/scheduled_evaluation.md")
        self.assertIn("ACTION: BUY", body)
        self.assertIn("ACTION: NONE", body)
        self.assertIn("Remaining monthly authorization", body)

    def test_the_prompt_requires_the_recommendation_payload_for_a_buy_only(self):
        body = self.read("prompts/scheduled_evaluation.md")
        step = body[body.index("## Step 13"):]
        self.assertIn("only if", step.lower()[:200])
        self.assertIn("reports/recommendations/", step)
        for forbidden in ("decision_id", "approval"):
            with self.subTest(forbidden=forbidden):
                self.assertIn(forbidden, step)

    def test_the_prompt_still_forbids_approval_and_submission(self):
        body = self.read("prompts/scheduled_evaluation.md")
        self.assertIn("report only", body.lower())
        self.assertIn("Do not run `scripts/approve_decision.py`", body)
        self.assertIn("Never submit anything", body)


class CapitalGateWiringTests(unittest.TestCase):
    """The gate must sit in front of the expensive call, and skip safely.

    A skip is not a job failure. The launchd schedule stays installed, and the
    next run re-decides from scratch — which is what makes a new month or a
    deposit reactivate evaluation with no human action.
    """

    RUNNER = "scripts/scheduled_evaluation.sh"

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_the_gate_runs_before_claude_is_invoked(self):
        body = self.read(self.RUNNER)
        gate = body.index("check_capital_gate.py")
        claude = body.index('"$CLAUDE_BIN" -p "$PROMPT"')
        self.assertLess(gate, claude, "the gate must precede the expensive call")

    def test_every_skip_outcome_exits_successfully(self):
        """A finished month and an unfunded account are not job failures.

        Each numbered branch must end in `exit 0`, so launchd records a clean
        run and the schedule stays installed. Only the catch-all — an exit code
        the gate is not supposed to produce — fails the job.
        """
        body = self.read(self.RUNNER)
        start = body.index('case "$GATE_EXIT"')
        section = body[start:body.index("esac", start)]
        branches = section.split(";;")
        for code in ("10)", "11)", "12)"):
            with self.subTest(code=code):
                branch = next(b for b in branches if code in b)
                self.assertIn("exit 0", branch)
                self.assertNotIn("exit 1", branch)
        catch_all = next(b for b in branches if b.strip().startswith("*)"))
        self.assertIn("exit 1", catch_all)

    def test_an_unexpected_gate_code_fails_closed(self):
        body = self.read(self.RUNNER)
        section = body[body.index("1a. Capital gate"):body.index("# 2. Run Claude")]
        self.assertIn("failing closed", section)

    def test_the_gate_script_calls_no_broker_or_order_tool(self):
        body = self.read("scripts/check_capital_gate.py")
        for tool in ORDER_TOOLS + MUTATING_TOOLS:
            with self.subTest(tool=tool):
                self.assertNotIn(tool, body)

    def test_the_gate_and_notifier_are_immutable_during_a_run(self):
        for rel in ("src/capital_gate.py", "src/notifications.py",
                    "scripts/check_capital_gate.py", "scripts/notify.py"):
            with self.subTest(rel=rel):
                self.assertIn(rel, IMMUTABLE_DURING_SCHEDULED_RUN)

    def test_a_scheduled_run_may_not_write_the_notification_ledger(self):
        """Otherwise the model could silence an alert about its own advice."""
        self.assertFalse(is_writable_during_scheduled_run("state/notifications.json"))

    def test_the_gate_never_arms_execution(self):
        body = self.read("scripts/check_capital_gate.py")
        for forbidden in ("live_trading\"] =", "agent_enabled\"] =", "APPROVAL_REQUIRED"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_the_runner_delivers_notifications_through_the_notifier(self):
        body = self.read(self.RUNNER)
        self.assertIn("scripts/notify.py --from-json", body)

    def test_notification_delivery_failure_does_not_fail_the_run(self):
        body = self.read(self.RUNNER)
        self.assertIn("notification delivery failed (the report is still written)", body)

    def test_the_recorder_emits_the_actionable_notification(self):
        body = self.read("scripts/record_scheduled_run.py")
        self.assertIn("def emit_notification", body)
        self.assertIn('"--emit-notification"', body)
        self.assertIn("actionable_buy", body)

    def test_weekly_discovery_is_not_gated_on_deployable_capital(self):
        """Research improves future decisions even in a spent month."""
        body = self.read("scripts/weekly_discovery.sh")
        self.assertNotIn("check_capital_gate.py", body)

    def test_the_notifier_is_the_only_thing_importing_subprocess(self):
        """src/ stays clean; delivery lives in scripts/ where I/O is allowed."""
        import ast

        for rel in ("src/capital_gate.py", "src/notifications.py"):
            with self.subTest(rel=rel):
                tree = ast.parse(self.read(rel))
                names = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        names.add(node.module.split(".")[0])
                self.assertNotIn("subprocess", names)
                self.assertNotIn("socket", names)


class SnapshotRefreshScopeTests(unittest.TestCase):
    """The refresh and the evaluation have disjoint write scopes.

    Not nested — disjoint. An evaluation may write reports and research but must
    never write the cash figure its own gate consumed; a refresh may write only
    that figure and nothing else. The first version of the refresh armed the
    *evaluation* scope and then asked for a path that scope denies, so it could
    never have written anything. A standalone run is what exposed it.
    """

    def test_the_two_scopes_share_no_path(self):
        for path in WRITABLE_DURING_SNAPSHOT_REFRESH:
            with self.subTest(path=path):
                self.assertFalse(is_writable_during_scheduled_run(path))
        for path in WRITABLE_DURING_SCHEDULED_RUN:
            with self.subTest(path=path):
                self.assertFalse(is_writable_during_snapshot_refresh(path))

    def test_a_refresh_may_write_only_the_snapshot(self):
        self.assertTrue(is_writable_during_snapshot_refresh(BROKER_SNAPSHOT_PATH))
        for path in ("reports/latest.md", "research/SNDK.md", "config.json",
                     "state/budget.json", "logs/decisions.jsonl",
                     "state/approvals.json", "state/notifications.json"):
            with self.subTest(path=path):
                self.assertFalse(is_writable_during_snapshot_refresh(path))

    def test_an_evaluation_still_cannot_forge_its_own_funding_check(self):
        self.assertFalse(is_writable_during_scheduled_run(BROKER_SNAPSHOT_PATH))

    def test_the_refresh_script_arms_the_refresh_scope_not_the_run_scope(self):
        with open(os.path.join(REPO_ROOT, "scripts", "refresh_broker_snapshot.sh"),
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("export %s=1" % SNAPSHOT_REFRESH_MARKER, body)
        self.assertNotIn("export %s=1" % SCHEDULED_RUN_MARKER, body)

    def test_the_guard_enforces_under_either_marker(self):
        """Asserted on the constants the guard imports, not on literals.

        The guard references SNAPSHOT_REFRESH_MARKER by name; the env-var string
        itself appears only in src/scheduling.py, which is where it belongs.
        """
        with open(os.path.join(REPO_ROOT, "scripts", "scheduled_write_guard.py"),
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("SNAPSHOT_REFRESH_MARKER", body)
        self.assertIn("SCHEDULED_RUN_MARKER", body)
        self.assertIn("is_writable_during_snapshot_refresh", body)

    def test_the_three_markers_are_distinct(self):
        markers = {SCHEDULED_RUN_MARKER, SCHEDULED_POST_RUN_MARKER,
                   SNAPSHOT_REFRESH_MARKER}
        self.assertEqual(len(markers), 3)


class PostflightBaselineOrderingTests(unittest.TestCase):
    """The postflight baseline must be the state the *model* starts from.

    A real end-to-end run failed here. The digest was captured before the
    pre-run steps, so the snapshot refresh — a deterministic shell step that
    legitimately writes state/broker_snapshot.json, deliberately outside the
    model's write scope — was reported as the run writing out of scope. The
    check was correct; the ordering was not.
    """

    RUNNER = "scripts/scheduled_evaluation.sh"

    def setUp(self):
        with open(os.path.join(REPO_ROOT, self.RUNNER), encoding="utf-8") as handle:
            self.body = handle.read()

    def test_the_digest_is_saved_after_the_snapshot_refresh(self):
        last_refresh = self.body.rindex("refresh_broker_snapshot.sh --out")
        last_digest = self.body.rindex("--save-digest")
        self.assertGreater(last_digest, last_refresh,
                           "the baseline must be captured after the refresh")

    def test_the_digest_is_saved_before_the_model_runs(self):
        digest = self.body.rindex("--save-digest")
        claude = self.body.index('"$CLAUDE_BIN" -p "$PROMPT"')
        self.assertLess(digest, claude)

    def test_a_safety_preflight_still_runs_before_anything_else(self):
        """The first preflight keeps its own job: refuse to start if armed."""
        first_preflight = self.body.index("check_scheduled_safety.py --preflight")
        gate = self.body.index("check_capital_gate.py")
        self.assertLess(first_preflight, gate)

    def test_the_snapshot_stays_outside_the_models_write_scope(self):
        """The fix must not have widened what the model may write."""
        self.assertFalse(is_writable_during_scheduled_run(BROKER_SNAPSHOT_PATH))

    def test_a_failed_re_digest_stops_the_run_before_the_model(self):
        section = self.body[self.body.rindex("--save-digest"):
                            self.body.index('"$CLAUDE_BIN" -p "$PROMPT"')]
        self.assertIn("exit 1", section)
        self.assertIn("Not invoking Claude", section)


class ScheduledRunRecorderTests(unittest.TestCase):
    """The recorder, end to end, on the two runs that ended in launchd exit 2.

    The two failures look identical from the outside — ``record_scheduled_run``
    returns non-zero, the runner logs "could not be recorded" and exits 2 — and
    have nothing to do with each other underneath:

    * **2026-09-16** the digest was 1,666 words, genuinely over its ceiling. The
      recorder was right, and must stay right.
    * **2026-09-17** the BUY payload declared itself with ``action``/``side``
      and no ``decision`` key, exactly as ``INVESTMENT_POLICY.md`` §11 describes
      a BUY, and the schema check rejected it anyway.

    Both are pinned here so the fix to the second can never quietly turn the
    first into a pass.
    """

    BUY_BANNER = (
        "> **ACTION: BUY $10.00 SNDK**\n"
        ">\n"
        "> - **Remaining monthly authorization:** $25.00 before, $15.00 after\n"
        "> - **Confidence:** MEDIUM\n"
        "> - **Why:** The contracted revenue floor is now verified in a filing.\n"
        "> - **Human approval required:** Yes — nothing here is approved or "
        "submitted.\n"
    )

    def setUp(self):
        import importlib.util

        from tests.test_reporting import VALID_DIGEST

        path = os.path.join(REPO_ROOT, "scripts", "record_scheduled_run.py")
        spec = importlib.util.spec_from_file_location("_recorder_e2e", path)
        self.recorder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.recorder)
        # Usage tracking appends to the repository's own log; a test must not.
        self.recorder.append_usage = lambda record, *a, **k: None

        self.wait_digest = VALID_DIGEST
        head = VALID_DIGEST.index("> **ACTION:")
        self.buy_digest = (
            VALID_DIGEST[:head] + self.BUY_BANNER + "\n"
            + VALID_DIGEST[VALID_DIGEST.index("## Status"):]
        ).replace("DECISION: WAIT", "DECISION: SINGLE_BUY")

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # -- fixtures ----------------------------------------------------------

    def audit_record(self, decision: str) -> str:
        """An audit record carrying every required section and a decision."""
        from src.reporting import DETAIL_REPORT_SECTIONS

        body = ["# Scheduled Evaluation — 2026-09-17 10:30 America/Chicago", ""]
        for section in DETAIL_REPORT_SECTIONS:
            body.append("## %s" % section)
            body.append("")
            if section == "Decision":
                body.append("DECISION: %s" % decision)
            else:
                body.append("Recorded in full in the audit record.")
            body.append("")
        return "\n".join(body)

    def run_recorder(self, decision: str, digest: str, recommendation=None):
        """Drive the recorder over one complete run directory."""
        report_path = os.path.join(self.tmp, "2026-09-17_1030.md")
        digest_path = os.path.join(self.tmp, "latest.md")
        note_path = os.path.join(self.tmp, "notification.json")
        research_dir = os.path.join(self.tmp, "research")
        os.makedirs(research_dir, exist_ok=True)

        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(self.audit_record(decision))
        with open(digest_path, "w", encoding="utf-8") as handle:
            handle.write(digest)

        argv = ["--report", report_path, "--digest", digest_path,
                "--research-dir", research_dir,
                "--emit-notification", note_path]
        rec_path = os.path.join(self.tmp, "2026-09-17_1030.json")
        if recommendation is not None:
            with open(rec_path, "w", encoding="utf-8") as handle:
                json.dump(recommendation, handle)
            argv += ["--recommendation", rec_path]

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.recorder.main(argv)
        return code, stdout.getvalue(), stderr.getvalue(), note_path

    def buy_recommendation(self, **leg_over):
        """The 2026-09-17 payload shape: no `decision` key on the leg."""
        leg = {
            "action": "BUY",
            "side": "buy",
            "ticker": "SNDK",
            "symbol": "SNDK",
            "asset_class": "EQUITY",
            "asset_type": "us_common_stock",
            "proposed_amount_usd": "10.00",
            "current_price_usd": "1609.1925",
            "quote_timestamp": "2026-09-17T15:31:25Z",
            "confidence": "MEDIUM",
        }
        leg.update(leg_over)
        return {
            "schema_version": 1,
            "generated_at": "2026-09-17T15:35:00Z",
            "source_report": "reports/2026-09-17_1030.md",
            "plan_type": "SINGLE_BUY",
            "legs": [leg],
        }

    # -- 2026-09-16: the digest ceiling, which must keep failing ------------

    def test_a_digest_over_the_word_ceiling_fails_the_run(self):
        """The 2026-09-16 failure. Not a defect — the recorder was correct."""
        from src.reporting import CONCISE_WORD_HARD_MAX, word_count

        padding = "\nThe memory complex moved again overnight and gave it back."
        bloated = self.wait_digest
        while word_count(bloated) <= CONCISE_WORD_HARD_MAX:
            bloated = bloated.replace(
                "## Watching", padding * 12 + "\n\n## Watching", 1)
        self.assertGreater(word_count(bloated), CONCISE_WORD_HARD_MAX)

        code, out, err, _ = self.run_recorder("WAIT", bloated)
        self.assertEqual(code, 1)
        self.assertIn("over the %d-word ceiling" % CONCISE_WORD_HARD_MAX, err)

    def test_a_digest_inside_the_ceiling_does_not_fail_the_run(self):
        """Being above the *target band* is a warning, never a failure."""
        code, out, err, _ = self.run_recorder("WAIT", self.wait_digest)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")

    # -- 2026-09-17: the BUY that could not be recorded ---------------------

    def test_a_buy_declaring_itself_with_action_and_side_records_cleanly(self):
        """The 2026-09-17 failure: a conforming payload, rejected."""
        code, out, err, note_path = self.run_recorder(
            "SINGLE_BUY", self.buy_digest, self.buy_recommendation())
        self.assertEqual(code, 0, err)
        self.assertNotIn("is not a BUY", err)
        self.assertIn("promotable", out)

    def test_that_buy_still_produces_the_approval_notification(self):
        """The alert a human acts on is the point of recording a BUY at all."""
        code, out, err, note_path = self.run_recorder(
            "SINGLE_BUY", self.buy_digest, self.buy_recommendation())
        self.assertEqual(code, 0, err)
        with open(note_path, encoding="utf-8") as handle:
            note = json.load(handle)
        self.assertEqual(note["kind"], "ACTIONABLE_BUY")
        self.assertIn("SNDK", json.dumps(note))

    def test_a_buy_whose_leg_contradicts_itself_still_fails_the_run(self):
        """Relaxing the key is not relaxing the check."""
        code, out, err, _ = self.run_recorder(
            "SINGLE_BUY", self.buy_digest,
            self.buy_recommendation(action="sell"))
        self.assertEqual(code, 1)
        self.assertIn("is not a BUY", err)

    def test_a_buy_leg_that_declares_nothing_still_fails_the_run(self):
        payload = self.buy_recommendation()
        for key in ("action", "side"):
            del payload["legs"][0][key]
        code, out, err, _ = self.run_recorder(
            "SINGLE_BUY", self.buy_digest, payload)
        self.assertEqual(code, 1)
        self.assertIn("does not say what it is", err)
