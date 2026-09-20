"""The canonical BUY contract — one schema, two prompts, no drift.

The 2026-09-17 scheduled BUY was rejected in full at promotion because its
nested blocks used different key spellings from the ones the guardrails read,
and its two calendar numbers were computed by hand and wrong. Neither was
carelessness: the run had been pointed at a prose table that names fields
without spelling their nested keys, while the machine-readable template lived in
a prompt a scheduled run never reads.

These tests hold the repair in place. The property is not "the template looks
complete" — it is that a payload built from the published contract **passes the
real guardrails**, and that both prompts carry that same contract byte for byte.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import guardrails  # noqa: E402
from src.decision_schema import (  # noqa: E402
    PROMPT_FILES,
    TEMPLATE_KINDS,
    buy_leg_example,
    buy_leg_template,
    extract_prompt_payload,
    key_shape,
    optionality_calendar,
    prompt_block,
    recommendation_template,
)
from src.guardrails import days_remaining_in_month, tradable_sessions_remaining  # noqa: E402
from src.promotion import validate_recommendation  # noqa: E402
from tests.helpers import make_config, make_state  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Mid-month on a weekday, so neither the mid-month optionality gate nor a
# month-end session count is the thing under test.
NOW = datetime(2026, 9, 17, 15, 30, tzinfo=timezone.utc)


def validate(leg, now=NOW, committed="0.00"):
    config = make_config()
    state = make_state(config, committed=committed)
    payload = dict(leg)
    payload.setdefault("decision_id", "dec_canonical00001")
    return guardrails.validate(payload, config, state, now=now)


class CanonicalBuyPassesTheGuardrails(unittest.TestCase):
    """The headline property: the published contract is actually promotable."""

    def test_the_new_position_equity_template_validates_clean(self):
        result = validate(buy_leg_example(NOW))
        self.assertTrue(
            result.valid,
            "the canonical BUY must pass its own guardrails: %s"
            % [(v.code, v.message) for v in result.violations])
        self.assertEqual([v.code for v in result.violations], [])

    def test_it_clears_the_new_position_hurdle_specifically(self):
        """The 2026-09-17 payload failed four of these by key name alone."""
        result = validate(buy_leg_example(NOW))
        for code in ("MISSING_NEW_POSITION_JUSTIFICATION",
                     "RESEARCH_INCOMPLETE_FOR_NEW_POSITION",
                     "MISSING_SPRAWL_ASSESSMENT",
                     "MISSING_OPTIONALITY_ANALYSIS"):
            self.assertNotIn(code, [v.code for v in result.violations])

    def test_an_existing_position_template_validates_clean(self):
        result = validate(buy_leg_example(NOW, position_type="EXISTING_POSITION"))
        self.assertTrue(result.valid,
                        [(v.code, v.message) for v in result.violations])

    def test_the_example_and_the_template_have_one_key_structure(self):
        """The filled example may not grow a field the published shape lacks."""
        for kind in sorted(TEMPLATE_KINDS):
            for position_type in ("NEW_POSITION", "EXISTING_POSITION"):
                with self.subTest(kind=kind, position_type=position_type):
                    self.assertEqual(
                        key_shape(buy_leg_example(NOW, kind=kind,
                                                  position_type=position_type)),
                        key_shape(buy_leg_template(NOW, kind=kind,
                                                   position_type=position_type)))

    def test_the_envelope_passes_the_recommendation_schema(self):
        payload = recommendation_template(NOW)
        payload["generated_at"] = "2026-09-17T15:35:00Z"
        payload["legs"] = [buy_leg_example(NOW)]
        self.assertEqual(validate_recommendation(payload), [])

    def test_the_template_mints_no_identifier_and_no_approval(self):
        """A scheduled recommendation may carry neither."""
        leg = buy_leg_template(NOW)
        for forbidden in ("decision_id", "approved", "approval", "approved_by",
                          "user_approved", "execution_state", "fingerprint"):
            self.assertNotIn(forbidden, leg)


class AdHocFieldNamesAreRefused(unittest.TestCase):
    """The exact 2026-09-17 spellings must keep failing, one at a time.

    Loosening the guardrails to accept these was the wrong repair: they enforce
    that specific analysis was *done*, not that a key exists.
    """

    RENAMES = {
        "portfolio_sprawl_assessment": {
            "position_count": "positions_held",
            "economic_significance_of_this_add": "economic_significance",
            "overlap_analysis": "overlap_with_existing",
            "concentration_alternative": "why_not_concentrate_instead",
        },
        "research_package": {
            "current_quote_and_valuation": "quote_and_valuation",
            "balance_sheet": "balance_sheet_condition",
            "material_news": "material_company_news",
        },
        "new_position_justification": {
            "diversification_is_economic_not_cosmetic": "diversification_is_not_the_reason",
            "why_new_position_beats_adding_to_existing": "does_existing_holding_provide_comparable_exposure",
            "initial_size_significance": "would_position_be_too_small_to_matter",
            "best_existing_alternative": "existing_holding_compared_against",
        },
        "monthly_optionality": {
            "known_events_this_month": "analysis",
            "candidates_being_watched": "budget_before_usd",
        },
    }

    def test_each_ad_hoc_rename_is_rejected(self):
        for block, renames in self.RENAMES.items():
            for canonical, ad_hoc in renames.items():
                with self.subTest(block=block, key=canonical):
                    leg = buy_leg_example(NOW)
                    leg[block][ad_hoc] = leg[block].pop(canonical)
                    result = validate(leg)
                    self.assertFalse(
                        result.valid,
                        "%s.%s renamed to %s was accepted" % (block, canonical, ad_hoc))

    def test_a_whole_block_renamed_is_rejected(self):
        leg = buy_leg_example(NOW)
        leg["sprawl"] = leg.pop("portfolio_sprawl_assessment")
        result = validate(leg)
        self.assertIn("MISSING_SPRAWL_ASSESSMENT",
                      [v.code for v in result.violations])


class OptionalityComesFromTheCalendar(unittest.TestCase):
    """The two numbers a run may not compute for itself."""

    def test_the_scaffold_agrees_with_the_guardrail(self):
        calendar = optionality_calendar(NOW)
        self.assertEqual(calendar["days_remaining_in_month"],
                         days_remaining_in_month(NOW))
        self.assertEqual(calendar["tradable_sessions_remaining"],
                         tradable_sessions_remaining(NOW))

    def test_the_template_carries_those_values_not_invented_ones(self):
        block = buy_leg_template(NOW)["monthly_optionality"]
        self.assertEqual(block["days_remaining_in_month"],
                         days_remaining_in_month(NOW))
        self.assertEqual(block["tradable_sessions_remaining"],
                         tradable_sessions_remaining(NOW))

    def test_the_2026_09_17_numbers_are_the_ones_that_were_wrong(self):
        """The regression itself: the run said 13 and 9; the calendar says 14 and 10."""
        calendar = optionality_calendar(NOW)
        self.assertEqual(calendar["days_remaining_in_month"], 14)
        self.assertEqual(calendar["tradable_sessions_remaining"], 10)

        leg = buy_leg_example(NOW)
        leg["monthly_optionality"]["days_remaining_in_month"] = 13
        leg["monthly_optionality"]["tradable_sessions_remaining"] = 9
        codes = [v.code for v in validate(leg).violations]
        self.assertEqual(codes.count("OPTIONALITY_MISREPORTED"), 2)

    def test_a_hand_computed_day_count_is_refused(self):
        for wrong in (1, 13, 15, 31):
            with self.subTest(days=wrong):
                leg = buy_leg_example(NOW)
                leg["monthly_optionality"]["days_remaining_in_month"] = wrong
                if wrong == days_remaining_in_month(NOW):
                    continue
                self.assertIn("OPTIONALITY_MISREPORTED",
                              [v.code for v in validate(leg).violations])


class BothPromptsCarryTheSameContract(unittest.TestCase):
    """Scheduled and interactive evaluation cannot drift apart again."""

    def read(self, relative):
        with open(os.path.join(REPO_ROOT, relative), encoding="utf-8") as handle:
            return handle.read()

    def test_every_prompt_carries_the_generated_block(self):
        generated = prompt_block()
        for relative in PROMPT_FILES:
            with self.subTest(prompt=relative):
                self.assertIn(
                    generated, self.read(relative),
                    "%s is out of date: run "
                    "`python3 scripts/emit_buy_scaffold.py --sync-prompts`" % relative)

    def test_each_prompt_block_parses_to_the_canonical_shape(self):
        canonical = key_shape(recommendation_template(calendar_values=False))
        for relative in PROMPT_FILES:
            with self.subTest(prompt=relative):
                payload = extract_prompt_payload(self.read(relative))
                self.assertIsNotNone(payload, "%s carries no JSON block" % relative)
                self.assertEqual(key_shape(payload), canonical)

    def test_the_two_prompts_agree_with_each_other(self):
        shapes = [key_shape(extract_prompt_payload(self.read(p)))
                  for p in PROMPT_FILES]
        self.assertEqual(shapes[0], shapes[1])

    def test_the_scheduled_prompt_sends_the_run_to_the_scaffold(self):
        """The calendar numbers have to come from somewhere the run can reach."""
        text = self.read("prompts/scheduled_evaluation.md")
        self.assertIn("scripts/emit_buy_scaffold.py", text)

    def test_the_runner_lets_the_scheduled_run_call_it(self):
        """A command the prompt demands and the runner denies is worse than neither."""
        runner = self.read("scripts/scheduled_evaluation.sh")
        self.assertIn("Bash(python3 scripts/emit_buy_scaffold.py:*)", runner)

    def test_the_contract_is_on_the_immutable_surface(self):
        from src.scheduling import IMMUTABLE_DURING_SCHEDULED_RUN

        for path in ("src/decision_schema.py", "scripts/emit_buy_scaffold.py",
                     "prompts/scheduled_evaluation.md"):
            self.assertIn(path, IMMUTABLE_DURING_SCHEDULED_RUN)


if __name__ == "__main__":
    unittest.main()
