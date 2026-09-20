"""Report structure — one concise digest, one detailed audit record.

## Why there are two files

A scheduled run does a large amount of work: three accounts reconciled, twenty
equity lines and nine cryptoassets priced, four watchlists read, a five-way
capital-use competition, a research package per finalist, and the guardrail
arithmetic. All of that has to be *recorded*, because a decision nobody can
audit later is not much of a record.

None of it has to be *read every morning*. A 4,800-word file that is 90%
unchanged from yesterday does not get read at all, which is a worse outcome than
a shorter one that does.

So the run writes three tiers, and this module holds the contract for the first:

| Tier | File | Audience | Contract |
|---|---|---|---|
| Digest | ``reports/latest.md`` | a human, daily | this module — ~800-1,200 words, hard-capped |
| Audit record | ``reports/YYYY-MM-DD_HHMM.md`` | a human or auditor, later | :data:`DETAIL_REPORT_SECTIONS` |
| Research | ``research/<SYMBOL>.md`` | the *next* run | :mod:`src.research_notes` |

**Shortening the digest must never shorten the analysis**, and the digest is
never allowed to become the only copy of anything. Two things enforce that
rather than merely asking for it:

* every digest must point at the timestamped audit record and at ``research/``
  (:func:`validate_concise_report` rejects one that does not), so the deeper
  material is always reachable from the thing a human actually opens;
* ``src.scheduling.archive_inventory`` fingerprints the audit records and the
  research notes before and after every run, so a run that deletes or guts one
  is reported as a safety failure.

## What the digest must contain

Exactly the decision-relevant material, and deterministically checked for it:
the timestamp and safety/budget status, what materially changed, the compact
five-way capital-use comparison, the decision, the proposed allocation,
confidence, the three-to-five things being watched, and the specific conditions
that would change the answer. A digest that omits any of those is invalid — the
word budget is never a licence to drop the substance.

And what it must *not* contain: full holdings tables, full watchlists, repeated
policy explanations, whole research packages, or long rejection narratives.
Those belong in the audit record and the research notes. The word cap and the
table-row cap are what make that stick.

Pure functions over text. No I/O, no network, no subprocess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# The detailed audit record (unchanged contract, kept here with its sibling)
# --------------------------------------------------------------------------

DETAIL_REPORT_SECTIONS = (
    "Portfolio & budget status",
    "Changes since last evaluation",
    "Watchlist developments",
    "Strongest candidates",
    "Decision",
    "Proposed allocation",
    "Confidence",
    "What would change this",
)

# --------------------------------------------------------------------------
# The concise daily digest
# --------------------------------------------------------------------------

# Required headings, in the order a reader should meet them. Matched against
# actual markdown headings rather than anywhere in the text, so a phrase that
# happens to appear in a sentence cannot satisfy a section requirement.
CONCISE_REPORT_SECTIONS = (
    "Status",
    "What changed",
    "Five-way capital-use comparison",
    "Decision",
    "Proposed allocation",
    "Confidence",
    "Watching",
    "What would change this",
    "Detail and evidence",
)

# The target band the prompt asks for: roughly two pages.
CONCISE_WORD_TARGET = (800, 1200)

# Enforced bounds. The ceiling has headroom above the target for a five-leg
# SPLIT_BUY_PLAN, which legitimately needs more room than a WAIT. The floor is
# not a padding requirement -- it is the point below which the required content
# cannot honestly be present, and the section checks will usually fire first.
CONCISE_WORD_HARD_MAX = 1500
CONCISE_WORD_FLOOR = 250

# Full holdings and watchlist tables are the main thing that turned the daily
# report into a 4,800-word document. A digest's own tables (status, five-way,
# allocation) come to well under this; a 20-line holdings table plus a 9-line
# crypto table does not.
CONCISE_MAX_TABLE_ROWS = 40

# The five uses of the month's capital, as the digest must name them. Searched
# within the five-way section only.
FIVE_WAY_BUCKET_TOKENS = (
    ("existing equity", ("existing equity",)),
    ("new equity", ("new equity",)),
    ("existing crypto", ("existing crypto",)),
    ("new crypto", ("new crypto",)),
    ("WAIT", ("wait",)),
)

# The three-to-five things being watched. A list of twelve is a research
# backlog, not a summary; a list of one is not a summary either.
WATCHING_ITEM_RANGE = (3, 5)

_DECISION_RE = re.compile(
    r"^\s*DECISION:\s*(WAIT|SINGLE_BUY|SPLIT_BUY_PLAN)\s*$", re.M | re.I
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\S")
_TABLE_ROW_RE = re.compile(r"^\s*\|")
_MONEY_RE = re.compile(r"\$\s?-?[\d,]+(?:\.\d{2})?")
_REMAINING_RE = re.compile(
    r"remaining[^\n$]{0,80}\$\s?[\d,]+(?:\.\d{2})?", re.I
)
_DETAIL_POINTER_RE = re.compile(r"reports/\d{4}-\d{2}-\d{2}_\d{4}\.md")
_RESEARCH_POINTER_RE = re.compile(r"research/")
_CONFIDENCE_RE = re.compile(r"\b(LOW|MEDIUM|HIGH)\b")

# Headings that signal the bulk material the digest is meant to leave behind.
BANNED_DIGEST_HEADINGS = (
    "portfolio & budget status",
    "household book",
    "watchlist developments",
    "strongest candidates",
    "research package",
    "full holdings",
    "all positions",
)


@dataclass
class ReportCheck:
    """Whether a report satisfies its structural contract."""

    ok: bool = True
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks: List[str] = field(default_factory=list)
    word_count: int = 0
    table_rows: int = 0
    decision: Optional[str] = None
    sections: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": list(self.violations),
            "warnings": list(self.warnings),
            "checks": list(self.checks),
            "word_count": self.word_count,
            "table_rows": self.table_rows,
            "decision": self.decision,
            "sections": list(self.sections),
        }


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------


def word_count(text: str) -> int:
    """Whitespace-separated tokens, markdown and all.

    Deliberately crude: it counts what a reader has to move their eyes over,
    including table cells and headings, which is the thing being budgeted.
    """
    return len((text or "").split())


def headings(text: str) -> List[Tuple[int, str]]:
    """``(level, title)`` for every markdown heading outside a code fence."""
    out: List[Tuple[int, str]] = []
    in_fence = False
    for line in (text or "").split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if match:
            out.append((len(match.group(1)), match.group(2).strip()))
    return out


def section_body(text: str, title: str) -> str:
    """The text under the heading whose title contains ``title``.

    Stops at the next heading of the same or a shallower level, so a section
    keeps its own subsections and nothing else.
    """
    lines = (text or "").split("\n")
    needle = title.strip().lower()
    start = None
    start_level = 0
    in_fence = False

    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING_RE.match(line)
        if not match:
            continue
        level, heading_title = len(match.group(1)), match.group(2).strip().lower()
        if start is None:
            if needle in heading_title:
                start, start_level = index + 1, level
            continue
        if level <= start_level:
            return "\n".join(lines[start:index])

    if start is None:
        return ""
    return "\n".join(lines[start:])


def count_bullets(text: str) -> int:
    """Top-level list items, ignoring nested continuation lines."""
    total = 0
    in_fence = False
    for line in (text or "").split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _BULLET_RE.match(line) and len(line) - len(line.lstrip()) < 2:
            total += 1
    return total


def count_table_rows(text: str) -> int:
    """Markdown table rows, separator rows included."""
    return sum(1 for line in (text or "").split("\n") if _TABLE_ROW_RE.match(line))


def extract_decision(text: str) -> Optional[str]:
    """The plan type a report declares, or None when it declares none."""
    match = _DECISION_RE.search(text or "")
    return match.group(1).upper() if match else None


def missing_detail_sections(text: str) -> List[str]:
    """Required sections the detailed audit record failed to include."""
    blob = (text or "").lower()
    return [s for s in DETAIL_REPORT_SECTIONS if s.lower() not in blob]


def missing_concise_sections(text: str) -> List[str]:
    """Required digest sections that are not present as headings."""
    present = [title.lower() for _, title in headings(text)]
    missing = []
    for required in CONCISE_REPORT_SECTIONS:
        needle = required.lower()
        if not any(needle in title for title in present):
            missing.append(required)
    return missing


# --------------------------------------------------------------------------
# The digest contract
# --------------------------------------------------------------------------


def validate_concise_report(text: str) -> ReportCheck:
    """Check ``reports/latest.md`` against the digest contract.

    Every required element is checked independently of the word budget, so
    "it had to be short" can never explain away a missing decision, a missing
    authorization figure, an incomplete five-way comparison, an absent
    allocation, an unstated confidence, or missing change triggers.
    """
    check = ReportCheck()
    text = text or ""
    check.word_count = word_count(text)
    check.table_rows = count_table_rows(text)
    check.sections = [title for _, title in headings(text)]

    # --- 1. structure ----------------------------------------------------
    check.checks.append("required_sections_present")
    missing = missing_concise_sections(text)
    if missing:
        check.violations.append(
            "the digest is missing required sections: %s. A short report may drop "
            "detail; it may never drop a required element." % ", ".join(missing)
        )

    # --- 2. the decision -------------------------------------------------
    check.checks.append("decision_declared")
    check.decision = extract_decision(text)
    check_action_banner(text, check.decision, check)
    if check.decision is None:
        check.violations.append(
            "the digest does not declare a decision; expected a line reading exactly "
            "'DECISION: WAIT', 'DECISION: SINGLE_BUY' or 'DECISION: SPLIT_BUY_PLAN'"
        )

    # --- 3. safety and budget status -------------------------------------
    check.checks.append("safety_and_budget_status_present")
    status = section_body(text, "Status")
    status_blob = status.lower()
    if not _REMAINING_RE.search(status):
        check.violations.append(
            "the Status section does not state the remaining monthly authorization "
            "as a dollar figure"
        )
    for switch in ("dry_run", "agent_enabled", "live_trading"):
        if switch not in status_blob:
            check.violations.append(
                "the Status section does not record %s; the three execution switches "
                "are part of the daily status, not boilerplate to be trimmed" % switch
            )
            break

    # --- 4. the five-way comparison --------------------------------------
    check.checks.append("five_way_comparison_complete")
    # Collapse whitespace first: "existing\ncrypto" is the same phrase as
    # "existing crypto", and a line wrap must not read as a missing bucket.
    five_way = " ".join(
        section_body(text, "Five-way capital-use comparison").lower().split()
    )
    if not five_way.strip():
        check.violations.append(
            "the five-way capital-use comparison section is empty"
        )
    else:
        for label, tokens in FIVE_WAY_BUCKET_TOKENS:
            if not any(token in five_way for token in tokens):
                check.violations.append(
                    "the five-way comparison does not name the %r use of the "
                    "capital; all five must appear, including the ones rejected"
                    % label
                )

    # --- 5. proposed allocation ------------------------------------------
    check.checks.append("proposed_allocation_stated")
    allocation = section_body(text, "Proposed allocation")
    if not allocation.strip():
        check.violations.append("the Proposed allocation section is empty")
    elif not (_MONEY_RE.search(allocation) or "none" in allocation.lower()):
        check.violations.append(
            "the Proposed allocation section states neither a dollar amount nor "
            "'None'; for a WAIT write 'None — WAIT' and the remaining authorization"
        )

    # --- 6. confidence ---------------------------------------------------
    check.checks.append("confidence_stated")
    confidence = section_body(text, "Confidence")
    if not _CONFIDENCE_RE.search(confidence):
        check.violations.append(
            "the Confidence section does not state LOW, MEDIUM or HIGH"
        )

    # --- 7. what is being watched ----------------------------------------
    check.checks.append("watching_list_present")
    watching = count_bullets(section_body(text, "Watching"))
    low, high = WATCHING_ITEM_RANGE
    if watching < low:
        check.violations.append(
            "the Watching section lists %d item(s); name the %d-%d most important "
            "things being watched" % (watching, low, high)
        )
    elif watching > high:
        check.warnings.append(
            "the Watching section lists %d items against a %d-%d target; a longer "
            "list belongs in the audit record as a research backlog"
            % (watching, low, high)
        )

    # --- 8. change triggers ----------------------------------------------
    check.checks.append("change_triggers_present")
    triggers_body = section_body(text, "What would change this")
    # Conditions may be bulleted or tabulated -- a dated-event table is a list
    # of specific conditions. Discount two rows for a table's header and rule.
    tabulated = count_table_rows(triggers_body)
    triggers = count_bullets(triggers_body) + (max(0, tabulated - 2) if tabulated else 0)
    if not triggers_body.strip():
        check.violations.append(
            "the 'What would change this' section is empty; a decision with no "
            "stated change conditions cannot be revisited"
        )
    elif triggers == 0:
        check.violations.append(
            "the 'What would change this' section lists no specific conditions"
        )
    elif triggers < 2:
        check.warnings.append(
            "only one change condition is listed; most decisions have more than one"
        )

    # --- 9. the deeper record must stay reachable ------------------------
    check.checks.append("detail_and_research_are_reachable")
    if not _DETAIL_POINTER_RE.search(text):
        check.violations.append(
            "the digest does not point at its timestamped audit record "
            "(reports/YYYY-MM-DD_HHMM.md). The digest must never be the only copy "
            "of the run's evidence."
        )
    if not _RESEARCH_POINTER_RE.search(text):
        check.violations.append(
            "the digest does not point at research/; per-candidate research must "
            "stay retrievable by the next run without re-deriving it"
        )

    # --- 10. the budget --------------------------------------------------
    check.checks.append("within_word_budget")
    if check.word_count > CONCISE_WORD_HARD_MAX:
        check.violations.append(
            "the digest is %d words, over the %d-word ceiling. Move holdings tables, "
            "watchlists, research packages and rejection narratives to the audit "
            "record and the research notes — do not cut required content."
            % (check.word_count, CONCISE_WORD_HARD_MAX)
        )
    elif check.word_count > CONCISE_WORD_TARGET[1]:
        check.warnings.append(
            "the digest is %d words, above the %d-%d target band"
            % (check.word_count, CONCISE_WORD_TARGET[0], CONCISE_WORD_TARGET[1])
        )
    if check.word_count < CONCISE_WORD_FLOOR:
        check.violations.append(
            "the digest is %d words, below the %d-word floor; the required content "
            "does not fit in less" % (check.word_count, CONCISE_WORD_FLOOR)
        )
    elif check.word_count < CONCISE_WORD_TARGET[0]:
        check.warnings.append(
            "the digest is %d words, below the %d-%d target band"
            % (check.word_count, CONCISE_WORD_TARGET[0], CONCISE_WORD_TARGET[1])
        )

    check.checks.append("bulk_material_left_to_the_audit_record")
    if check.table_rows > CONCISE_MAX_TABLE_ROWS:
        check.violations.append(
            "the digest contains %d table rows, over the %d-row cap. Full holdings "
            "and watchlist tables belong in the audit record."
            % (check.table_rows, CONCISE_MAX_TABLE_ROWS)
        )
    present = [title.lower() for _, title in headings(text)]
    for banned in BANNED_DIGEST_HEADINGS:
        if any(banned in title for title in present):
            check.warnings.append(
                "the digest carries a %r section, which reads like audit-record "
                "material rather than a daily summary" % banned
            )

    check.ok = not check.violations
    return check


# --------------------------------------------------------------------------
# The ACTION banner
# --------------------------------------------------------------------------
#
# The point of the digest is that a human can learn whether there is something
# to buy without reading anything. That means the answer has to be the first
# thing on the page, in a fixed shape, every day:
#
#     ACTION: BUY $10.00 SNDK + $5.00 BTC-USD
#     ACTION: NONE — WAIT
#
# followed immediately by the four things a reader needs in order to decide
# whether to act at all: what is left of the month's authorization, how
# confident the run is, why in one sentence, and whether a human still has to
# approve. That last line is not decoration — a recommendation is never
# permission, and the banner is the most likely thing to be read in isolation,
# so it is the most important place to say so.
#
# WAIT gets a banner too. "Nothing today" is exactly the answer the reader came
# for, and making them infer it from the absence of a banner defeats the point.

ACTION_BUY = "BUY"
ACTION_NONE = "NONE"

#: The banner must appear before any section heading — i.e. at the very top.
ACTION_BANNER_MAX_LINE = 24

#: What must follow the ACTION line, matched case-insensitively.
ACTION_BANNER_FIELDS = (
    ("remaining", ("remaining monthly authorization", "remaining authorization")),
    ("confidence", ("confidence",)),
    ("rationale", ("why", "rationale")),
    ("approval", ("human approval", "approval required")),
)

#: Language that would misstate the standing of a recommendation.
# Affirmative claims only. The banner's own correct language — "nothing here is
# approved or submitted" — must pass, so a bare \bsubmitted\b is wrong: it flags
# the very sentence that states the truth.
ACTION_BANNER_FORBIDDEN = (
    (r"\bhuman\s+approval\s+required\s*:?\s*\**\s*\b(?:no|none)\b",
     "says human approval is not required"),
    (r"\bno\s+(?:human\s+)?approval\s+(?:is\s+)?(?:needed|required)\b",
     "says no approval is needed"),
    (r"\bapproval\s+(?:is\s+)?not\s+(?:needed|required)\b",
     "says approval is not required"),
    (r"\bpre[-\s]?approved\b", "claims pre-approval"),
    (r"\balready\s+approved\b", "claims prior approval"),
    (r"\b(?:was|were|has\s+been|have\s+been|i|we)\s+submitted\b",
     "claims an order was submitted"),
    (r"\bsubmitted\s+(?:an?\s+|the\s+)?order\b",
     "claims an order was submitted"),
    (r"\bexecuted\b", "claims the purchase was executed"),
)

_ACTION_LINE_RE = re.compile(
    r"^[ \t>]*\**\s*ACTION:\s*(?P<body>[^\n]*?)\s*\**[ \t]*$", re.M)
_ACTION_LEG_RE = re.compile(
    r"\$\s?(?P<amount>[\d,]+(?:\.\d{1,2})?)\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,14})")
_FORBIDDEN_BANNER_RES = [
    (re.compile(pattern, re.I), why) for pattern, why in ACTION_BANNER_FORBIDDEN
]

# A claim and its denial are built from the same verbs. "The order was
# submitted" is a lie; "no order has been submitted" is the truth the banner is
# required to tell. What separates them is a negation earlier in the same
# clause, so the scan looks for one before flagging.
_NEGATION_RE = re.compile(
    r"\b(?:no|not|never|nothing|none|neither|nor|without|cannot|can't|won't|"
    r"isn't|aren't|wasn't|weren't|hasn't|haven't|doesn't|didn't)\b", re.I)
_CLAUSE_BOUNDARY_RE = re.compile(r"[.;:,\u2013\u2014\n]")


def _claim_is_denied(text: str, start: int) -> bool:
    """True when a negation governs the phrase beginning at ``start``.

    Scoped to the current clause: a negation two sentences earlier says nothing
    about this claim, and the forbidden phrases that carry their own negation
    ("no approval is needed") begin *at* the negation, so their preceding text
    is unaffected.
    """
    boundary = 0
    for match in _CLAUSE_BOUNDARY_RE.finditer(text, 0, start):
        boundary = match.end()
    return _NEGATION_RE.search(text, boundary, start) is not None


@dataclass
class ActionBanner:
    """The one-glance answer at the top of the digest."""

    kind: str = ""
    legs: List[Tuple[str, str]] = field(default_factory=list)
    line: str = ""
    line_number: int = 0
    body: str = ""

    @property
    def total(self) -> Optional[Decimal]:
        if not self.legs:
            return None
        total = Decimal("0")
        for amount, _ in self.legs:
            try:
                total += Decimal(amount.replace(",", ""))
            except InvalidOperation:
                return None
        return total

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "legs": [{"amount": a, "symbol": s} for a, s in self.legs],
            "total": str(self.total) if self.total is not None else None,
            "line": self.line,
            "line_number": self.line_number,
        }


def parse_action_banner(text: str) -> Optional[ActionBanner]:
    """The ACTION banner, or None when the digest carries no ACTION line."""
    match = _ACTION_LINE_RE.search(text or "")
    if match is None:
        return None
    body = (match.group("body") or "").strip()
    line_number = (text or "")[:match.start()].count("\n") + 1

    banner = ActionBanner(
        line=match.group(0).strip(), line_number=line_number, body=body)
    upper = body.upper()
    if upper.startswith(ACTION_NONE) or "WAIT" in upper:
        banner.kind = ACTION_NONE
        return banner

    legs = [(m.group("amount"), m.group("symbol"))
            for m in _ACTION_LEG_RE.finditer(body)]
    if legs and "BUY" in upper:
        banner.kind = ACTION_BUY
        banner.legs = legs
    return banner


def banner_block(text: str, banner: "ActionBanner", limit: int = 14) -> str:
    """The contiguous banner block: the ACTION line and the fields under it.

    Ends at the first section heading, or at the first blank line once fields
    have been collected. A leading blank line is tolerated so the banner may be
    written as a blockquote or as plain bold text plus bullets.
    """
    lines = (text or "").split("\n")
    start = max(0, banner.line_number - 1)
    collected: List[str] = []
    seen_field = False
    for line in lines[start:start + limit]:
        stripped = line.strip()
        if collected and stripped.startswith("#"):
            break
        if not stripped:
            if seen_field:
                break
            collected.append(line)
            continue
        collected.append(line)
        if collected and stripped not in (">", ">>"):
            seen_field = len(collected) > 1 or seen_field
    return "\n".join(collected)


def render_action_banner(
    decision: str,
    legs: Sequence[Tuple[str, str]],
    remaining_before: str,
    remaining_after: str,
    confidence: str,
    rationale: str,
) -> str:
    """The banner block, for the prompt's reference and the derived digest."""
    if decision == "WAIT" or not legs:
        headline = "ACTION: NONE — WAIT"
    else:
        headline = "ACTION: BUY " + " + ".join(
            "$%s %s" % (amount, symbol) for amount, symbol in legs)
    waiting = decision == "WAIT" or not legs
    if waiting:
        authorization = "> - **Remaining monthly authorization:** $%s" % remaining_before
        approval = (
            "> - **Human approval required:** Not applicable — no purchase is "
            "proposed, so there is nothing to approve."
        )
    else:
        authorization = (
            "> - **Remaining monthly authorization:** $%s → $%s if this is "
            "approved and filled" % (remaining_before, remaining_after)
        )
        approval = (
            "> - **Human approval required:** YES — nothing here is approved or "
            "submitted. Promote it with `python3 "
            "scripts/promote_latest_recommendation.py`, then approve each leg "
            "separately."
        )
    return "\n".join([
        "> **%s**" % headline,
        ">",
        authorization,
        "> - **Confidence:** %s" % confidence,
        "> - **Why:** %s" % rationale,
        approval,
    ])


def check_action_banner(text: str, decision: Optional[str], check: "ReportCheck") -> None:
    """Validate the banner against the decision the digest declares."""
    check.checks.append("action_banner_present_and_agrees_with_the_decision")
    banner = parse_action_banner(text)

    if banner is None:
        check.violations.append(
            "the digest carries no ACTION line. The first thing on the page must "
            "answer 'is there something to buy?' — 'ACTION: BUY $10.00 SNDK' or "
            "'ACTION: NONE — WAIT' — so the answer can be read without reading "
            "the report."
        )
        return

    if not banner.kind:
        check.violations.append(
            "the ACTION line %r is not readable as either a BUY or NONE. Use "
            "'ACTION: BUY $<amount> <SYMBOL>' (joined with ' + ' for a plan) or "
            "'ACTION: NONE — WAIT'." % banner.body[:60]
        )
        return

    if banner.line_number > ACTION_BANNER_MAX_LINE:
        check.violations.append(
            "the ACTION line is on line %d; it must be within the first %d lines, "
            "above every section heading. A banner further down is not a banner."
            % (banner.line_number, ACTION_BANNER_MAX_LINE)
        )

    # --- the banner and the decision must say the same thing -------------
    if decision == "WAIT" and banner.kind != ACTION_NONE:
        check.violations.append(
            "the digest declares DECISION: WAIT but the ACTION line announces a "
            "purchase. The banner and the decision must agree."
        )
    elif decision in ("SINGLE_BUY", "SPLIT_BUY_PLAN") and banner.kind != ACTION_BUY:
        check.violations.append(
            "the digest declares DECISION: %s but the ACTION line announces no "
            "purchase. The banner and the decision must agree." % decision
        )
    elif decision == "SINGLE_BUY" and len(banner.legs) != 1:
        check.violations.append(
            "DECISION: SINGLE_BUY but the ACTION line names %d legs"
            % len(banner.legs)
        )
    elif decision == "SPLIT_BUY_PLAN" and not (2 <= len(banner.legs) <= 5):
        check.violations.append(
            "DECISION: SPLIT_BUY_PLAN but the ACTION line names %d leg(s); a plan "
            "has 2 to 5" % len(banner.legs)
        )

    # --- the four things that must follow it -----------------------------
    # Scoped to the banner block itself, not to a window of leading lines. The
    # user asked for these "immediately after" the ACTION line, and a wider
    # window both weakens that and picks up the report's own correct language —
    # the Status section legitimately says "nothing was approved, enabled, or
    # submitted", which a loose scan reads as the banner overstating itself.
    header = banner_block(text, banner).lower()
    for label, tokens in ACTION_BANNER_FIELDS:
        if not any(token in header for token in tokens):
            check.violations.append(
                "the ACTION banner does not state the %s. It must be followed "
                "immediately by the remaining monthly authorization, the "
                "confidence, a one-sentence rationale, and whether human "
                "approval is still required." % label
            )

    # --- and the standing of a recommendation is never overstated --------
    check.checks.append("banner_does_not_overstate_its_standing")
    for pattern, why in _FORBIDDEN_BANNER_RES:
        found = next(
            (m for m in pattern.finditer(header)
             if not _claim_is_denied(header, m.start())), None)
        if found:
            check.violations.append(
                "the ACTION banner %s (matched %r). A scheduled recommendation is "
                "never approved and never submitted; every leg still needs its "
                "own separate human approval." % (why, found.group(0)[:40])
            )
            break


# --------------------------------------------------------------------------
# The weekly discovery report
# --------------------------------------------------------------------------
#
# A weekly broad-market discovery pass widens the candidate universe. It has
# LESS authority than the weekday evaluation, not more: it may add research
# candidates and it may not propose, approve, or submit anything. The monthly
# $25 decision stays entirely inside the weekday evaluation.
#
# That is enforced here rather than asked for: a discovery report containing a
# DECISION: line, a proposed allocation, or approval language is rejected.

DISCOVERY_REPORT_SECTIONS = (
    "Scope",
    "New candidates",
    "Screened out",
    "Research queued",
    "No purchase proposed",
)

# A discovery pass reads thousands of instruments; its report still has to be
# readable. Generous relative to the daily digest, hard-capped well below an
# audit record.
DISCOVERY_WORD_TARGET = (600, 1400)
DISCOVERY_WORD_HARD_MAX = 2000

# Language that would turn a discovery pass into an allocation decision.
FORBIDDEN_DISCOVERY_PATTERNS = (
    (r"^\s*DECISION:", "a DECISION: line — discovery declares no decision"),
    (r"\bDECISION:\s*(?:WAIT|SINGLE_BUY|SPLIT_BUY_PLAN)\b",
     "a decision verdict — discovery declares none"),
    (r"\bSPLIT_BUY_PLAN\b|\bSINGLE_BUY\b", "a buy-plan type"),
    (r"\b(?:propos|recommend)\w*\s+(?:a\s+)?(?:purchase|buy|allocation|leg)\b",
     "a proposed purchase"),
    (r"\ballocat\w*\s+\$\s?[\d,]", "a dollar allocation"),
    (r"\b(?:approve|approved|approval)\b", "approval language"),
    (r"\bsubmit(?:ted|ting)?\s+(?:an?\s+)?order\b", "order submission"),
    (r"\bdecision_id\b|\bplan_id\b", "a decision or plan identifier"),
)
_FORBIDDEN_DISCOVERY_RES = [
    (re.compile(pattern, re.I | re.M), why)
    for pattern, why in FORBIDDEN_DISCOVERY_PATTERNS
]

# The statement a discovery report must carry, in substance.
DISCOVERY_DISCLAIMER_TOKENS = ("no purchase", "not a recommendation")


def validate_discovery_report(text: str) -> ReportCheck:
    """Check a weekly discovery report against its contract.

    Two jobs: make sure the pass recorded what it looked at, and make sure it
    did not quietly become an investment decision.
    """
    check = ReportCheck()
    text = text or ""
    check.word_count = word_count(text)
    check.table_rows = count_table_rows(text)
    check.sections = [title for _, title in headings(text)]

    check.checks.append("required_sections_present")
    present = [title.lower() for _, title in headings(text)]
    missing = [
        required for required in DISCOVERY_REPORT_SECTIONS
        if not any(required.lower() in title for title in present)
    ]
    if missing:
        check.violations.append(
            "the discovery report is missing required sections: %s"
            % ", ".join(missing)
        )

    # --- the load-bearing check: discovery proposes nothing ---
    check.checks.append("proposes_no_purchase")
    for pattern, why in _FORBIDDEN_DISCOVERY_RES:
        match = pattern.search(text)
        if match:
            check.violations.append(
                "a weekly discovery report must not contain %s (matched %r). "
                "Discovery widens the candidate universe; the monthly "
                "authorization is decided only in the weekday evaluation."
                % (why, match.group(0)[:60])
            )

    check.checks.append("no_decision_declared")
    check.decision = extract_decision(text)
    if check.decision is not None:
        check.violations.append(
            "the discovery report declares DECISION: %s. A discovery pass "
            "declares no decision at all." % check.decision
        )

    check.checks.append("disclaimer_present")
    # Checked against the section BODY, not the whole document: the heading
    # "No purchase proposed" would otherwise satisfy its own requirement, and
    # a heading is not a statement.
    disclaimer = section_body(text, "No purchase proposed").lower()
    if not any(token in disclaimer for token in DISCOVERY_DISCLAIMER_TOKENS):
        check.violations.append(
            "the 'No purchase proposed' section must say plainly, in its body, "
            "that no purchase is proposed and that nothing in the report is a "
            "recommendation. The heading alone does not say it."
        )

    check.checks.append("scope_is_dated")
    scope = section_body(text, "Scope")
    if not re.search(r"\d{4}-\d{2}-\d{2}", scope):
        check.violations.append(
            "the Scope section must date what was read; an undated universe "
            "sweep cannot be audited or reused"
        )

    check.checks.append("candidates_are_named")
    candidates = section_body(text, "New candidates")
    if not candidates.strip():
        check.violations.append("the New candidates section is empty")
    elif count_bullets(candidates) + count_table_rows(candidates) == 0:
        check.violations.append(
            "the New candidates section names nothing; list the symbols that "
            "entered the universe, or say explicitly that none did"
        )

    check.checks.append("within_word_budget")
    if check.word_count > DISCOVERY_WORD_HARD_MAX:
        check.violations.append(
            "the discovery report is %d words, over the %d-word ceiling"
            % (check.word_count, DISCOVERY_WORD_HARD_MAX)
        )
    elif check.word_count > DISCOVERY_WORD_TARGET[1]:
        check.warnings.append(
            "the discovery report is %d words, above the %d-%d target band"
            % (check.word_count, DISCOVERY_WORD_TARGET[0], DISCOVERY_WORD_TARGET[1])
        )

    check.ok = not check.violations
    return check


# --------------------------------------------------------------------------
# A deterministic fallback digest
# --------------------------------------------------------------------------

_DERIVED_NOTICE = (
    "> **Auto-derived digest.** The run did not write a valid `reports/latest.md`, "
    "so this was extracted mechanically from the audit record below. It is a "
    "pointer, not a summary: read the audit record for anything that matters."
)


def find_remaining_authorization(text: str) -> Optional[str]:
    """The phrase in which a report states its remaining authorization.

    Used by :func:`derive_digest` to *restate* a figure the audit record already
    contains. It never supplies one of its own: an unfindable authorization is
    reported as a problem, not filled in with a guess.
    """
    match = _REMAINING_RE.search(text or "")
    return match.group(0) if match else None


def _trim(body: str, max_words: int) -> str:
    """Keep whole lines up to a word budget."""
    kept: List[str] = []
    used = 0
    for line in body.strip().split("\n"):
        cost = len(line.split())
        if used + cost > max_words and kept:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept).strip()


def derive_digest(detail_text: str, detail_rel: str, timestamp: str = "") -> str:
    """Build a digest from a detailed report, for when the run wrote none.

    This exists so ``reports/latest.md`` is never silently yesterday's file. It
    is honest about being mechanical, and it is held to the same contract as a
    written digest — a run that hits this path still gets a valid, if blunt,
    daily summary, and the audit record stays authoritative.
    """
    decision = extract_decision(detail_text) or "WAIT"
    status = _trim(section_body(detail_text, "Portfolio & budget status"), 150)
    changed = _trim(section_body(detail_text, "Changes since last evaluation"), 220)
    five_way = _trim(section_body(detail_text, "five-way capital-use comparison"), 220)
    allocation = _trim(section_body(detail_text, "Proposed allocation"), 90)
    confidence = _trim(section_body(detail_text, "Confidence"), 90)
    triggers = _trim(section_body(detail_text, "What would change this"), 160)

    if not five_way.strip():
        five_way = _trim(section_body(detail_text, "Strongest candidates"), 220)

    remaining = find_remaining_authorization(detail_text)
    remaining_match = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", remaining or "")
    remaining_figure = remaining_match.group(1) if remaining_match else ""

    # The contract requires all five buckets and the three switches by name; a
    # mechanical extract cannot guarantee the prose carries them, so they are
    # stated explicitly here rather than hoped for.
    banner = render_action_banner(
        decision,
        [],  # a mechanical extract never announces a purchase it cannot verify
        remaining_figure or "0.00",
        remaining_figure or "0.00",
        "LOW",
        "Auto-derived from the audit record; read the audit record before acting.",
    )
    if decision != "WAIT":
        banner = banner.replace(
            "ACTION: NONE — WAIT",
            "ACTION: REVIEW REQUIRED — the audit record declares %s" % decision,
        )

    return "\n".join(
        [
            "# Scheduled Evaluation — Digest%s" % (" — " + timestamp if timestamp else ""),
            "",
            banner,
            "",
            _DERIVED_NOTICE,
            "",
            "## Status",
            "",
            "Execution disabled: `execution_mode=DRY_RUN`, `agent_enabled=false`,",
            "`live_trading=false`. Nothing was approved, enabled, or submitted.",
            "",
            # Restated from the audit record, never supplied. When the audit
            # record does not state it, this line says so and the digest fails
            # validation -- which is the correct outcome, not a reason to guess.
            "Authorization, as stated by the audit record: **%s**."
            % (remaining or "NOT STATED IN THE AUDIT RECORD — read it directly"),
            "",
            status or "No budget section was extractable from the audit record.",
            "",
            "## What changed",
            "",
            changed or "Not extractable from the audit record.",
            "",
            "## Five-way capital-use comparison",
            "",
            "All five uses of the month's capital were compared in the audit record:",
            "adding to an existing equity/ETF position, opening a new equity/ETF",
            "position, adding to an existing crypto position, opening a new crypto",
            "position, and WAIT. This extract is mechanical and may not carry the",
            "reasoning for each; read the audit record for the comparison itself.",
            "",
            five_way or "Not extractable from the audit record.",
            "",
            "## Decision",
            "",
            "DECISION: %s" % decision,
            "",
            "## Proposed allocation",
            "",
            allocation or "None — see the audit record.",
            "",
            "## Confidence",
            "",
            confidence or "Not stated in the audit record; treat as LOW.",
            "",
            "## Watching",
            "",
            "- This digest was derived mechanically, so the run's own watch list was",
            "  not summarised here; it is in the audit record.",
            "- The audit record's *What would change this* section is reproduced below,",
            "  and is the reliable statement of what the run was watching for.",
            "- Standing per-candidate research is under `research/`, one note per",
            "  candidate, and carries the thesis the next run should start from.",
            "",
            "## What would change this",
            "",
            triggers or "- See the audit record.",
            "",
            "## Detail and evidence",
            "",
            "- Audit record: `%s`" % detail_rel,
            "- Per-candidate research: `research/`",
            "- Machine state: `state/last_evaluation.json`, `logs/decisions.jsonl`",
            "",
            "*(Report only. Nothing was approved, enabled, or submitted.)*",
            "",
        ]
    )
