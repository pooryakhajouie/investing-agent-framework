"""Persistent per-candidate research notes.

## The problem

A scheduled run that re-derives twenty theses every weekday against an
unchanged tape is burning money to produce yesterday's conclusion. The previous
arrangement had nowhere else to put the work: research lived inside whichever
report happened to contain it, and ``reports/latest.md`` was a byte copy of the
newest one. Shortening that copy would have thrown the research away.

So research gets its own tier, keyed by candidate rather than by date:

    research/GOOGL.md      research/BTC-USD.md      research/SNDK.md

A note is the accumulated position on one candidate — its thesis, both cases,
the risks, the evidence with dates, and what would change its classification.
A run **reads** the notes before deciding what to research, **refreshes** the
ones whose facts moved, and **leaves the rest alone**. That is what makes a
short daily digest safe: the analysis is not in the digest, so shortening the
digest cannot lose it.

## What a note is not

It is not a decision, and it is not an approval. A note may say
``ADD_CANDIDATE``; that is a reasoning label (``DISCOVERY_POLICY.md`` §7), and
it authorises nothing. Notes are inputs to an evaluation, exactly like a
watchlist — an interest signal, never permission.

Nor is a note a substitute for the audit record. It carries the standing view of
a candidate; ``reports/YYYY-MM-DD_HHMM.md`` carries what a particular run saw
and decided on a particular day. Both are fingerprinted before and after every
scheduled run (``src.scheduling.archive_inventory``), so a run that deletes or
guts one fails.

Pure text handling plus reads of ``research/``. Nothing here writes, approves,
or executes anything.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH_DIRNAME = "research"
RESEARCH_DIR = os.path.join(REPO_ROOT, RESEARCH_DIRNAME)
RESEARCH_INDEX = os.path.join(RESEARCH_DIR, "INDEX.md")

# Header keys every note carries, as ``- **key:** value`` lines under the title.
# Deliberately a flat list rather than YAML: standard library only, and a human
# reading the file sees the same thing the parser does.
REQUIRED_NOTE_KEYS = (
    "symbol",
    "asset_class",
    "position",
    "classification",
    "last_reviewed",
)

REQUIRED_NOTE_SECTIONS = (
    "Snapshot",
    "Thesis",
    "Bull case",
    "Bear case",
    "Risks",
    "What would change the classification",
    "Evidence",
)

VALID_NOTE_ASSET_CLASSES = ("EQUITY", "ETF", "CRYPTO")
VALID_NOTE_POSITIONS = ("EXISTING_POSITION", "NEW_POSITION")

# How long a note's *thesis* stays usable before a run should revisit it.
# Prices and quotes are never reused from a note -- they are re-read every run,
# and the Snapshot section records when they were taken.
NOTE_STALE_AFTER_DAYS = 14

# A note may be long. It is read by a machine deciding what to re-research, not
# by a human every morning, so there is no word budget here -- only a floor,
# because a two-line note is not research.
NOTE_WORD_FLOOR = 120

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_HEADER_RE = re.compile(r"^\s*[-*]\s+\*\*(?P<key>[a-z_]+):\*\*\s*(?P<value>.+?)\s*$", re.M)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ResearchNoteError(ValueError):
    """Raised when a note cannot be located or parsed at all."""


@dataclass
class ResearchNote:
    """One candidate's standing research position."""

    symbol: str = ""
    asset_class: str = ""
    position: str = ""
    classification: str = ""
    last_reviewed: Optional[date] = None
    path: str = ""
    header: Dict[str, str] = field(default_factory=dict)
    sections: List[str] = field(default_factory=list)
    word_count: int = 0

    def age_days(self, now: Optional[datetime] = None) -> Optional[int]:
        if self.last_reviewed is None:
            return None
        now = now or datetime.now()
        return (now.date() - self.last_reviewed).days

    def is_stale(
        self, now: Optional[datetime] = None, max_age_days: int = NOTE_STALE_AFTER_DAYS
    ) -> bool:
        """True when the thesis is old enough to be worth revisiting.

        A note with no ``last_reviewed`` counts as stale: an undated view is
        not a view a run may lean on.
        """
        age = self.age_days(now)
        return age is None or age > max_age_days

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "position": self.position,
            "classification": self.classification,
            "last_reviewed": self.last_reviewed.isoformat() if self.last_reviewed else None,
            "path": self.path,
            "sections": list(self.sections),
            "word_count": self.word_count,
        }


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def normalize_symbol(symbol: str) -> str:
    """Uppercase a ticker or crypto pair, and reject anything path-shaped.

    A note filename comes from a symbol, so this is also the guard that keeps a
    symbol from becoming a traversal.
    """
    cleaned = (symbol or "").strip().upper()
    if not _SYMBOL_RE.match(cleaned):
        raise ResearchNoteError(
            "%r is not a usable symbol for a research note; expected a ticker "
            "(GOOGL) or a hyphenated crypto pair (BTC-USD)" % symbol
        )
    return cleaned


def note_filename(symbol: str) -> str:
    return normalize_symbol(symbol) + ".md"


def note_path(symbol: str, research_dir: str = RESEARCH_DIR) -> str:
    return os.path.join(research_dir, note_filename(symbol))


def list_note_paths(research_dir: str = RESEARCH_DIR) -> List[str]:
    """Every note file, sorted. Never raises when the directory is absent."""
    try:
        names = os.listdir(research_dir)
    except OSError:
        return []
    return [
        os.path.join(research_dir, name)
        for name in sorted(names)
        if name.endswith(".md") and name != os.path.basename(RESEARCH_INDEX)
    ]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_note(text: str, path: str = "") -> ResearchNote:
    """Read a note's header and section list. Never raises on bad content."""
    note = ResearchNote(path=path)
    text = text or ""
    note.word_count = len(text.split())

    for match in _HEADER_RE.finditer(text):
        note.header[match.group("key")] = match.group("value").strip()

    note.symbol = note.header.get("symbol", "").strip().upper()
    note.asset_class = note.header.get("asset_class", "").strip().upper()
    note.position = note.header.get("position", "").strip().upper()
    note.classification = note.header.get("classification", "").strip().upper()

    raw_date = note.header.get("last_reviewed", "").strip()
    if _DATE_RE.match(raw_date):
        try:
            note.last_reviewed = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            note.last_reviewed = None

    in_fence = False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            note.sections.append(heading.group(2).strip())

    return note


def load_note(symbol: str, research_dir: str = RESEARCH_DIR) -> Optional[ResearchNote]:
    """The note for ``symbol``, or None when there is none."""
    path = note_path(symbol, research_dir)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return parse_note(handle.read(), path)
    except OSError:
        return None


def load_all(research_dir: str = RESEARCH_DIR) -> List[ResearchNote]:
    notes = []
    for path in list_note_paths(research_dir):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                notes.append(parse_note(handle.read(), path))
        except OSError:
            continue
    return notes


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate_note(text: str, path: str = "") -> List[str]:
    """Problems with a note, as messages. Empty means the note is well-formed."""
    problems: List[str] = []
    note = parse_note(text, path)
    label = os.path.basename(path) or note.symbol or "note"

    missing_keys = [k for k in REQUIRED_NOTE_KEYS if not note.header.get(k, "").strip()]
    if missing_keys:
        problems.append(
            "%s is missing header keys: %s. Each is a '- **key:** value' line "
            "under the title." % (label, ", ".join(missing_keys))
        )

    if note.symbol:
        try:
            normalize_symbol(note.symbol)
        except ResearchNoteError as exc:
            problems.append("%s: %s" % (label, exc))

    if note.asset_class and note.asset_class not in VALID_NOTE_ASSET_CLASSES:
        problems.append(
            "%s declares asset_class %r; expected one of %s"
            % (label, note.asset_class, list(VALID_NOTE_ASSET_CLASSES))
        )

    if note.position and note.position not in VALID_NOTE_POSITIONS:
        problems.append(
            "%s declares position %r; expected one of %s"
            % (label, note.position, list(VALID_NOTE_POSITIONS))
        )

    if note.header.get("last_reviewed") and note.last_reviewed is None:
        problems.append(
            "%s has an unparseable last_reviewed %r; use YYYY-MM-DD"
            % (label, note.header.get("last_reviewed"))
        )

    present = [s.lower() for s in note.sections]
    for required in REQUIRED_NOTE_SECTIONS:
        if not any(required.lower() in section for section in present):
            problems.append("%s is missing the %r section" % (label, required))

    if note.word_count < NOTE_WORD_FLOOR:
        problems.append(
            "%s is %d words, below the %d-word floor; a note this thin is a "
            "placeholder, not research" % (label, note.word_count, NOTE_WORD_FLOOR)
        )

    # A note names a symbol; the filename must agree, or the next run looks in
    # the wrong place and re-derives work that already exists.
    if path and note.symbol:
        expected = note_filename(note.symbol) if _SYMBOL_RE.match(note.symbol) else None
        if expected and os.path.basename(path) != expected:
            problems.append(
                "%s declares symbol %s but is filed as %s"
                % (label, note.symbol, os.path.basename(path))
            )

    return problems


def validate_directory(research_dir: str = RESEARCH_DIR) -> Dict[str, List[str]]:
    """Problems per note file. An empty dict means every note is well-formed."""
    out: Dict[str, List[str]] = {}
    for path in list_note_paths(research_dir):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            out[path] = ["could not read: %s" % exc]
            continue
        problems = validate_note(text, path)
        if problems:
            out[path] = problems
    return out


# --------------------------------------------------------------------------
# The index a run reads first
# --------------------------------------------------------------------------


def index_rows(
    notes: List[ResearchNote], now: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    """One row per note: enough to decide what needs re-researching."""
    rows = []
    for note in sorted(notes, key=lambda n: (n.asset_class, n.symbol)):
        rows.append(
            {
                "symbol": note.symbol,
                "asset_class": note.asset_class,
                "position": note.position,
                "classification": note.classification,
                "last_reviewed": note.last_reviewed.isoformat() if note.last_reviewed else "—",
                "age_days": note.age_days(now),
                "stale": note.is_stale(now),
            }
        )
    return rows


def render_index(notes: List[ResearchNote], now: Optional[datetime] = None) -> str:
    """A compact markdown index of the research notes.

    Regenerated rather than hand-maintained, so it cannot drift from the notes.
    """
    now = now or datetime.now()
    lines = [
        "# Research notes — index",
        "",
        "Standing per-candidate research, carried across scheduled runs so the same",
        "analysis is not re-derived every weekday. A classification here is a",
        "reasoning label, **never** authorization to buy anything.",
        "",
        "Regenerated automatically — edit the notes, not this file.",
        "",
        "*Last regenerated: %s*" % now.strftime("%Y-%m-%d %H:%M"),
        "",
        "| Symbol | Class | Position | Classification | Last reviewed | Age (d) | Stale |",
        "|---|---|---|---|---|---:|---|",
    ]
    for row in index_rows(notes, now):
        lines.append(
            "| [%s](%s.md) | %s | %s | %s | %s | %s | %s |"
            % (
                row["symbol"],
                row["symbol"],
                row["asset_class"],
                row["position"],
                row["classification"],
                row["last_reviewed"],
                "—" if row["age_days"] is None else row["age_days"],
                "yes" if row["stale"] else "no",
            )
        )
    lines.extend(
        [
            "",
            "A note is **stale** after %d days. Stale means *revisit the thesis*, not"
            % NOTE_STALE_AFTER_DAYS,
            "*discard it*. Prices and quotes are never reused from a note: they are",
            "re-read from the broker every run, and each note's Snapshot section records",
            "when its figures were taken.",
            "",
        ]
    )
    return "\n".join(lines)


def stale_symbols(
    notes: List[ResearchNote], now: Optional[datetime] = None
) -> List[str]:
    """Symbols whose standing thesis is old enough to revisit."""
    return [n.symbol for n in notes if n.is_stale(now) and n.symbol]
