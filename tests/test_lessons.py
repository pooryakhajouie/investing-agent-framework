"""Lessons — learning from the past without learning the wrong thing.

The properties under test are the ones that stop a learning layer from
degenerating into hindsight bias:

* a finding that needs post-decision information **cannot be typed** as a
  citable lesson;
* a sound decision with a bad outcome yields **no** lesson;
* an unsound decision with a good outcome **does** — that is the luck quadrant;
* one or two instances are not a pattern;
* an opportunity-cost comparator must have been genuinely available, or be a
  benchmark fixed in advance;
* a lesson is advisory, and the guardrails refuse a decision that treats one as
  authorization.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.lessons import (  # noqa: E402
    ACCEPTED_RISK,
    COMPARATOR_BENCHMARK,
    COMPARATOR_DECISION_SET,
    CONFIRMED,
    CORRECTABLE,
    LESSON,
    LESSON_MISUSE_FIELDS,
    LESSON_STALE_AFTER_DAYS,
    LUCK,
    MIN_LESSON_SAMPLE,
    OUTCOME_BAD,
    OUTCOME_GOOD,
    OUTCOME_OBSERVATION,
    OUTCOME_UNKNOWN,
    PREDEFINED_BENCHMARKS,
    PROCESS_SOUND,
    PROCESS_UNKNOWN,
    PROCESS_UNSOUND,
    SCOPE_AGENT,
    SCOPE_MANUAL,
    UNCLASSIFIED,
    Basis,
    Lesson,
    OpportunityCostComparison,
    admissible_comparators,
    citable,
    classify,
    is_learnable,
    outcome_criteria,
    overreach_match,
    process_criteria_at_decision_time,
    references_lesson,
    summarize_quadrants,
    validate_lesson,
    validate_opportunity_cost,
)


def good_lesson(**over) -> Lesson:
    lesson = Lesson(
        id="L-001",
        kind=LESSON,
        statement=(
            "Five new positions were opened on a single day, taking the book "
            "from 15 to 20 lines without a stated diversification argument."
        ),
        quadrant=CORRECTABLE,
        scope=SCOPE_MANUAL,
        confidence="MEDIUM",
        basis=Basis(
            n=5,
            window="2023-07-12 to 2026-09-04",
            data_sources=["state/portfolio_history.json", "get_equity_orders"],
            uses_post_decision_data=False,
        ),
        created_on="2026-09-08",
    )
    for key, value in over.items():
        setattr(lesson, key, value)
    return lesson


class QuadrantTests(unittest.TestCase):
    def test_the_four_quadrants(self):
        self.assertEqual(classify(PROCESS_SOUND, OUTCOME_GOOD), CONFIRMED)
        self.assertEqual(classify(PROCESS_SOUND, OUTCOME_BAD), ACCEPTED_RISK)
        self.assertEqual(classify(PROCESS_UNSOUND, OUTCOME_GOOD), LUCK)
        self.assertEqual(classify(PROCESS_UNSOUND, OUTCOME_BAD), CORRECTABLE)

    def test_unknown_inputs_do_not_get_classified(self):
        self.assertEqual(classify(PROCESS_UNKNOWN, OUTCOME_GOOD), UNCLASSIFIED)
        self.assertEqual(classify(PROCESS_SOUND, OUTCOME_UNKNOWN), UNCLASSIFIED)

    def test_a_profitable_trade_is_not_automatically_a_good_decision(self):
        """The headline rule, as a test."""
        self.assertEqual(classify(PROCESS_UNSOUND, OUTCOME_GOOD), LUCK)
        self.assertTrue(is_learnable(LUCK))

    def test_a_losing_trade_is_not_automatically_a_bad_decision(self):
        """The other half, and the one a naive system gets wrong."""
        self.assertEqual(classify(PROCESS_SOUND, OUTCOME_BAD), ACCEPTED_RISK)
        self.assertFalse(is_learnable(ACCEPTED_RISK))

    def test_accepted_risk_yields_no_lesson(self):
        problems = validate_lesson(good_lesson(quadrant=ACCEPTED_RISK))
        self.assertTrue(any("ACCEPTED_RISK" in p for p in problems))
        self.assertTrue(any("risk aversion" in p for p in problems))

    def test_luck_does_yield_a_lesson(self):
        self.assertEqual(validate_lesson(good_lesson(quadrant=LUCK)), [])

    def test_confirmed_yields_a_lesson(self):
        self.assertEqual(validate_lesson(good_lesson(quadrant=CONFIRMED)), [])

    def test_quadrants_are_countable(self):
        counts = summarize_quadrants([good_lesson(), good_lesson(quadrant=LUCK)])
        self.assertEqual(counts[CORRECTABLE], 1)
        self.assertEqual(counts[LUCK], 1)


class AntiHindsightTests(unittest.TestCase):
    """The type rule that makes hindsight bias structurally impossible."""

    def test_a_finding_using_post_decision_data_cannot_be_a_lesson(self):
        lesson = good_lesson()
        lesson.basis.uses_post_decision_data = True
        problems = validate_lesson(lesson)
        self.assertTrue(any("post-decision data" in p for p in problems))
        self.assertTrue(any("OUTCOME_OBSERVATION" in p for p in problems))

    def test_the_same_finding_is_fine_as_an_outcome_observation(self):
        lesson = good_lesson(kind=OUTCOME_OBSERVATION)
        lesson.basis.uses_post_decision_data = True
        self.assertEqual(validate_lesson(lesson), [])

    def test_an_outcome_observation_is_never_citable(self):
        observation = good_lesson(kind=OUTCOME_OBSERVATION)
        self.assertEqual(citable([observation]), [])

    def test_a_valid_lesson_is_citable(self):
        self.assertEqual(len(citable([good_lesson()], datetime(2026, 9, 9))), 1)

    def test_process_criteria_are_all_knowable_at_decision_time(self):
        for criterion in process_criteria_at_decision_time():
            with self.subTest(criterion=criterion):
                for banned in ("since_entry", "after", "subsequent", "realized"):
                    self.assertNotIn(banned, criterion)

    def test_outcome_criteria_are_disjoint_from_process_criteria(self):
        self.assertEqual(
            set(process_criteria_at_decision_time()) & set(outcome_criteria()), set()
        )


class SampleFloorTests(unittest.TestCase):
    def test_one_instance_is_not_a_pattern(self):
        lesson = good_lesson()
        lesson.basis.n = 1
        problems = validate_lesson(lesson)
        self.assertTrue(any("anecdote" in p for p in problems))

    def test_the_floor_is_enforced_exactly(self):
        for n in range(0, MIN_LESSON_SAMPLE):
            with self.subTest(n=n):
                lesson = good_lesson()
                lesson.basis.n = n
                self.assertTrue(validate_lesson(lesson))
        lesson = good_lesson()
        lesson.basis.n = MIN_LESSON_SAMPLE
        self.assertEqual(validate_lesson(lesson), [])

    def test_a_basis_must_name_its_sources_and_window(self):
        lesson = good_lesson()
        lesson.basis.data_sources = []
        self.assertTrue(any("data_sources" in p for p in validate_lesson(lesson)))
        lesson = good_lesson()
        lesson.basis.window = ""
        self.assertTrue(any("window" in p for p in validate_lesson(lesson)))


class StalenessTests(unittest.TestCase):
    def test_a_fresh_lesson_is_not_stale(self):
        self.assertFalse(good_lesson().is_stale(datetime(2026, 9, 20)))

    def test_a_lesson_goes_stale_and_stops_being_citable(self):
        late = datetime(2026, 9, 8) + timedelta(days=LESSON_STALE_AFTER_DAYS + 5)
        self.assertTrue(good_lesson().is_stale(late))
        self.assertEqual(citable([good_lesson()], late), [])

    def test_a_lesson_is_still_citable_on_the_last_fresh_day(self):
        edge = datetime(2026, 9, 8) + timedelta(days=LESSON_STALE_AFTER_DAYS)
        self.assertFalse(good_lesson().is_stale(edge))
        self.assertEqual(len(citable([good_lesson()], edge)), 1)

    def test_an_undated_lesson_counts_as_stale(self):
        self.assertTrue(good_lesson(created_on=None).is_stale(datetime(2026, 9, 9)))


class AdvisoryStandingTests(unittest.TestCase):
    def test_a_lesson_must_declare_itself_advisory(self):
        problems = validate_lesson(good_lesson(advisory_only=False))
        self.assertTrue(any("never authorization" in p for p in problems))

    def test_a_statement_claiming_authority_is_rejected(self):
        for statement in (
            "This lesson authorizes adding to NVDA without further research.",
            "The precedent approves a $25 purchase here.",
            "Buy it because it worked last time.",
            "No further research is required because the lesson covers it.",
        ):
            with self.subTest(statement=statement[:40]):
                problems = validate_lesson(good_lesson(statement=statement))
                self.assertTrue(
                    any("advisory only" in p for p in problems),
                    "%r was accepted" % statement,
                )

    def test_ordinary_descriptive_statements_pass(self):
        for statement in (
            "Five new positions were opened in one day without a stated argument.",
            "Cost basis rose across every MU lot, from $787.72 to $1,001.80.",
            "The $5 ticket size was used in 99 of 106 purchases.",
        ):
            with self.subTest(statement=statement[:40]):
                self.assertEqual(validate_lesson(good_lesson(statement=statement)), [])

    def test_overreach_detection_is_directly_testable(self):
        self.assertIsNotNone(overreach_match("the lesson authorizes this buy"))
        self.assertIsNone(overreach_match("the lesson describes a pattern"))

    def test_a_lesson_reference_is_recognised(self):
        self.assertTrue(references_lesson("see research/lessons/L-001.md"))
        self.assertTrue(references_lesson("LESSON: averaging up is habitual"))
        self.assertFalse(references_lesson("10-Q filed 2026-07-22"))

    def test_every_misuse_field_names_a_lesson_or_a_precedent(self):
        """The list is what the guardrail scans for; keep it recognisable."""
        self.assertTrue(LESSON_MISUSE_FIELDS)
        for field in LESSON_MISUSE_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(
                    "lesson" in field or "precedent" in field,
                    "%r names neither a lesson nor a precedent" % field,
                )


class OpportunityCostTests(unittest.TestCase):
    """A comparator must have been available, not chosen afterwards."""

    def comparison(self, **over) -> OpportunityCostComparison:
        comparison = OpportunityCostComparison(
            subject_symbol="SNDK",
            subject_date="2026-09-07",
            comparator_symbol="VOO",
            comparator_basis=COMPARATOR_BENCHMARK,
        )
        for key, value in over.items():
            setattr(comparison, key, value)
        return comparison

    def test_a_predefined_benchmark_is_admissible(self):
        for benchmark in PREDEFINED_BENCHMARKS:
            with self.subTest(benchmark=benchmark):
                self.assertEqual(
                    validate_opportunity_cost(
                        self.comparison(comparator_symbol=benchmark)), [])

    def test_a_hindsight_selected_winner_is_refused_as_a_benchmark(self):
        problems = validate_opportunity_cost(self.comparison(comparator_symbol="NVDA"))
        self.assertTrue(any("not a predefined benchmark" in p for p in problems))
        self.assertTrue(any("chosen after the fact" in p for p in problems))

    def test_a_sector_fund_is_not_a_benchmark(self):
        """Only broad-market — a sector fund smuggles a view back in."""
        problems = validate_opportunity_cost(self.comparison(comparator_symbol="SMH"))
        self.assertTrue(problems)

    def test_a_decision_set_comparator_must_be_in_the_decision_set(self):
        comparison = self.comparison(
            comparator_basis=COMPARATOR_DECISION_SET,
            comparator_symbol="GOOGL",
            decision_set=["GOOGL", "ISRG", "BTC-USD"],
            decision_set_source="logs/decisions.jsonl",
        )
        self.assertEqual(validate_opportunity_cost(comparison), [])

    def test_a_comparator_outside_the_decision_set_is_refused(self):
        comparison = self.comparison(
            comparator_basis=COMPARATOR_DECISION_SET,
            comparator_symbol="NVDA",
            decision_set=["GOOGL", "ISRG"],
            decision_set_source="logs/decisions.jsonl",
        )
        problems = validate_opportunity_cost(comparison)
        self.assertTrue(any("was not in the decision set" in p for p in problems))
        self.assertTrue(any("hindsight bias" in p for p in problems))

    def test_a_purchase_with_no_recorded_decision_set_may_only_use_a_benchmark(self):
        """Every pre-agent manual buy is in this position."""
        comparison = self.comparison(
            comparator_basis=COMPARATOR_DECISION_SET,
            comparator_symbol="GOOGL",
            decision_set=[],
        )
        problems = validate_opportunity_cost(comparison)
        self.assertTrue(any("no decision set is recorded" in p for p in problems))
        self.assertTrue(any("predefined benchmark" in p for p in problems))

    def test_a_decision_set_must_name_its_source(self):
        comparison = self.comparison(
            comparator_basis=COMPARATOR_DECISION_SET,
            comparator_symbol="GOOGL",
            decision_set=["GOOGL"],
            decision_set_source="",
        )
        self.assertTrue(any("name its source" in p
                            for p in validate_opportunity_cost(comparison)))

    def test_an_unknown_comparator_basis_is_refused(self):
        problems = validate_opportunity_cost(
            self.comparison(comparator_basis="VIBES"))
        self.assertTrue(any("comparator_basis" in p for p in problems))

    def test_admissible_comparators_without_a_decision_set_are_benchmarks_only(self):
        options = admissible_comparators([], has_decision_set=False)
        self.assertEqual(options["decision_set"], [])
        self.assertEqual(options["benchmarks"], list(PREDEFINED_BENCHMARKS))

    def test_admissible_comparators_with_a_decision_set_include_it(self):
        options = admissible_comparators(["googl", "isrg"], has_decision_set=True)
        self.assertEqual(options["decision_set"], ["GOOGL", "ISRG"])


class WellFormednessTests(unittest.TestCase):
    def test_the_reference_lesson_is_valid(self):
        self.assertEqual(validate_lesson(good_lesson()), [])

    def test_a_lesson_needs_a_statement(self):
        self.assertTrue(any("no statement" in p
                            for p in validate_lesson(good_lesson(statement="  "))))

    def test_an_invalid_kind_is_caught(self):
        self.assertTrue(any("kind" in p for p in validate_lesson(good_lesson(kind="TIP"))))

    def test_an_invalid_scope_is_caught(self):
        self.assertTrue(any("scope" in p for p in validate_lesson(good_lesson(scope="ME"))))

    def test_an_invalid_confidence_is_caught(self):
        self.assertTrue(any("confidence" in p
                            for p in validate_lesson(good_lesson(confidence="SURE"))))

    def test_manual_and_agent_scopes_are_both_expressible(self):
        for scope in (SCOPE_MANUAL, SCOPE_AGENT):
            with self.subTest(scope=scope):
                self.assertEqual(validate_lesson(good_lesson(scope=scope)), [])

    def test_a_lesson_serializes_with_its_basis(self):
        data = good_lesson().to_dict()
        self.assertEqual(data["basis"]["n"], 5)
        self.assertTrue(data["advisory_only"])


if __name__ == "__main__":
    unittest.main()


VALID_LESSON_FILE = """# L-042 — Ticket sizes are consistent

- **id:** L-042
- **kind:** LESSON
- **quadrant:** UNCLASSIFIED
- **scope:** MANUAL_ACTION
- **confidence:** HIGH
- **sample_n:** 221
- **window:** 2023-07-12 to 2026-09-04
- **data_sources:** state/portfolio_history.json, get_equity_orders
- **uses_post_decision_data:** false
- **created_on:** 2026-09-08
- **advisory_only:** true

## Statement

Across 221 filled orders, 172 were placed at exactly $5.00 — 78.5% at the single
most common ticket size. Position sizing is a rule the owner follows rather than
something improvised per purchase.

## What this rests on

The dollar amount entered on each order, fixed at the moment of the decision.
No price history and no outcome of any kind.

## What it does not say

Nothing about whether $5 is the right size, and nothing about the quality of any
individual selection.

## Standing

Advisory. Not authorization, not a research substitute, not a guardrail override.
"""


class LessonFileTests(unittest.TestCase):
    def test_a_well_formed_lesson_file_validates(self):
        from src.lessons import validate_lesson_file

        self.assertEqual(validate_lesson_file(VALID_LESSON_FILE, "L-042.md"), [])

    def test_the_header_parses_into_a_lesson(self):
        from src.lessons import parse_lesson_file

        lesson, header, sections = parse_lesson_file(VALID_LESSON_FILE)
        self.assertEqual(lesson.id, "L-042")
        self.assertEqual(lesson.kind, LESSON)
        self.assertEqual(lesson.scope, SCOPE_MANUAL)
        self.assertEqual(lesson.basis.n, 221)
        self.assertEqual(lesson.confidence, "HIGH")
        self.assertFalse(lesson.basis.uses_post_decision_data)
        self.assertEqual(len(lesson.basis.data_sources), 2)
        self.assertIn("Statement", sections)

    def test_a_missing_header_key_is_caught(self):
        from src.lessons import REQUIRED_LESSON_FILE_KEYS, validate_lesson_file

        for key in REQUIRED_LESSON_FILE_KEYS:
            with self.subTest(key=key):
                broken = VALID_LESSON_FILE.replace(
                    "- **%s:**" % key, "- **removed_%s:**" % key)
                problems = validate_lesson_file(broken, "L-042.md")
                self.assertTrue(problems, "%s could be omitted" % key)

    def test_a_missing_section_is_caught(self):
        from src.lessons import REQUIRED_LESSON_SECTIONS, validate_lesson_file

        for section in REQUIRED_LESSON_SECTIONS:
            with self.subTest(section=section):
                broken = VALID_LESSON_FILE.replace(
                    "## %s" % section, "## Something else")
                self.assertTrue(validate_lesson_file(broken, "L-042.md"))

    def test_a_file_below_the_sample_floor_is_refused(self):
        from src.lessons import validate_lesson_file

        broken = VALID_LESSON_FILE.replace("**sample_n:** 221", "**sample_n:** 2")
        problems = validate_lesson_file(broken, "L-042.md")
        self.assertTrue(any("anecdote" in p for p in problems))

    def test_a_file_using_post_decision_data_cannot_be_a_lesson(self):
        from src.lessons import validate_lesson_file

        broken = VALID_LESSON_FILE.replace(
            "**uses_post_decision_data:** false", "**uses_post_decision_data:** true")
        problems = validate_lesson_file(broken, "L-042.md")
        self.assertTrue(any("OUTCOME_OBSERVATION" in p for p in problems))

    def test_a_file_denying_its_advisory_standing_is_refused(self):
        from src.lessons import validate_lesson_file

        broken = VALID_LESSON_FILE.replace(
            "**advisory_only:** true", "**advisory_only:** false")
        self.assertTrue(validate_lesson_file(broken, "L-042.md"))

    def test_an_accepted_risk_lesson_file_is_refused(self):
        from src.lessons import validate_lesson_file

        broken = VALID_LESSON_FILE.replace(
            "**quadrant:** UNCLASSIFIED", "**quadrant:** ACCEPTED_RISK")
        problems = validate_lesson_file(broken, "L-042.md")
        self.assertTrue(any("ACCEPTED_RISK" in p for p in problems))

    def test_the_index_renders_and_states_the_standing(self):
        from src.lessons import parse_lesson_file, render_lessons_index

        lesson, _, _ = parse_lesson_file(VALID_LESSON_FILE)
        index = render_lessons_index([("L-042.md", lesson)], datetime(2026, 9, 9))
        self.assertIn("L-042", index)
        self.assertIn("advisory", index)
        self.assertIn("not authorization", index)
        self.assertIn("OUTCOME_OBSERVATION", index)

    def test_a_missing_lessons_directory_is_not_an_error(self):
        from src.lessons import list_lesson_paths, validate_lessons_directory

        self.assertEqual(list_lesson_paths("/nonexistent-lessons-xyz"), [])
        self.assertEqual(validate_lessons_directory("/nonexistent-lessons-xyz"), {})


class ShippedLessonsTests(unittest.TestCase):
    """The lessons actually derived from the ingested history."""

    def setUp(self):
        from src.lessons import LESSONS_DIR, list_lesson_paths

        self.dir = LESSONS_DIR
        if not list_lesson_paths(self.dir):
            self.skipTest("no lessons derived yet")

    def test_every_shipped_lesson_is_admissible(self):
        from src.lessons import validate_lessons_directory

        problems = validate_lessons_directory(self.dir)
        self.assertEqual(problems, {}, problems)

    def test_none_rests_on_post_decision_data(self):
        from src.lessons import load_lessons

        for filename, lesson in load_lessons(self.dir):
            with self.subTest(filename=filename):
                if lesson.kind == LESSON:
                    self.assertFalse(lesson.basis.uses_post_decision_data)

    def test_every_shipped_lesson_declares_itself_advisory(self):
        from src.lessons import load_lessons

        for filename, lesson in load_lessons(self.dir):
            with self.subTest(filename=filename):
                self.assertTrue(lesson.advisory_only)

    def test_every_shipped_lesson_meets_the_sample_floor(self):
        from src.lessons import load_lessons

        for filename, lesson in load_lessons(self.dir):
            with self.subTest(filename=filename):
                self.assertGreaterEqual(lesson.basis.n, MIN_LESSON_SAMPLE)

    def test_the_sell_timing_refusal_is_recorded_as_a_lesson(self):
        """The most important thing this history could NOT support."""
        import os

        from src.lessons import list_lesson_paths

        blob = ""
        for path in list_lesson_paths(self.dir):
            with open(path, encoding="utf-8") as handle:
                blob += handle.read()
        self.assertIn("INSUFFICIENT_DATA", blob)
        self.assertIn("sold too early", blob.lower().replace("'", ""))

    def test_the_index_exists_and_lists_every_lesson(self):
        import os

        from src.lessons import LESSONS_INDEX, load_lessons

        self.assertTrue(os.path.exists(LESSONS_INDEX))
        with open(LESSONS_INDEX, encoding="utf-8") as handle:
            index = handle.read()
        for filename, lesson in load_lessons(self.dir):
            with self.subTest(filename=filename):
                self.assertIn(filename, index)
