"""Lessons — learning from past decisions without learning the wrong thing.

## The central distinction

**A profitable trade is not automatically a good decision, and a losing trade
is not automatically a bad one.** Outcome and process are separate axes, and
only one of them is a decision:

|                    | Good outcome                      | Bad outcome                |
|--------------------|-----------------------------------|----------------------------|
| **Sound process**  | ``CONFIRMED`` — reinforce         | ``ACCEPTED_RISK`` — do not learn against |
| **Unsound process**| ``LUCK`` — the dangerous quadrant | ``CORRECTABLE`` — the only true lesson |

``ACCEPTED_RISK`` is the quadrant a naive system destroys: a well-reasoned
purchase that lost money is not a mistake, and "training" against it teaches
risk aversion rather than judgement. ``LUCK`` is the quadrant a naive system
never notices: an unsound decision that happened to pay is the one most likely
to be repeated.

## Two record types, and why the distinction is structural

* A :data:`LESSON` may rest **only on what was knowable when the decision was
  made**. It can be cited in later reasoning.
* An :data:`OUTCOME_OBSERVATION` needs information that did not exist yet —
  what the price did afterwards. It is recorded, and it may **never** be cited
  as a reason.

:func:`validate_lesson` enforces that separation, so "it went up, therefore it
was a good decision" cannot be written down as a lesson at all. That is the
anti-hindsight guard, and it is a type rule rather than an instruction.

## Opportunity cost without hindsight

Comparing a past purchase against whatever turned out to be the best performer
is hindsight bias with arithmetic attached. A comparison is admissible only
when the comparator was **demonstrably available in the decision set at the
time** — meaning this repository's own decision log recorded it — or when it is
one of the :data:`PREDEFINED_BENCHMARKS`, fixed in advance and not chosen after
the fact.

That has a consequence worth stating plainly: the pre-agent manual purchases
have **no recorded decision set**, so they may be compared against a broad-market
benchmark and nothing else. :func:`validate_opportunity_cost` refuses the rest.

## Standing

Lessons are **advisory**. A lesson is not authorization, cannot satisfy a
research requirement, and cannot relax a guardrail — the same standing as a
watchlist entry (``DISCOVERY_POLICY.md`` §2) or a research note. The guardrails
reject a decision payload that tries to use one otherwise.

Pure functions over plain data, plus reads of ``research/lessons/``. No
network, no subprocess — the same rule as the rest of ``src/``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Record kinds
# --------------------------------------------------------------------------

#: Rests only on time-of-decision information. May be cited.
LESSON = "LESSON"
#: Needs post-decision information. Recorded, never cited as a reason.
OUTCOME_OBSERVATION = "OUTCOME_OBSERVATION"

LESSON_KINDS = (LESSON, OUTCOME_OBSERVATION)

# --------------------------------------------------------------------------
# The quadrants
# --------------------------------------------------------------------------

PROCESS_SOUND = "PROCESS_SOUND"
PROCESS_UNSOUND = "PROCESS_UNSOUND"
PROCESS_UNKNOWN = "PROCESS_UNKNOWN"
PROCESS_VERDICTS = (PROCESS_SOUND, PROCESS_UNSOUND, PROCESS_UNKNOWN)

OUTCOME_GOOD = "OUTCOME_GOOD"
OUTCOME_BAD = "OUTCOME_BAD"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
OUTCOME_VERDICTS = (OUTCOME_GOOD, OUTCOME_BAD, OUTCOME_UNKNOWN)

CONFIRMED = "CONFIRMED"
ACCEPTED_RISK = "ACCEPTED_RISK"
LUCK = "LUCK"
CORRECTABLE = "CORRECTABLE"
UNCLASSIFIED = "UNCLASSIFIED"

QUADRANT_GUIDANCE = {
    CONFIRMED: "Sound process, good outcome. Reinforce the process, not the pick.",
    ACCEPTED_RISK: (
        "Sound process, bad outcome. This is NOT a mistake and must never be "
        "learned against — the risk was priced and accepted."
    ),
    LUCK: (
        "Unsound process, good outcome. The most dangerous quadrant: the "
        "outcome will encourage repeating a decision that was not justified."
    ),
    CORRECTABLE: (
        "Unsound process, bad outcome. The only quadrant that yields a genuine "
        "corrective lesson."
    ),
    UNCLASSIFIED: "Not enough information to place this decision.",
}


def classify(process: str, outcome: str) -> str:
    """Place a past decision in the four-quadrant model."""
    if process == PROCESS_SOUND and outcome == OUTCOME_GOOD:
        return CONFIRMED
    if process == PROCESS_SOUND and outcome == OUTCOME_BAD:
        return ACCEPTED_RISK
    if process == PROCESS_UNSOUND and outcome == OUTCOME_GOOD:
        return LUCK
    if process == PROCESS_UNSOUND and outcome == OUTCOME_BAD:
        return CORRECTABLE
    return UNCLASSIFIED


def is_learnable(quadrant: str) -> bool:
    """Whether a quadrant may produce a corrective lesson at all.

    ``ACCEPTED_RISK`` deliberately returns False: a sound decision that lost
    money teaches nothing about process. ``LUCK`` returns True because the
    lesson there is about the process, despite the pleasant outcome.
    """
    return quadrant in (CONFIRMED, LUCK, CORRECTABLE)


# --------------------------------------------------------------------------
# Sample and staleness thresholds
# --------------------------------------------------------------------------

#: No lesson from fewer than this many instances. One anecdote is not a pattern.
MIN_LESSON_SAMPLE = 3

#: A lesson must be re-derived after this long, like a research note.
LESSON_STALE_AFTER_DAYS = 90

#: Scope of the behaviour a lesson describes. Lessons about manual behaviour are
#: information for the owner and may not justify an agent purchase.
SCOPE_MANUAL = "MANUAL_ACTION"
SCOPE_AGENT = "AGENT_ACTION"
SCOPE_BOTH = "BOTH"
LESSON_SCOPES = (SCOPE_MANUAL, SCOPE_AGENT, SCOPE_BOTH)

CONFIDENCE_LEVELS = ("LOW", "MEDIUM", "HIGH")

# --------------------------------------------------------------------------
# Opportunity cost
# --------------------------------------------------------------------------

#: Fixed in advance, so a comparator can never be chosen after the fact.
#: Broad-market only — no sector or thematic funds, which would smuggle a view
#: back in.
PREDEFINED_BENCHMARKS = ("VOO", "SPY", "VTI", "ITOT")

COMPARATOR_DECISION_SET = "DECISION_SET"
COMPARATOR_BENCHMARK = "BENCHMARK"
COMPARATOR_BASES = (COMPARATOR_DECISION_SET, COMPARATOR_BENCHMARK)

# --------------------------------------------------------------------------
# Misuse detection
# --------------------------------------------------------------------------

#: Payload keys that would make a lesson do something a lesson may not do.
LESSON_MISUSE_FIELDS = (
    "lesson_authorizes",
    "lesson_authorization",
    "lesson_approves",
    "lesson_waives",
    "lesson_overrides_guardrail",
    "lesson_satisfies_research",
    "lessons_satisfy_research",
    "precedent_authorizes",
    "precedent_approves",
    "historical_precedent_approves",
)

#: Language that claims a lesson has standing it does not have.
LESSON_OVERREACH_PATTERNS = (
    r"lesson[s]?\s+(?:\w+\s+){0,3}?(?:authoris|authoriz|approv|permit|waiv|override)",
    r"(?:precedent|history|past\s+decision)[s]?\s+(?:\w+\s+){0,3}?(?:authoris|authoriz|approv|permit)",
    r"(?:because|since)\s+(?:it|this)\s+worked\s+(?:before|last\s+time)",
    r"no\s+further\s+research\s+(?:is\s+)?(?:needed|required)\s+(?:because|given)\s+(?:the\s+)?(?:lesson|precedent)",
)
_OVERREACH_RES = [re.compile(p, re.I) for p in LESSON_OVERREACH_PATTERNS]

#: A research component may not be satisfied by pointing at a lesson.
LESSON_REFERENCE_PATTERNS = (
    r"research/lessons/",
    r"^\s*LESSON[:\s]",
    r"\bsee\s+lesson\b",
)
_REFERENCE_RES = [re.compile(p, re.I | re.M) for p in LESSON_REFERENCE_PATTERNS]


def overreach_match(text: Any) -> Optional[str]:
    """The pattern by which this text claims a lesson has authority, if any."""
    blob = text if isinstance(text, str) else ""
    for pattern in _OVERREACH_RES:
        if pattern.search(blob):
            return pattern.pattern
    return None


def references_lesson(text: Any) -> bool:
    """Whether this text is a pointer to a lesson rather than research."""
    blob = text if isinstance(text, str) else ""
    return any(pattern.search(blob) for pattern in _REFERENCE_RES)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass
class Basis:
    """What a lesson rests on. Absent or thin, and the lesson is inadmissible."""

    n: int = 0
    window: str = ""
    data_sources: List[str] = field(default_factory=list)
    #: True when any input postdates the decisions being judged.
    uses_post_decision_data: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Lesson:
    """One derived, advisory finding about past decisions."""

    id: str = ""
    kind: str = LESSON
    statement: str = ""
    quadrant: str = UNCLASSIFIED
    scope: str = SCOPE_BOTH
    confidence: str = "LOW"
    basis: Basis = field(default_factory=Basis)
    created_on: Optional[str] = None
    #: Always advisory. Present in the record so a reader cannot miss it.
    advisory_only: bool = True

    def age_days(self, now: Optional[datetime] = None) -> Optional[int]:
        if not self.created_on:
            return None
        try:
            created = datetime.strptime(self.created_on, "%Y-%m-%d").date()
        except ValueError:
            return None
        return ((now or datetime.now()).date() - created).days

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        age = self.age_days(now)
        return age is None or age > LESSON_STALE_AFTER_DAYS

    def to_dict(self) -> Dict[str, Any]:
        data = dict(self.__dict__)
        data["basis"] = self.basis.to_dict()
        return data


@dataclass
class OpportunityCostComparison:
    """A past purchase measured against something it could actually have been."""

    subject_symbol: str = ""
    subject_date: Optional[str] = None
    comparator_symbol: str = ""
    comparator_basis: str = ""
    #: Symbols recorded as considered at decision time, from the decision log.
    decision_set: List[str] = field(default_factory=list)
    #: Where the decision set came from. Empty means there was none.
    decision_set_source: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_lesson(lesson: Lesson) -> List[str]:
    """Problems that make a lesson inadmissible. Empty means it may be kept."""
    problems: List[str] = []
    label = lesson.id or "lesson"

    if lesson.kind not in LESSON_KINDS:
        problems.append("%s: kind %r is not one of %s" % (label, lesson.kind, list(LESSON_KINDS)))

    if not (lesson.statement or "").strip():
        problems.append("%s: has no statement" % label)

    if lesson.scope not in LESSON_SCOPES:
        problems.append("%s: scope %r is not one of %s" % (label, lesson.scope, list(LESSON_SCOPES)))

    if lesson.confidence not in CONFIDENCE_LEVELS:
        problems.append(
            "%s: confidence %r is not one of %s"
            % (label, lesson.confidence, list(CONFIDENCE_LEVELS))
        )

    # --- the sample floor ---
    if lesson.basis.n < MIN_LESSON_SAMPLE:
        problems.append(
            "%s: basis.n is %d, below the %d-instance floor. One or two cases "
            "are an anecdote, not a pattern."
            % (label, lesson.basis.n, MIN_LESSON_SAMPLE)
        )

    if not lesson.basis.data_sources:
        problems.append("%s: basis.data_sources is empty — say what it rests on" % label)
    if not (lesson.basis.window or "").strip():
        problems.append("%s: basis.window is empty — say over what period" % label)

    # --- the anti-hindsight type rule ---
    if lesson.basis.uses_post_decision_data and lesson.kind == LESSON:
        problems.append(
            "%s: rests on post-decision data but is typed LESSON. Anything that "
            "needs to know what happened afterwards is an OUTCOME_OBSERVATION "
            "and may not be cited as a reason." % label
        )

    # --- the quadrant rule ---
    if lesson.kind == LESSON and lesson.quadrant == ACCEPTED_RISK:
        problems.append(
            "%s: ACCEPTED_RISK cannot produce a lesson. A sound decision that "
            "lost money is not a mistake, and learning against it teaches risk "
            "aversion rather than judgement." % label
        )

    # --- standing ---
    if not lesson.advisory_only:
        problems.append("%s: advisory_only must be true; a lesson is never authorization" % label)

    matched = overreach_match(lesson.statement)
    if matched:
        problems.append(
            "%s: the statement claims a lesson can authorise, approve, waive or "
            "override something (matched %r). Lessons are advisory only."
            % (label, matched)
        )

    return problems


def validate_opportunity_cost(comparison: OpportunityCostComparison) -> List[str]:
    """Reject a comparison whose comparator was picked with hindsight."""
    problems: List[str] = []
    label = "%s@%s" % (comparison.subject_symbol or "?", comparison.subject_date or "?")

    if comparison.comparator_basis not in COMPARATOR_BASES:
        problems.append(
            "%s: comparator_basis %r must be one of %s"
            % (label, comparison.comparator_basis, list(COMPARATOR_BASES))
        )
        return problems

    comparator = (comparison.comparator_symbol or "").strip().upper()
    if not comparator:
        problems.append("%s: no comparator_symbol" % label)
        return problems

    if comparison.comparator_basis == COMPARATOR_BENCHMARK:
        if comparator not in PREDEFINED_BENCHMARKS:
            problems.append(
                "%s: %r is not a predefined benchmark. Only %s may be used, and "
                "the list is fixed in advance precisely so a comparator cannot "
                "be chosen after the fact."
                % (label, comparator, list(PREDEFINED_BENCHMARKS))
            )
        return problems

    # DECISION_SET: the comparator must have been demonstrably on the table.
    if not comparison.decision_set:
        problems.append(
            "%s: comparator_basis is DECISION_SET but no decision set is "
            "recorded. A purchase with no recorded decision set — every "
            "pre-agent manual buy — may only be compared against a predefined "
            "benchmark." % label
        )
        return problems

    if not (comparison.decision_set_source or "").strip():
        problems.append(
            "%s: the decision set must name its source (e.g. "
            "logs/decisions.jsonl) so it can be audited" % label
        )

    available = {str(s).strip().upper() for s in comparison.decision_set}
    if comparator not in available:
        problems.append(
            "%s: %r was not in the decision set %s. Comparing against an "
            "alternative that was never actually on the table is hindsight "
            "bias, not opportunity cost."
            % (label, comparator, sorted(available)[:8])
        )
    return problems


def admissible_comparators(
    decision_set: Sequence[str], has_decision_set: bool
) -> Dict[str, List[str]]:
    """What a given past purchase may legitimately be measured against."""
    return {
        "benchmarks": list(PREDEFINED_BENCHMARKS),
        "decision_set": (
            sorted({str(s).strip().upper() for s in decision_set})
            if has_decision_set else []
        ),
    }


# --------------------------------------------------------------------------
# Lesson files under research/lessons/
# --------------------------------------------------------------------------
#
# Same shape as a research note: a title, a header block of ``- **key:** value``
# lines, then sections. Human-readable, machine-checkable, and validated by the
# same rules as an in-memory Lesson so a file cannot claim standing the type
# system refuses.

LESSONS_DIRNAME = "lessons"

REQUIRED_LESSON_FILE_KEYS = (
    "id", "kind", "quadrant", "scope", "confidence",
    "sample_n", "window", "created_on",
)

REQUIRED_LESSON_SECTIONS = (
    "Statement",
    "What this rests on",
    "What it does not say",
    "Standing",
)

_LESSON_HEADER_RE = re.compile(
    r"^\s*[-*]\s+\*\*(?P<key>[a-z_]+):\*\*\s*(?P<value>.+?)\s*$", re.M)
_LESSON_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)


def parse_lesson_file(text: str) -> Tuple[Lesson, Dict[str, str], List[str]]:
    """Read a lesson file into a :class:`Lesson`, its header, and its sections."""
    header = {m.group("key"): m.group("value").strip()
              for m in _LESSON_HEADER_RE.finditer(text or "")}
    sections = [m.group(2).strip() for m in _LESSON_HEADING_RE.finditer(text or "")]

    def integer(key: str) -> int:
        try:
            return int(str(header.get(key, "")).strip())
        except (TypeError, ValueError):
            return 0

    sources = [s.strip() for s in str(header.get("data_sources", "")).split(",")
               if s.strip()]
    statement = ""
    lines = (text or "").split("\n")
    for index, line in enumerate(lines):
        if line.strip().lower().startswith("## statement"):
            statement = " ".join(
                l.strip() for l in lines[index + 1:index + 8]
                if l.strip() and not l.startswith("#")
            )
            break

    lesson = Lesson(
        id=header.get("id", ""),
        kind=header.get("kind", ""),
        statement=statement,
        quadrant=header.get("quadrant", ""),
        scope=header.get("scope", ""),
        confidence=header.get("confidence", ""),
        basis=Basis(
            n=integer("sample_n"),
            window=header.get("window", ""),
            data_sources=sources,
            uses_post_decision_data=str(
                header.get("uses_post_decision_data", "false")
            ).strip().lower() in ("true", "yes"),
        ),
        created_on=header.get("created_on") or None,
        advisory_only=str(header.get("advisory_only", "true")).strip().lower()
        not in ("false", "no"),
    )
    return lesson, header, sections


def validate_lesson_file(text: str, path: str = "") -> List[str]:
    """Problems with a lesson file. Empty means it may be kept and cited."""
    import os as _os

    problems: List[str] = []
    label = _os.path.basename(path) or "lesson file"
    lesson, header, sections = parse_lesson_file(text)

    missing_keys = [k for k in REQUIRED_LESSON_FILE_KEYS if not header.get(k, "").strip()]
    if missing_keys:
        problems.append("%s is missing header keys: %s" % (label, ", ".join(missing_keys)))

    present = [s.lower() for s in sections]
    for required in REQUIRED_LESSON_SECTIONS:
        if not any(required.lower() in section for section in present):
            problems.append("%s is missing the %r section" % (label, required))

    problems.extend("%s: %s" % (label, p) for p in validate_lesson(lesson))
    return problems


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LESSONS_DIR = os.path.join(REPO_ROOT, "research", LESSONS_DIRNAME)
LESSONS_INDEX = os.path.join(LESSONS_DIR, "INDEX.md")


def list_lesson_paths(lessons_dir: str = LESSONS_DIR) -> List[str]:
    """Every lesson file, sorted. Never raises when the directory is absent."""
    try:
        names = os.listdir(lessons_dir)
    except OSError:
        return []
    return [
        os.path.join(lessons_dir, name)
        for name in sorted(names)
        if name.endswith(".md") and name != "INDEX.md"
    ]


def load_lessons(lessons_dir: str = LESSONS_DIR) -> List[Tuple[str, Lesson]]:
    """``(filename, Lesson)`` for every lesson file that parses."""
    out: List[Tuple[str, Lesson]] = []
    for path in list_lesson_paths(lessons_dir):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        lesson, _, _ = parse_lesson_file(text)
        out.append((os.path.basename(path), lesson))
    return out


def validate_lessons_directory(lessons_dir: str = LESSONS_DIR) -> Dict[str, List[str]]:
    """Problems per lesson file. Empty dict means every lesson is admissible."""
    out: Dict[str, List[str]] = {}
    for path in list_lesson_paths(lessons_dir):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            out[path] = ["could not read: %s" % exc]
            continue
        problems = validate_lesson_file(text, path)
        if problems:
            out[path] = problems
    return out


def render_lessons_index(
    entries: Sequence[Tuple[str, Lesson]], now: Optional[datetime] = None
) -> str:
    """A compact index of the lesson files. Regenerated, never hand-edited."""
    now = now or datetime.now()
    lines = [
        "# Historical lessons — index",
        "",
        "Derived findings about **past** decisions, kept so a later evaluation can",
        "learn from them instead of re-deriving them.",
        "",
        "> **Lessons are advisory.** A lesson is not authorization, cannot satisfy a",
        "> research requirement, and cannot relax a guardrail — the same standing as a",
        "> watchlist entry. `src/guardrails.py` rejects a decision that treats one",
        "> otherwise (`LESSON_MISUSED_AS_AUTHORIZATION`).",
        "",
        "`LESSON` rests only on what was knowable when the decision was made and may",
        "be cited. `OUTCOME_OBSERVATION` needs to know what happened afterwards and",
        "may **never** be cited as a reason — that separation is what keeps hindsight",
        "bias out.",
        "",
        "Regenerated automatically — edit the lesson files, not this one.",
        "",
        "*Last regenerated: %s*" % now.strftime("%Y-%m-%d %H:%M"),
        "",
        "| ID | Kind | Quadrant | Scope | n | Confidence | Reviewed | Stale |",
        "|---|---|---|---|---:|---|---|---|",
    ]
    for filename, lesson in sorted(entries):
        lines.append("| [%s](%s) | %s | %s | %s | %d | %s | %s | %s |" % (
            lesson.id or filename, filename, lesson.kind, lesson.quadrant,
            lesson.scope, lesson.basis.n, lesson.confidence,
            lesson.created_on or "—", "yes" if lesson.is_stale(now) else "no"))
    lines.extend([
        "",
        "A lesson goes stale after %d days and stops being citable until it is"
        % LESSON_STALE_AFTER_DAYS,
        "re-derived. No lesson may rest on fewer than %d instances."
        % MIN_LESSON_SAMPLE,
        "",
    ])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Derivation helpers
# --------------------------------------------------------------------------


def process_criteria_at_decision_time() -> Tuple[str, ...]:
    """The only inputs a process verdict may use.

    Every one of these is knowable at the moment of the decision. Nothing here
    depends on what the price did next, which is what keeps a process verdict
    from collapsing into an outcome verdict.
    """
    return (
        "position_size_consistent_with_stated_policy",
        "portfolio_concentration_at_entry",
        "correlation_with_existing_holdings_at_entry",
        "trailing_run_up_before_entry",
        "basis_direction_across_prior_lots",
        "whether_a_thesis_was_recorded",
        "whether_the_research_package_was_complete",
        "whether_alternatives_were_compared",
    )


def outcome_criteria() -> Tuple[str, ...]:
    """Inputs that postdate the decision. Only ever OUTCOME_OBSERVATION."""
    return (
        "unrealized_return_since_entry",
        "realized_gain_or_loss",
        "price_path_after_entry",
        "subsequent_relative_performance_vs_benchmark",
    )


def summarize_quadrants(lessons: Sequence[Lesson]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for lesson in lessons:
        counts[lesson.quadrant] = counts.get(lesson.quadrant, 0) + 1
    return counts


def citable(lessons: Sequence[Lesson], now: Optional[datetime] = None) -> List[Lesson]:
    """Lessons a later evaluation may actually cite.

    Excludes outcome observations (wrong type), stale lessons, and anything
    that fails validation.
    """
    out = []
    for lesson in lessons:
        if lesson.kind != LESSON:
            continue
        if lesson.is_stale(now):
            continue
        if validate_lesson(lesson):
            continue
        out.append(lesson)
    return out
