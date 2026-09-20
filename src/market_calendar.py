"""US equity market calendar, and what it means for a one-month authorization.

The $25 authorization does not carry into the next month. That makes the
**final tradable opportunity of the calendar month** a hard boundary, not a
soft one:

    An event that occurs after the month's last tradable moment cannot be
    acted on with that month's authorization. Whatever it teaches is
    information for *next* month's $25.

The concrete case this module exists for: a company reporting *after the close*
on the last trading day of the month. The print lands inside the month on a
calendar, but the first equity session in which anyone could respond to it is in
the following month. A report that describes such an event as something this
month's budget can act on is simply wrong, and waiting through it means the
month's authorization expires unused — which is a legitimate choice, but only if
it is stated as one.

The boundary is **asset-class dependent**, which is the whole reason it needs
computing rather than eyeballing:

* equities and ETFs trade only during exchange sessions, so the boundary is the
  close of the month's last trading day;
* crypto trades continuously, so the boundary is the last instant of the month.

An event after the equity close on the 30th is therefore next-month-only for a
stock and still this-month for a coin.

Everything here is pure, deterministic and standard-library only. No network,
no subprocess, no I/O. Holidays are computed from the NYSE rules rather than
looked up, so the module is correct for years nobody has hard-coded yet.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, FrozenSet, Optional, Tuple

try:  # Python 3.9+ standard library
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 3.8 and earlier are unsupported
    ZoneInfo = None  # type: ignore[assignment]

# --------------------------------------------------------------------------
# Three clocks, because one clock is wrong for at least one of these jobs
# --------------------------------------------------------------------------
#
# A single global clock produced a real failure: a decision written on
# 2026-09-09 at 19:10 America/Chicago was re-validated a few minutes later and
# rejected for reporting 22 days and 16 sessions remaining, because UTC had
# already rolled to 2026-09-10 while neither the operator's date nor the
# market's date had. The calendar arithmetic was right; the clock was wrong.
#
# The three jobs want three different clocks:
#
#   PROJECT   the monthly authorization. "Which month is this $25 in, and how
#             many days are left" is the owner's calendar question, answered in
#             the project timezone. Applies to equities and crypto alike: the
#             budget boundary is a budget concept, not a market one.
#
#   EXCHANGE  equity sessions. Whether the NYSE is open, how many sessions are
#             left, and whether an event landed before or after a close, are
#             all questions about the exchange's own date.
#
#   CRYPTO    continuous markets, which genuinely have no local date. UTC is
#             the honest choice for "is it still the same trading moment",
#             while the *authorization* boundary above still follows PROJECT.
PROJECT_TIMEZONE = "America/Chicago"
EXCHANGE_TIMEZONE = "America/New_York"
CRYPTO_TIMEZONE = "UTC"


def _zone(name: str):
    if ZoneInfo is None:  # pragma: no cover
        raise RuntimeError("zoneinfo is required; Python 3.9+ is the stated floor")
    return timezone.utc if name == "UTC" else ZoneInfo(name)


def _aware(moment: Optional[datetime]) -> datetime:
    """Any datetime, as an aware one. Naive input is read as UTC."""
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def project_now(now: Optional[datetime] = None) -> datetime:
    """``now`` in the project/budget timezone."""
    return _aware(now).astimezone(_zone(PROJECT_TIMEZONE))


def exchange_now(now: Optional[datetime] = None) -> datetime:
    """``now`` in the exchange timezone."""
    return _aware(now).astimezone(_zone(EXCHANGE_TIMEZONE))


def project_date(now: Optional[datetime] = None) -> date:
    """The calendar date that owns the monthly authorization right now."""
    return project_now(now).date()


def exchange_date(now: Optional[datetime] = None) -> date:
    """The calendar date the equity market is on right now."""
    return exchange_now(now).date()


def project_month(now: Optional[datetime] = None) -> str:
    """The authorization month as ``YYYY-MM``, in the project timezone."""
    return project_now(now).strftime("%Y-%m")

# --------------------------------------------------------------------------
# Asset classes, as they bear on when a market is open
# --------------------------------------------------------------------------

EXCHANGE_TRADED = ("EQUITY", "ETF")
CONTINUOUS = ("CRYPTO",)
VALID_EVENT_ASSET_CLASSES = EXCHANGE_TRADED + CONTINUOUS

# What a boundary computation can conclude.
ACTIONABLE_THIS_MONTH = "ACTIONABLE_THIS_MONTH"
ACTIONABLE_NEXT_MONTH_ONLY = "ACTIONABLE_NEXT_MONTH_ONLY"

# Regular and early NYSE closes, in exchange local time (America/New_York).
# The repository runs in America/Chicago and the agent reasons in calendar
# dates, so these are used as *clock times on the trading date* rather than
# converted; the only thing that depends on them is whether an event landed
# before or after the session ended on that date.
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
REGULAR_OPEN = time(9, 30)

# Juneteenth became an NYSE holiday in 2022.
JUNETEENTH_FIRST_YEAR = 2022


# --------------------------------------------------------------------------
# Holidays
# --------------------------------------------------------------------------


def easter_sunday(year: int) -> date:
    """Easter Sunday, by the anonymous Gregorian algorithm.

    Needed only because Good Friday is an NYSE holiday and is the one market
    closure that does not fall on a fixed date or an nth weekday.
    """
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0) of a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` (Mon=0) of a month."""
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> Optional[date]:
    """NYSE observance for a fixed-date holiday.

    Saturday holidays are observed on the preceding Friday and Sunday holidays
    on the following Monday — except that a Saturday January 1 is not observed
    at all, because the Friday before it belongs to the previous year.
    """
    if day.weekday() == 5:  # Saturday
        if day.month == 1 and day.day == 1:
            return None
        return day - timedelta(days=1)
    if day.weekday() == 6:  # Sunday
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> FrozenSet[date]:
    """Full-day NYSE closures for a calendar year."""
    days = []

    for fixed in (date(year, 1, 1), date(year, 7, 4), date(year, 12, 25)):
        observed = _observed(fixed)
        if observed is not None and observed.year == year:
            days.append(observed)

    if year >= JUNETEENTH_FIRST_YEAR:
        observed = _observed(date(year, 6, 19))
        if observed is not None:
            days.append(observed)

    days.append(nth_weekday(year, 1, 0, 3))          # MLK Day
    days.append(nth_weekday(year, 2, 0, 3))          # Washington's Birthday
    days.append(easter_sunday(year) - timedelta(days=2))  # Good Friday
    days.append(last_weekday(year, 5, 0))            # Memorial Day
    days.append(nth_weekday(year, 9, 0, 1))          # Labor Day
    days.append(nth_weekday(year, 11, 3, 4))         # Thanksgiving

    # A Sunday January 1 pushes New Year's observance into the following year.
    if date(year - 1, 12, 31).weekday() == 6:
        days.append(date(year, 1, 1))

    return frozenset(d for d in days if d.year == year)


def nyse_early_closes(year: int) -> FrozenSet[date]:
    """Half sessions (13:00 close). These are trading days, just short ones."""
    days = []

    thanksgiving = nth_weekday(year, 11, 3, 4)
    days.append(thanksgiving + timedelta(days=1))  # the Friday after

    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5:
        days.append(christmas_eve)

    july_third = date(year, 7, 3)
    if july_third.weekday() < 5 and date(year, 7, 4).weekday() < 5:
        days.append(july_third)

    holidays = nyse_holidays(year)
    return frozenset(d for d in days if d.weekday() < 5 and d not in holidays)


def is_trading_day(day: date) -> bool:
    """True when the US equity market holds a session on ``day``."""
    if day.weekday() >= 5:
        return False
    return day not in nyse_holidays(day.year)


def market_close(day: date) -> time:
    """The closing time for a session — 13:00 on a half day, else 16:00."""
    return EARLY_CLOSE if day in nyse_early_closes(day.year) else REGULAR_CLOSE


def last_trading_day_of_month(year: int, month: int) -> date:
    """The final US equity session of a calendar month."""
    day = date(year, month, calendar.monthrange(year, month)[1])
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def next_trading_day(day: date) -> date:
    """The first session strictly after ``day``."""
    candidate = day + timedelta(days=1)
    while not is_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


# --------------------------------------------------------------------------
# The month boundary
# --------------------------------------------------------------------------


def final_opportunity(year: int, month: int, asset_class: str = "EQUITY") -> datetime:
    """The last moment in the month at which this month's $25 could be used.

    For an exchange-traded asset that is the close of the month's last session.
    For crypto, which trades continuously, it is the last instant of the month.
    """
    normalized = (asset_class or "EQUITY").strip().upper()
    if normalized in CONTINUOUS:
        last = date(year, month, calendar.monthrange(year, month)[1])
        return datetime.combine(last, time(23, 59, 59))
    session = last_trading_day_of_month(year, month)
    return datetime.combine(session, market_close(session))


def event_moment(
    event_date: date,
    occurs_after_close: bool = False,
    asset_class: str = "EQUITY",
) -> datetime:
    """When an event's information becomes usable, as a datetime.

    An event released *after the close* is stamped one second past that
    session's close: the information exists on that calendar date, but the
    earliest exchange session in which anyone could respond is the next one.
    """
    normalized = (asset_class or "EQUITY").strip().upper()
    if occurs_after_close:
        if normalized in CONTINUOUS:
            return datetime.combine(event_date, time(16, 0, 1))
        return datetime.combine(event_date, market_close(event_date)) + timedelta(seconds=1)
    return datetime.combine(event_date, REGULAR_OPEN)


def event_actionability(
    event_date: date,
    asset_class: str = "EQUITY",
    occurs_after_close: bool = False,
    reference: Optional[datetime] = None,
) -> str:
    """Can this month's authorization act on this event?

    Returns ``ACTIONABLE_THIS_MONTH`` or ``ACTIONABLE_NEXT_MONTH_ONLY``,
    judged against the calendar month of ``reference`` (default: the event's
    own month). An event that already happened counts as actionable this
    month — the information is in hand and the budget is still live.
    """
    reference = reference or datetime.combine(event_date, REGULAR_OPEN)
    boundary = final_opportunity(reference.year, reference.month, asset_class)
    moment = event_moment(event_date, occurs_after_close, asset_class)
    if moment > boundary:
        return ACTIONABLE_NEXT_MONTH_ONLY
    return ACTIONABLE_THIS_MONTH


def first_actionable_session(
    event_date: date,
    asset_class: str = "EQUITY",
    occurs_after_close: bool = False,
) -> date:
    """The first date on which the event could actually be traded on."""
    normalized = (asset_class or "EQUITY").strip().upper()
    if normalized in CONTINUOUS:
        return event_date
    if occurs_after_close or not is_trading_day(event_date):
        return next_trading_day(event_date)
    return event_date


def describe_boundary(
    event_date: date,
    asset_class: str = "EQUITY",
    occurs_after_close: bool = False,
    reference: Optional[datetime] = None,
) -> Dict[str, str]:
    """A plain-language account of the boundary, for reports and violations."""
    reference = reference or datetime.combine(event_date, REGULAR_OPEN)
    verdict = event_actionability(event_date, asset_class, occurs_after_close, reference)
    boundary = final_opportunity(reference.year, reference.month, asset_class)
    session = first_actionable_session(event_date, asset_class, occurs_after_close)
    return {
        "actionability": verdict,
        "final_opportunity": boundary.strftime("%Y-%m-%d %H:%M"),
        "first_actionable_session": session.isoformat(),
        "reference_month": "%04d-%02d" % (reference.year, reference.month),
        "explanation": (
            "%s%s is the month's final tradable opportunity for a %s position. "
            "The event's information is first tradable on %s, which is %s."
            % (
                boundary.strftime("%Y-%m-%d"),
                " " + boundary.strftime("%H:%M"),
                (asset_class or "EQUITY").strip().upper(),
                session.isoformat(),
                "inside the month"
                if verdict == ACTIONABLE_THIS_MONTH
                else "in the following month, so this month's authorization cannot "
                     "be used on it and expires unused if the wait continues",
            )
        ),
    }


def parse_event_date(value: str) -> Tuple[Optional[date], Optional[str]]:
    """Parse ``YYYY-MM-DD``, returning ``(date, error)``."""
    if not isinstance(value, str) or not value.strip():
        return None, "an event date must be a non-empty 'YYYY-MM-DD' string"
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date(), None
    except ValueError:
        return None, "%r is not a valid 'YYYY-MM-DD' date" % value
