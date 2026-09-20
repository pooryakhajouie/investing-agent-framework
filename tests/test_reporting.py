"""Report structure — a short digest that cannot be short of substance.

Two properties under test.

**The digest cannot omit anything that matters.** ``reports/latest.md`` is
budgeted at roughly 800-1,200 words, and every decision-relevant element is
checked independently of that budget: the decision line, the remaining
authorization, all five capital-use rows, the proposed allocation, the
confidence, and the change triggers. There is deliberately no path by which
"it had to fit" removes one of them.

**Shortening the report cannot lose research.** The digest must point at its
audit record and at ``research/``, per-candidate notes carry the standing
analysis across runs, and the archive is fingerprinted before and after every
run so a run that deletes or guts one fails.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import research_notes  # noqa: E402
from src.reporting import (  # noqa: E402
    ACTION_BUY,
    ACTION_NONE,
    BANNED_DIGEST_HEADINGS,
    banner_block,
    parse_action_banner,
    render_action_banner,
    CONCISE_MAX_TABLE_ROWS,
    CONCISE_REPORT_SECTIONS,
    CONCISE_WORD_FLOOR,
    CONCISE_WORD_HARD_MAX,
    CONCISE_WORD_TARGET,
    DETAIL_REPORT_SECTIONS,
    count_bullets,
    count_table_rows,
    derive_digest,
    extract_decision,
    headings,
    missing_concise_sections,
    missing_detail_sections,
    section_body,
    validate_concise_report,
    word_count,
)
from src.research_notes import (  # noqa: E402
    NOTE_STALE_AFTER_DAYS,
    REQUIRED_NOTE_KEYS,
    REQUIRED_NOTE_SECTIONS,
    ResearchNoteError,
    index_rows,
    normalize_symbol,
    note_filename,
    parse_note,
    render_index,
    stale_symbols,
    validate_note,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# A valid digest, and the machinery to break exactly one thing at a time
# --------------------------------------------------------------------------

VALID_DIGEST = """# Scheduled Evaluation — 2026-09-08 10:30 America/Chicago

> **ACTION: NONE — WAIT**
>
> - **Remaining monthly authorization:** $25.00
> - **Confidence:** MEDIUM
> - **Why:** Nothing clears the bar at today's prices.
> - **Human approval required:** Not applicable — no purchase is proposed, so
>   there is nothing to approve.

## Status

REPORT ONLY. Verified before any analysis: `execution_mode=DRY_RUN`,
`agent_enabled=false`, `live_trading=false`. Nothing was approved or submitted.

| | |
|---|---|
| Month | 2026-09 — day 8 of 30, 16 sessions left |
| **Remaining monthly authorization** | **$25.00** |

Broker-reconciled against `get_equity_orders` and `get_crypto_orders`: no
agentic orders this month, matching the local ledger. Household $1,340.02.

## What changed

The memory complex gapped up again overnight and then gave most of it back at
the open. Nothing else material moved, and no holding or finalist reported.
Watchlist membership unchanged. Alphabet's normalized multiple arithmetic is
carried forward from the 2026-07-22 10-Q rather than re-derived, because nothing
since has touched it. The macro setup is unchanged: hike odds near 58-60% into
an FOMC that has not met yet, and record diesel prices feeding the CPI print.

## Five-way capital-use comparison

| Use of the $25 | Candidate | Label | Why not |
|---|---|---|---|
| Existing equity/ETF | GOOGL | `WATCH` | 27-32x normalized; price, not quality |
| New equity/ETF | SNDK | `TOO_EXTENDED` | Extended entry, unverified figure |
| Existing crypto | BTC-USD | `HOLD_NO_ADD` | Adoption weakening, 35.7% concentration |
| New crypto | LINK-USD | `RESEARCH_INCOMPLETE` | Supply and drawdown data absent |
| WAIT — preserve as cash | — | chosen | Everything above fails on a checkable defect |

## Decision

DECISION: WAIT

Nothing clears the bar at today's prices, and each rejection names a specific
defect rather than a vague lack of enthusiasm. The full authorization is
preserved with sixteen sessions and three dated catalysts still in the month.

## Proposed allocation

None — WAIT. Remaining monthly authorization $25.00 of $25.00, so the four-way
ceiling stands at $0.00 against $25.00. No crypto leg is proposed; the reason is
concentration and a drawdown distribution where 77-93% is the historical norm.

## Confidence

MEDIUM. The arithmetic is high-confidence: the authorization is reconciled
against the broker and the concentration figures are computed, not estimated.
The one thing that most undermines it is whether the on-chain adoption decline
reflects falling use or migration to layer-2 rails, which was not resolved.

## Watching

- SanDisk's own filing on the contracted-revenue floor.
- A current dated active-address reading for Bitcoin.
- Chainlink's supply schedule and drawdown history.
- The memory complex at the next open.

## What would change this

- ISRG near 30x trailing EPS, roughly $307 against a 52-week low of $328.57.
- GOOGL nearer 22-24x normalized earnings, materially below tonight's price.
- SNDK pulling back and the contracted floor verified in a filing — both.
- MU reports after the close on 2026-09-30, September's last session, so its
  equity reaction is first tradable 2026-10-01 on October's authorization.

## Detail and evidence

- Audit record: `reports/2026-09-08_1030.md`
- Standing per-candidate research: `research/` (see `research/INDEX.md`)
- Machine state: `state/last_evaluation.json`, `logs/decisions.jsonl`

Every BUY leg would require its own separate human approval. Nothing was
approved, enabled, or submitted.
"""


def digest_without_section(title: str) -> str:
    """The valid digest with one whole section removed."""
    lines = VALID_DIGEST.split("\n")
    out, dropping = [], False
    for line in lines:
        if line.startswith("## "):
            dropping = title.lower() in line.lower()
        if not dropping:
            out.append(line)
    return "\n".join(out)


class ValidDigestTests(unittest.TestCase):
    def test_the_reference_digest_passes(self):
        check = validate_concise_report(VALID_DIGEST)
        self.assertTrue(check.ok, check.violations)
        self.assertEqual(check.decision, "WAIT")

    def test_it_is_within_the_enforced_bounds(self):
        """The fixture is deliberately compact so the tests stay readable.

        The 800-1,200 target band is asserted against the *shipped*
        reports/latest.md in ShippedArtifactTests, where it belongs.
        """
        count = word_count(VALID_DIGEST)
        self.assertGreater(count, CONCISE_WORD_FLOOR)
        self.assertLess(count, CONCISE_WORD_HARD_MAX)

    def test_every_required_check_actually_ran(self):
        check = validate_concise_report(VALID_DIGEST)
        for name in (
            "required_sections_present",
            "decision_declared",
            "safety_and_budget_status_present",
            "five_way_comparison_complete",
            "proposed_allocation_stated",
            "confidence_stated",
            "watching_list_present",
            "change_triggers_present",
            "detail_and_research_are_reachable",
            "within_word_budget",
            "bulk_material_left_to_the_audit_record",
        ):
            with self.subTest(check=name):
                self.assertIn(name, check.checks)


class NothingRequiredCanBeOmittedTests(unittest.TestCase):
    """The load-bearing property: brevity is never an excuse for absence."""

    def test_dropping_any_required_section_fails(self):
        for section in CONCISE_REPORT_SECTIONS:
            with self.subTest(section=section):
                check = validate_concise_report(digest_without_section(section))
                self.assertFalse(check.ok, "%s could be dropped" % section)
                self.assertIn(section, missing_concise_sections(
                    digest_without_section(section)))

    def test_a_missing_decision_line_fails(self):
        broken = VALID_DIGEST.replace("DECISION: WAIT", "We are waiting.")
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertIsNone(check.decision)
        self.assertTrue(any("does not declare a decision" in v for v in check.violations))

    def test_an_invalid_decision_value_is_not_accepted(self):
        broken = VALID_DIGEST.replace("DECISION: WAIT", "DECISION: MAYBE")
        self.assertIsNone(extract_decision(broken))
        self.assertFalse(validate_concise_report(broken).ok)

    def test_a_missing_remaining_authorization_fails(self):
        broken = VALID_DIGEST.replace(
            "| **Remaining monthly authorization** | **$25.00** |",
            "| Authorized | see the audit record |",
        )
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(
            any("remaining monthly authorization" in v.lower() for v in check.violations)
        )

    def test_a_missing_execution_switch_fails(self):
        broken = VALID_DIGEST.replace(
            "`execution_mode=DRY_RUN`,\n`agent_enabled=false`, `live_trading=false`.",
            "Execution is disabled as usual.",
        )
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("execution switches" in v for v in check.violations))

    def test_dropping_any_of_the_five_capital_uses_fails(self):
        rows = {
            "existing equity": "| Existing equity/ETF | GOOGL | `WATCH` | 27-32x normalized; price, not quality |",
            "new equity": "| New equity/ETF | SNDK | `TOO_EXTENDED` | Extended entry, unverified figure |",
            "existing crypto": "| Existing crypto | BTC-USD | `HOLD_NO_ADD` | Adoption weakening, 35.7% concentration |",
            "new crypto": "| New crypto | LINK-USD | `RESEARCH_INCOMPLETE` | Supply and drawdown data absent |",
            "WAIT": "| WAIT — preserve as cash | — | chosen | Everything above fails on a checkable defect |",
        }
        for label, row in rows.items():
            with self.subTest(bucket=label):
                broken = VALID_DIGEST.replace(row + "\n", "")
                self.assertNotEqual(broken, VALID_DIGEST, "row not found: %s" % label)
                check = validate_concise_report(broken)
                self.assertFalse(check.ok, "%s could be dropped" % label)
                self.assertTrue(
                    any("five-way comparison" in v for v in check.violations),
                    check.violations,
                )

    def test_an_empty_five_way_section_fails(self):
        broken = VALID_DIGEST.replace(
            "## Five-way capital-use comparison\n", "## Five-way capital-use comparison\n\n"
        )
        start = broken.index("## Five-way capital-use comparison")
        end = broken.index("## Decision")
        broken = broken[:start] + "## Five-way capital-use comparison\n\n" + broken[end:]
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)

    def test_an_empty_proposed_allocation_fails(self):
        start = VALID_DIGEST.index("## Proposed allocation")
        end = VALID_DIGEST.index("## Confidence")
        broken = VALID_DIGEST[:start] + "## Proposed allocation\n\n" + VALID_DIGEST[end:]
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("Proposed allocation" in v for v in check.violations))

    def test_an_allocation_with_neither_an_amount_nor_none_fails(self):
        start = VALID_DIGEST.index("## Proposed allocation")
        end = VALID_DIGEST.index("## Confidence")
        broken = (
            VALID_DIGEST[:start]
            + "## Proposed allocation\n\nSee the audit record for the plan.\n\n"
            + VALID_DIGEST[end:]
        )
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("neither a dollar amount nor" in v for v in check.violations))

    def test_an_unstated_confidence_fails(self):
        start = VALID_DIGEST.index("## Confidence")
        end = VALID_DIGEST.index("## Watching")
        broken = (
            VALID_DIGEST[:start]
            + "## Confidence\n\nAbout the same as yesterday.\n\n"
            + VALID_DIGEST[end:]
        )
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("LOW, MEDIUM or HIGH" in v for v in check.violations))

    def test_missing_change_triggers_fail(self):
        start = VALID_DIGEST.index("## What would change this")
        end = VALID_DIGEST.index("## Detail and evidence")
        broken = (
            VALID_DIGEST[:start]
            + "## What would change this\n\nA better price, broadly speaking.\n\n"
            + VALID_DIGEST[end:]
        )
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("no specific conditions" in v for v in check.violations))

    def test_an_empty_change_trigger_section_fails(self):
        start = VALID_DIGEST.index("## What would change this")
        end = VALID_DIGEST.index("## Detail and evidence")
        broken = VALID_DIGEST[:start] + "## What would change this\n\n" + VALID_DIGEST[end:]
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("cannot be revisited" in v for v in check.violations))

    def test_change_triggers_may_be_a_table_instead_of_bullets(self):
        start = VALID_DIGEST.index("## What would change this")
        end = VALID_DIGEST.index("## Detail and evidence")
        table = (
            "## What would change this\n\n"
            "| Event | Date | Actionable this month? |\n"
            "|---|---|---|\n"
            "| August CPI | 2026-09-11 | Yes |\n"
            "| MU FQ4, after the close | 2026-09-30 | No — 2026-10-01, October's money |\n\n"
        )
        variant = VALID_DIGEST[:start] + table + VALID_DIGEST[end:]
        self.assertTrue(validate_concise_report(variant).ok)

    def test_a_watch_list_that_is_too_short_fails(self):
        start = VALID_DIGEST.index("## Watching")
        end = VALID_DIGEST.index("## What would change this")
        broken = (
            VALID_DIGEST[:start]
            + "## Watching\n\n- The memory complex.\n- Bitcoin adoption.\n\n"
            + VALID_DIGEST[end:]
        )
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("most important things being watched" in v for v in check.violations))

    def test_a_watch_list_that_is_too_long_only_warns(self):
        start = VALID_DIGEST.index("## Watching")
        end = VALID_DIGEST.index("## What would change this")
        items = "\n".join("- Item %d." % n for n in range(9))
        variant = VALID_DIGEST[:start] + "## Watching\n\n" + items + "\n\n" + VALID_DIGEST[end:]
        check = validate_concise_report(variant)
        self.assertTrue(check.ok, check.violations)
        self.assertTrue(any("against a 3-5 target" in w for w in check.warnings))


class ActionBannerTests(unittest.TestCase):
    """The one line that answers 'is there anything to buy?'

    The user's requirement is that the answer be readable without reading the
    report. That makes the banner load-bearing in two directions: it must be
    present, at the top, and agree with the decision below it — and it must
    never claim more standing than a scheduled recommendation has.
    """

    BUY_BANNER = "\n".join([
        "> **ACTION: BUY $10.00 SNDK**",
        ">",
        "> - **Remaining monthly authorization:** $25.00 → $15.00 if this is "
        "approved and filled",
        "> - **Confidence:** MEDIUM",
        "> - **Why:** The contracted-revenue floor was reconciled from the "
        "company's own filing.",
        "> - **Human approval required:** YES — nothing here is approved or "
        "submitted.",
    ])

    def wait_banner_block(self) -> str:
        start = VALID_DIGEST.index("> **ACTION:")
        end = VALID_DIGEST.index("## Status")
        return VALID_DIGEST[start:end].rstrip() + "\n"

    def buy_digest(self, banner: str = "", decision: str = "SINGLE_BUY") -> str:
        """The reference digest turned into a BUY, one knob at a time."""
        text = VALID_DIGEST.replace(
            self.wait_banner_block(), (banner or self.BUY_BANNER) + "\n")
        text = text.replace("DECISION: WAIT", "DECISION: " + decision)
        return text.replace(
            "None — WAIT. Remaining monthly authorization",
            "$10.00 SNDK. Remaining monthly authorization")

    def swap_banner_field(self, digest: str, old: str, new: str) -> str:
        self.assertIn(old, digest)
        return digest.replace(old, new)

    # --- presence and position -------------------------------------------

    def test_the_reference_digest_carries_a_readable_none_banner(self):
        banner = parse_action_banner(VALID_DIGEST)
        self.assertIsNotNone(banner)
        self.assertEqual(banner.kind, ACTION_NONE)
        self.assertEqual(banner.legs, [])
        self.assertIsNone(banner.total)

    def test_a_digest_with_no_action_line_fails(self):
        broken = VALID_DIGEST.replace(self.wait_banner_block(), "")
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("no ACTION line" in v for v in check.violations))

    def test_the_banner_must_sit_above_the_sections(self):
        """A banner below the fold is not a banner."""
        block = self.wait_banner_block()
        buried = VALID_DIGEST.replace(block, "")
        buried = buried.replace("## Decision", block + "\n## Decision")
        check = validate_concise_report(buried)
        self.assertFalse(check.ok)
        self.assertTrue(any("must be within the first" in v
                            for v in check.violations))

    def test_an_unreadable_action_line_fails(self):
        broken = VALID_DIGEST.replace(
            "> **ACTION: NONE — WAIT**", "> **ACTION: see below**")
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("not readable as either" in v
                            for v in check.violations))

    def test_the_banner_check_runs_on_every_digest(self):
        check = validate_concise_report(VALID_DIGEST)
        self.assertIn(
            "action_banner_present_and_agrees_with_the_decision", check.checks)
        self.assertIn("banner_does_not_overstate_its_standing", check.checks)

    # --- the banner and the decision must say the same thing --------------

    def test_a_buy_digest_with_a_buy_banner_passes(self):
        check = validate_concise_report(self.buy_digest())
        self.assertTrue(check.ok, check.violations)
        self.assertEqual(check.decision, "SINGLE_BUY")

    def test_a_wait_digest_announcing_a_purchase_fails(self):
        broken = VALID_DIGEST.replace(
            self.wait_banner_block(), self.BUY_BANNER + "\n")
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("declares DECISION: WAIT but the ACTION line "
                            "announces a purchase" in v for v in check.violations))

    def test_a_buy_digest_announcing_nothing_fails(self):
        """The failure the live SNDK run actually hit."""
        broken = self.buy_digest(banner=self.wait_banner_block().rstrip())
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("announces no purchase" in v
                            for v in check.violations))

    def test_a_single_buy_announcing_two_legs_fails(self):
        two = self.BUY_BANNER.replace(
            "ACTION: BUY $10.00 SNDK", "ACTION: BUY $10.00 SNDK + $5.00 BTC-USD")
        check = validate_concise_report(self.buy_digest(banner=two))
        self.assertFalse(check.ok)
        self.assertTrue(any("SINGLE_BUY but the ACTION line names 2 legs" in v
                            for v in check.violations))

    def test_a_plan_announcing_one_leg_fails(self):
        check = validate_concise_report(self.buy_digest(decision="SPLIT_BUY_PLAN"))
        self.assertFalse(check.ok)
        self.assertTrue(any("names 1 leg(s); a plan has 2 to 5" in v
                            for v in check.violations))

    def test_a_plan_announcing_two_legs_passes(self):
        two = self.BUY_BANNER.replace(
            "ACTION: BUY $10.00 SNDK", "ACTION: BUY $10.00 SNDK + $5.00 BTC-USD")
        check = validate_concise_report(
            self.buy_digest(banner=two, decision="SPLIT_BUY_PLAN"))
        self.assertTrue(check.ok, check.violations)

    def test_a_plan_of_six_legs_fails(self):
        legs = " + ".join("$4.00 SYM%d" % n for n in range(6))
        six = self.BUY_BANNER.replace("ACTION: BUY $10.00 SNDK", "ACTION: BUY " + legs)
        check = validate_concise_report(
            self.buy_digest(banner=six, decision="SPLIT_BUY_PLAN"))
        self.assertFalse(check.ok)
        self.assertTrue(any("a plan has 2 to 5" in v for v in check.violations))

    # --- the four fields that must follow it ------------------------------

    def test_each_required_field_is_checked_independently(self):
        for label, line in (
            ("remaining", "> - **Remaining monthly authorization:** $25.00 → "
                          "$15.00 if this is approved and filled"),
            ("confidence", "> - **Confidence:** MEDIUM"),
            ("rationale", "> - **Why:** The contracted-revenue floor was "
                          "reconciled from the company's own filing."),
            ("approval", "> - **Human approval required:** YES — nothing here "
                         "is approved or submitted."),
        ):
            with self.subTest(field=label):
                stripped = self.BUY_BANNER.replace(line + "\n", "")
                stripped = stripped.replace("\n" + line, "")
                self.assertNotIn(line, stripped)
                check = validate_concise_report(self.buy_digest(banner=stripped))
                self.assertFalse(check.ok, "%s was not required" % label)
                self.assertTrue(
                    any("does not state the %s" % label in v
                        for v in check.violations),
                    check.violations)

    def test_rationale_may_be_labelled_why_or_rationale(self):
        for label in ("Why", "Rationale"):
            with self.subTest(label=label):
                banner = self.BUY_BANNER.replace("**Why:**", "**%s:**" % label)
                check = validate_concise_report(self.buy_digest(banner=banner))
                self.assertTrue(check.ok, check.violations)

    def test_a_field_further_down_the_page_does_not_count(self):
        """'Immediately after' means inside the block, not somewhere below."""
        stripped = self.BUY_BANNER.replace("> - **Confidence:** MEDIUM\n", "")
        digest = self.buy_digest(banner=stripped)
        self.assertIn("## Confidence", digest)  # the section still exists
        check = validate_concise_report(digest)
        self.assertFalse(check.ok)
        self.assertTrue(any("does not state the confidence" in v
                            for v in check.violations))

    # --- and it never overstates what it is -------------------------------

    def test_overreaching_language_is_caught(self):
        for phrasing in (
            "> - **Human approval required:** NO",
            "> - **Human approval required:** None",
            "> - **Human approval required:** YES, but no approval is needed",
            "> - **Human approval required:** approval is not required",
            "> - **Human approval required:** YES — this leg is pre-approved",
            "> - **Human approval required:** YES — it was already approved",
            "> - **Human approval required:** YES — the order was submitted",
            "> - **Human approval required:** YES — I submitted an order",
            "> - **Human approval required:** YES — the purchase executed",
        ):
            with self.subTest(phrasing=phrasing):
                banner = self.BUY_BANNER.replace(
                    "> - **Human approval required:** YES — nothing here is "
                    "approved or submitted.", phrasing)
                check = validate_concise_report(self.buy_digest(banner=banner))
                self.assertFalse(check.ok, "allowed: %s" % phrasing)
                self.assertTrue(
                    any("ACTION banner" in v and "never approved" in v
                        for v in check.violations), check.violations)

    def test_correct_negations_are_not_flagged(self):
        """The banner's own honest language must survive the scan."""
        for phrasing in (
            "> - **Human approval required:** YES — nothing here is approved or "
            "submitted.",
            "> - **Human approval required:** YES. No order has been submitted "
            "and none will be without your approval.",
            "> - **Human approval required:** YES — this is a recommendation, "
            "not an approved decision, and nothing was submitted.",
        ):
            with self.subTest(phrasing=phrasing):
                banner = self.BUY_BANNER.replace(
                    "> - **Human approval required:** YES — nothing here is "
                    "approved or submitted.", phrasing)
                check = validate_concise_report(self.buy_digest(banner=banner))
                self.assertTrue(check.ok, check.violations)

    def test_a_negation_in_an_earlier_clause_does_not_excuse_a_later_claim(self):
        """The guard is clause-scoped, not sentence-soup-scoped."""
        banner = self.BUY_BANNER.replace(
            "> - **Human approval required:** YES — nothing here is approved or "
            "submitted.",
            "> - **Human approval required:** YES, but no approval was sought "
            "for it, and the order was submitted.")
        check = validate_concise_report(self.buy_digest(banner=banner))
        self.assertFalse(check.ok)
        self.assertTrue(any("claims an order was submitted" in v
                            for v in check.violations), check.violations)

    def test_a_denied_execution_claim_is_not_flagged(self):
        banner = self.BUY_BANNER.replace(
            "nothing here is approved or submitted.",
            "nothing here is approved and no order was executed.")
        self.assertTrue(
            validate_concise_report(self.buy_digest(banner=banner)).ok)

    def test_the_reports_own_status_line_is_not_read_as_the_banner(self):
        """The Status section correctly says 'nothing was approved ... submitted'.

        An earlier version scanned a fixed window of leading lines and flagged
        that sentence as the banner overstating itself.
        """
        self.assertIn("Nothing was approved or submitted.", VALID_DIGEST)
        self.assertTrue(validate_concise_report(VALID_DIGEST).ok)
        block = banner_block(VALID_DIGEST, parse_action_banner(VALID_DIGEST))
        self.assertNotIn("## Status", block)
        self.assertNotIn("REPORT ONLY", block)

    # --- parsing ----------------------------------------------------------

    def test_legs_and_totals_are_parsed(self):
        banner = parse_action_banner("ACTION: BUY $10.00 SNDK + $5.50 BTC-USD")
        self.assertEqual(banner.kind, ACTION_BUY)
        self.assertEqual(banner.legs, [("10.00", "SNDK"), ("5.50", "BTC-USD")])
        self.assertEqual(banner.total, Decimal("15.50"))

    def test_a_bare_dollar_amount_is_accepted(self):
        banner = parse_action_banner("ACTION: BUY $10 SNDK")
        self.assertEqual(banner.legs, [("10", "SNDK")])
        self.assertEqual(banner.total, Decimal("10"))

    def test_markup_around_the_line_is_tolerated(self):
        for line in ("ACTION: BUY $10.00 SNDK",
                     "**ACTION: BUY $10.00 SNDK**",
                     "> **ACTION: BUY $10.00 SNDK**",
                     ">    ACTION:  BUY $10.00 SNDK"):
            with self.subTest(line=line):
                banner = parse_action_banner(line)
                self.assertEqual(banner.kind, ACTION_BUY)
                self.assertEqual(banner.legs, [("10.00", "SNDK")])

    def test_a_wait_banner_is_recognised_in_either_wording(self):
        for line in ("ACTION: NONE — WAIT", "ACTION: NONE", "ACTION: WAIT"):
            with self.subTest(line=line):
                self.assertEqual(parse_action_banner(line).kind, ACTION_NONE)

    def test_no_action_line_parses_to_none(self):
        self.assertIsNone(parse_action_banner("# A report\n\nNothing here."))
        self.assertIsNone(parse_action_banner(""))

    def test_the_line_number_is_reported_one_based(self):
        banner = parse_action_banner("# Title\n\n> **ACTION: NONE — WAIT**\n")
        self.assertEqual(banner.line_number, 3)

    def test_the_dict_form_is_serializable(self):
        import json

        banner = parse_action_banner("ACTION: BUY $10.00 SNDK + $5.00 BTC-USD")
        payload = json.loads(json.dumps(banner.to_dict()))
        self.assertEqual(payload["total"], "15.00")
        self.assertEqual(payload["legs"][1]["symbol"], "BTC-USD")

    # --- rendering round-trips --------------------------------------------

    def test_a_rendered_buy_banner_validates(self):
        rendered = render_action_banner(
            "SINGLE_BUY", [("10.00", "SNDK")], "25.00", "15.00", "MEDIUM",
            "A named, checkable condition fired.")
        check = validate_concise_report(self.buy_digest(banner=rendered))
        self.assertTrue(check.ok, check.violations)

    def test_a_rendered_wait_banner_validates(self):
        rendered = render_action_banner(
            "WAIT", [], "25.00", "25.00", "MEDIUM", "Nothing clears the bar.")
        digest = VALID_DIGEST.replace(
            self.wait_banner_block(), rendered + "\n")
        check = validate_concise_report(digest)
        self.assertTrue(check.ok, check.violations)

    def test_a_rendered_plan_banner_names_every_leg(self):
        rendered = render_action_banner(
            "SPLIT_BUY_PLAN", [("10.00", "SNDK"), ("5.00", "BTC-USD")],
            "25.00", "10.00", "MEDIUM", "Two different return drivers.")
        self.assertIn("ACTION: BUY $10.00 SNDK + $5.00 BTC-USD", rendered)
        check = validate_concise_report(
            self.buy_digest(banner=rendered, decision="SPLIT_BUY_PLAN"))
        self.assertTrue(check.ok, check.violations)

    def test_a_wait_banner_never_promises_a_promotion(self):
        rendered = render_action_banner(
            "WAIT", [], "25.00", "25.00", "LOW", "Nothing clears the bar.")
        self.assertNotIn("promote_latest_recommendation", rendered)
        self.assertIn("nothing to approve", rendered)

    def test_a_buy_banner_points_at_the_promotion_command(self):
        rendered = render_action_banner(
            "SINGLE_BUY", [("10.00", "SNDK")], "25.00", "15.00", "MEDIUM", "x.")
        self.assertIn("scripts/promote_latest_recommendation.py", rendered)
        self.assertIn("approve each leg separately", rendered)

    # --- the derived digest carries one too --------------------------------

    def test_a_derived_digest_banner_agrees_with_the_detail(self):
        from src.reporting import extract_decision

        detail = VALID_DIGEST  # shape is close enough for the derivation
        derived = derive_digest(detail, "reports/2026-09-08_1030.md")
        banner = parse_action_banner(derived)
        self.assertIsNotNone(banner)
        self.assertEqual(
            banner.kind,
            ACTION_NONE if extract_decision(derived) == "WAIT" else ACTION_BUY)


class TheDigestIsNeverTheOnlyCopyTests(unittest.TestCase):
    """A short summary is only safe while the depth stays reachable."""

    def test_a_digest_that_does_not_point_at_its_audit_record_fails(self):
        broken = VALID_DIGEST.replace("`reports/2026-09-08_1030.md`", "the audit record")
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("only copy" in v for v in check.violations))

    def test_a_digest_that_does_not_point_at_research_fails(self):
        broken = VALID_DIGEST.replace(
            "- Standing per-candidate research: `research/` (see `research/INDEX.md`)\n",
            "- Standing per-candidate research: on file\n",
        )
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("re-deriving" in v for v in check.violations))


class WordAndBulkBudgetTests(unittest.TestCase):
    def test_a_digest_over_the_hard_ceiling_fails(self):
        padded = VALID_DIGEST + "\n\n" + ("filler " * (CONCISE_WORD_HARD_MAX + 50))
        check = validate_concise_report(padded)
        self.assertFalse(check.ok)
        self.assertTrue(any("over the %d-word ceiling" % CONCISE_WORD_HARD_MAX in v
                            for v in check.violations))

    def test_the_ceiling_message_says_to_move_detail_not_cut_content(self):
        padded = VALID_DIGEST + "\n\n" + ("filler " * (CONCISE_WORD_HARD_MAX + 50))
        message = next(v for v in validate_concise_report(padded).violations
                       if "ceiling" in v)
        self.assertIn("do not cut required content", message)

    def test_a_digest_above_the_target_but_under_the_ceiling_only_warns(self):
        over = CONCISE_WORD_TARGET[1] + 50 - word_count(VALID_DIGEST)
        padded = VALID_DIGEST + "\n\n" + ("filler " * over)
        check = validate_concise_report(padded)
        self.assertTrue(check.ok, check.violations)
        self.assertTrue(any("above the 800-1200 target band" in w for w in check.warnings))

    def test_a_digest_below_the_floor_fails(self):
        check = validate_concise_report("# Digest\n\nDECISION: WAIT\n")
        self.assertFalse(check.ok)
        self.assertTrue(any("below the %d-word floor" % CONCISE_WORD_FLOOR in v
                            for v in check.violations))

    def test_a_full_holdings_table_blows_the_row_cap(self):
        rows = "\n".join("| SYM%d | $10.00 | $9.00 | +11%% |" % n for n in range(30))
        broken = VALID_DIGEST.replace(
            "## What changed\n",
            "## What changed\n\n| Symbol | Value | Avg | vs avg |\n|---|---|---|---|\n"
            + rows
            + "\n",
        )
        check = validate_concise_report(broken)
        self.assertFalse(check.ok)
        self.assertTrue(any("over the %d-row cap" % CONCISE_MAX_TABLE_ROWS in v
                            for v in check.violations))

    def test_audit_record_headings_in_the_digest_warn(self):
        for banned in ("Watchlist developments", "Strongest candidates"):
            with self.subTest(banned=banned):
                variant = VALID_DIGEST.replace(
                    "## What changed", "## %s\n\nStuff.\n\n## What changed" % banned, 1
                )
                check = validate_concise_report(variant)
                self.assertTrue(
                    any("audit-record material" in w for w in check.warnings),
                    check.warnings,
                )

    def test_the_banned_headings_are_the_audit_records_own_sections(self):
        """The bulk we are pushing out is exactly the audit record's content."""
        banned = " ".join(BANNED_DIGEST_HEADINGS)
        for section in ("Watchlist developments", "Strongest candidates"):
            self.assertIn(section.lower(), banned)


class TextHelperTests(unittest.TestCase):
    def test_headings_ignores_code_fences(self):
        text = "# Real\n\n```\n# Not a heading\n```\n\n## Also real\n"
        self.assertEqual(headings(text), [(1, "Real"), (2, "Also real")])

    def test_section_body_stops_at_the_next_same_level_heading(self):
        text = "## One\n\nalpha\n\n### Nested\n\nbeta\n\n## Two\n\ngamma\n"
        body = section_body(text, "One")
        self.assertIn("alpha", body)
        self.assertIn("beta", body)
        self.assertNotIn("gamma", body)

    def test_section_body_of_a_missing_section_is_empty(self):
        self.assertEqual(section_body("## One\n\nalpha\n", "Nope"), "")

    def test_count_bullets_ignores_nested_items(self):
        text = "- one\n    - nested\n- two\n1. three\n"
        self.assertEqual(count_bullets(text), 3)

    def test_count_table_rows(self):
        self.assertEqual(count_table_rows("| a | b |\n|---|---|\n| 1 | 2 |\n"), 3)

    def test_detail_report_sections_are_unchanged(self):
        """The audit record keeps its old contract; only latest.md changed."""
        self.assertEqual(
            DETAIL_REPORT_SECTIONS,
            (
                "Portfolio & budget status",
                "Changes since last evaluation",
                "Watchlist developments",
                "Strongest candidates",
                "Decision",
                "Proposed allocation",
                "Confidence",
                "What would change this",
            ),
        )

    def test_missing_detail_sections_still_works(self):
        self.assertEqual(missing_detail_sections(""), list(DETAIL_REPORT_SECTIONS))


class DerivedDigestTests(unittest.TestCase):
    """The fallback for a run that wrote no digest, so latest.md is never stale."""

    DETAIL = """# Scheduled Evaluation — 2026-09-08 10:30

## Portfolio & budget status

Remaining monthly authorization: $25.00 of $25.00, day 8 of 30.

## Changes since last evaluation

The memory complex gapped up overnight and gave it back at the open. Nothing
else material moved and no holding reported earnings in the window.

## Watchlist developments

Membership unchanged at 29 / 5 / 90 / 22.

## Strongest candidates

### The five-way capital-use comparison

GOOGL `WATCH` on valuation. SNDK `TOO_EXTENDED`. BTC-USD `HOLD_NO_ADD` on
concentration and weakening adoption. LINK-USD `RESEARCH_INCOMPLETE`. WAIT wins.

## Decision

DECISION: WAIT

## Proposed allocation

None — WAIT. Remaining monthly authorization $25.00.

## Confidence

MEDIUM, and the components are unequal.

## What would change this

- SanDisk's own filing on the contracted floor.
- A current dated Bitcoin active-address reading.
"""

    def test_a_derived_digest_is_valid(self):
        derived = derive_digest(self.DETAIL, "reports/2026-09-08_1030.md")
        check = validate_concise_report(derived)
        self.assertTrue(check.ok, check.violations)
        self.assertEqual(check.decision, "WAIT")

    def test_it_says_it_is_mechanical_rather_than_pretending(self):
        derived = derive_digest(self.DETAIL, "reports/2026-09-08_1030.md")
        self.assertIn("Auto-derived digest", derived)
        self.assertIn("read the audit record", derived)

    def test_it_points_at_the_audit_record_it_came_from(self):
        derived = derive_digest(self.DETAIL, "reports/2026-09-08_1030.md")
        self.assertIn("reports/2026-09-08_1030.md", derived)
        self.assertIn("research/", derived)

    def test_it_carries_the_decision_forward(self):
        detail = self.DETAIL.replace("DECISION: WAIT", "DECISION: SINGLE_BUY")
        derived = derive_digest(detail, "reports/2026-09-08_1030.md")
        self.assertEqual(extract_decision(derived), "SINGLE_BUY")

    def test_it_restates_the_authorization_rather_than_inventing_one(self):
        derived = derive_digest(self.DETAIL, "reports/2026-09-08_1030.md")
        self.assertIn("$25.00", derived)

    def test_an_unparseable_audit_record_yields_an_invalid_digest_not_a_crash(self):
        """A run whose audit record is unusable must fail loudly, not quietly.

        The fallback still writes something readable and honest, but it does not
        manufacture an authorization figure to pass validation with.
        """
        derived = derive_digest("# Nothing useful here\n", "reports/2026-09-08_1030.md")
        self.assertIn("Auto-derived digest", derived)
        self.assertIn("NOT STATED IN THE AUDIT RECORD", derived)
        check = validate_concise_report(derived)
        self.assertFalse(check.ok)
        self.assertTrue(
            any("remaining monthly authorization" in v.lower() for v in check.violations)
        )


# --------------------------------------------------------------------------
# Per-candidate research notes
# --------------------------------------------------------------------------

VALID_NOTE = """# TEST — Test Corporation

- **symbol:** TEST
- **asset_class:** EQUITY
- **position:** NEW_POSITION
- **classification:** WATCH
- **last_reviewed:** 2026-09-07

## Snapshot

Mark $100.00 as of 2026-09-07 19:55 CDT. Held in no account. Prices go stale
immediately; re-read them rather than reusing this figure.

## Thesis

The standing case rests on unit growth rather than multiple expansion, and the
binding constraint is the entry price rather than the quality of the business.

## Bull case

Penetration is low and the installed base compounds recurring revenue at a
margin that has held through a full cycle.

## Bear case

The de-rating is correct and margins grind down from here, leaving a buyer at
today's multiple paying for a decade of flawless execution.

## Risks

Valuation risk, competitive entry, and customer concentration.

## What would change the classification

- Twenty percent lower, or two quarters of accelerating unit volume.

## Evidence

| Finding | Source | `source_type` | Date |
|---|---|---|---|
| Margin history | `get_financials` | `OFFICIAL_FINANCIALS` | 2026-09-07 |
"""


class SymbolTests(unittest.TestCase):
    def test_tickers_and_pairs_normalize(self):
        self.assertEqual(normalize_symbol("googl"), "GOOGL")
        self.assertEqual(normalize_symbol(" btc-usd "), "BTC-USD")
        self.assertEqual(normalize_symbol("MOG.A"), "MOG.A")

    def test_a_path_shaped_symbol_is_refused(self):
        """A note filename comes from a symbol, so this is a traversal guard."""
        for bad in ("../../etc/passwd", "a/b", "", "..", "x" * 40, "9NOPE"):
            with self.subTest(bad=bad):
                with self.assertRaises(ResearchNoteError):
                    normalize_symbol(bad)

    def test_the_filename_follows_the_symbol(self):
        self.assertEqual(note_filename("btc-usd"), "BTC-USD.md")


class NoteValidationTests(unittest.TestCase):
    def test_the_reference_note_is_valid(self):
        self.assertEqual(validate_note(VALID_NOTE, "research/TEST.md"), [])

    def test_the_header_parses(self):
        note = parse_note(VALID_NOTE, "research/TEST.md")
        self.assertEqual(note.symbol, "TEST")
        self.assertEqual(note.asset_class, "EQUITY")
        self.assertEqual(note.position, "NEW_POSITION")
        self.assertEqual(note.classification, "WATCH")
        self.assertEqual(note.last_reviewed.isoformat(), "2026-09-07")

    def test_a_missing_header_key_is_caught(self):
        for key in REQUIRED_NOTE_KEYS:
            with self.subTest(key=key):
                broken = VALID_NOTE.replace("- **%s:**" % key, "- **removed_%s:**" % key)
                problems = validate_note(broken, "research/TEST.md")
                self.assertTrue(any(key in p for p in problems), problems)

    def test_a_missing_section_is_caught(self):
        for section in REQUIRED_NOTE_SECTIONS:
            with self.subTest(section=section):
                broken = VALID_NOTE.replace("## %s" % section, "## Something else")
                problems = validate_note(broken, "research/TEST.md")
                self.assertTrue(any(section in p for p in problems), problems)

    def test_an_invalid_asset_class_is_caught(self):
        broken = VALID_NOTE.replace("**asset_class:** EQUITY", "**asset_class:** OPTION")
        problems = validate_note(broken, "research/TEST.md")
        self.assertTrue(any("asset_class" in p for p in problems))

    def test_an_invalid_position_is_caught(self):
        broken = VALID_NOTE.replace("NEW_POSITION", "MAYBE_POSITION")
        problems = validate_note(broken, "research/TEST.md")
        self.assertTrue(any("position" in p for p in problems))

    def test_an_unparseable_review_date_is_caught(self):
        broken = VALID_NOTE.replace("2026-09-07", "September 7th", 1)
        problems = validate_note(broken, "research/TEST.md")
        self.assertTrue(any("last_reviewed" in p for p in problems))

    def test_a_placeholder_note_is_caught(self):
        thin = "\n".join(
            ["# TEST — Test", "", "- **symbol:** TEST", "- **asset_class:** EQUITY",
             "- **position:** NEW_POSITION", "- **classification:** WATCH",
             "- **last_reviewed:** 2026-09-07", ""]
            + ["## %s\n\nTBD.\n" % s for s in REQUIRED_NOTE_SECTIONS]
        )
        problems = validate_note(thin, "research/TEST.md")
        self.assertTrue(any("placeholder, not research" in p for p in problems), problems)

    def test_a_misfiled_note_is_caught(self):
        problems = validate_note(VALID_NOTE, "research/WRONG.md")
        self.assertTrue(any("filed as" in p for p in problems), problems)


class StalenessTests(unittest.TestCase):
    def test_a_fresh_thesis_is_not_stale(self):
        note = parse_note(VALID_NOTE)
        self.assertFalse(note.is_stale(datetime(2026, 9, 10)))
        self.assertEqual(note.age_days(datetime(2026, 9, 10)), 3)

    def test_a_thesis_older_than_the_window_is_stale(self):
        note = parse_note(VALID_NOTE)
        self.assertTrue(
            note.is_stale(datetime(2026, 9, 7 + NOTE_STALE_AFTER_DAYS + 1))
        )

    def test_an_undated_note_counts_as_stale(self):
        broken = VALID_NOTE.replace("- **last_reviewed:** 2026-09-07\n", "")
        note = parse_note(broken)
        self.assertIsNone(note.last_reviewed)
        self.assertTrue(note.is_stale(datetime(2026, 9, 7)))

    def test_stale_symbols_reports_what_to_revisit(self):
        fresh = parse_note(VALID_NOTE)
        old = parse_note(VALID_NOTE.replace("2026-09-07", "2026-01-01", 1))
        old.symbol = "OLD"
        self.assertEqual(stale_symbols([fresh, old], datetime(2026, 9, 8)), ["OLD"])


class NoteDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def write(self, name, body):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            handle.write(body)

    def test_a_clean_directory_reports_no_problems(self):
        self.write("TEST.md", VALID_NOTE)
        self.assertEqual(research_notes.validate_directory(self.dir), {})

    def test_a_broken_note_is_reported_by_path(self):
        self.write("TEST.md", VALID_NOTE.replace("## Thesis", "## Musings"))
        problems = research_notes.validate_directory(self.dir)
        self.assertEqual(len(problems), 1)

    def test_the_index_is_excluded_from_validation(self):
        self.write("TEST.md", VALID_NOTE)
        self.write("INDEX.md", "# Research notes — index\n\nnot a note\n")
        self.assertEqual(research_notes.validate_directory(self.dir), {})

    def test_a_missing_directory_is_not_an_error(self):
        self.assertEqual(research_notes.list_note_paths("/nonexistent-xyz"), [])
        self.assertEqual(research_notes.validate_directory("/nonexistent-xyz"), {})

    def test_load_note_finds_a_note_by_symbol(self):
        self.write("TEST.md", VALID_NOTE)
        note = research_notes.load_note("test", self.dir)
        self.assertIsNotNone(note)
        self.assertEqual(note.symbol, "TEST")

    def test_load_note_returns_none_for_an_unknown_symbol(self):
        self.assertIsNone(research_notes.load_note("NOPE", self.dir))


class IndexTests(unittest.TestCase):
    def test_the_index_lists_every_note(self):
        notes = [parse_note(VALID_NOTE)]
        rendered = render_index(notes, datetime(2026, 9, 8))
        self.assertIn("[TEST](TEST.md)", rendered)
        self.assertIn("WATCH", rendered)

    def test_the_index_says_a_classification_is_not_authorization(self):
        rendered = render_index([parse_note(VALID_NOTE)], datetime(2026, 9, 8))
        self.assertIn("never** authorization", rendered)

    def test_the_index_says_prices_are_never_reused(self):
        rendered = render_index([parse_note(VALID_NOTE)], datetime(2026, 9, 8))
        self.assertIn("never reused", rendered)

    def test_rows_carry_the_staleness_verdict(self):
        rows = index_rows([parse_note(VALID_NOTE)], datetime(2026, 9, 8))
        self.assertEqual(rows[0]["symbol"], "TEST")
        self.assertFalse(rows[0]["stale"])
        self.assertEqual(rows[0]["age_days"], 1)


# --------------------------------------------------------------------------
# The repository as shipped
# --------------------------------------------------------------------------


class ShippedArtifactTests(unittest.TestCase):
    """What is actually on disk must satisfy the contract, not just the tests."""

    def read(self, rel):
        with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as handle:
            return handle.read()

    def test_the_shipped_latest_md_is_a_valid_digest(self):
        check = validate_concise_report(self.read("reports/latest.md"))
        self.assertTrue(check.ok, check.violations)

    def test_the_shipped_latest_md_is_within_the_enforced_bounds(self):
        """The 800-1,200 band is a target, not an invariant.

        The contract enforces the 1,500-word ceiling and warns outside the
        band, so a real run landing slightly over is a warning to act on, not
        a broken repository. Asserting the target as a hard rule made this test
        fail the first time a genuine run came in at 1,252 words.
        """
        check = validate_concise_report(self.read("reports/latest.md"))
        self.assertTrue(check.ok, check.violations)
        self.assertGreater(check.word_count, CONCISE_WORD_FLOOR)
        self.assertLessEqual(check.word_count, CONCISE_WORD_HARD_MAX)

    def test_a_shipped_digest_outside_the_target_band_is_flagged(self):
        """Being over target must still produce a visible signal."""
        check = validate_concise_report(self.read("reports/latest.md"))
        low, high = CONCISE_WORD_TARGET
        if not (low <= check.word_count <= high):
            self.assertTrue(
                any("target band" in w for w in check.warnings),
                "%d words is outside %d-%d but produced no warning"
                % (check.word_count, low, high),
            )

    def test_the_shipped_latest_md_is_not_a_copy_of_any_audit_record(self):
        digest = self.read("reports/latest.md").strip()
        reports_dir = os.path.join(REPO_ROOT, "reports")
        for name in sorted(os.listdir(reports_dir)):
            if name == "latest.md" or not name.endswith(".md"):
                continue
            with self.subTest(name=name):
                self.assertNotEqual(digest, self.read("reports/" + name).strip())

    def test_the_audit_records_are_still_full_detail(self):
        """The depth did not vanish; it moved."""
        detail = self.read("reports/2026-01-16_0930.md")
        self.assertEqual(missing_detail_sections(detail), [])
        self.assertGreater(word_count(detail), CONCISE_WORD_HARD_MAX)

    def test_every_shipped_research_note_is_valid(self):
        problems = research_notes.validate_directory()
        self.assertEqual(problems, {}, problems)

    def test_the_shipped_research_directory_is_not_empty(self):
        self.assertGreater(len(research_notes.list_note_paths()), 0)

    def test_the_shipped_research_index_matches_the_notes(self):
        notes = research_notes.load_all()
        index = self.read("research/INDEX.md")
        for note in notes:
            with self.subTest(symbol=note.symbol):
                self.assertIn("[%s](%s.md)" % (note.symbol, note.symbol), index)

    def test_the_digest_names_candidates_that_have_research_notes(self):
        """The digest's pointer to research/ has to actually lead somewhere."""
        digest = self.read("reports/latest.md")
        symbols = {n.symbol for n in research_notes.load_all()}
        named = [s for s in symbols if s in digest]
        self.assertGreaterEqual(len(named), 4, named)


if __name__ == "__main__":
    unittest.main()
