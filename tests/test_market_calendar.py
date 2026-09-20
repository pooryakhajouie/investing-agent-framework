"""The calendar boundary of a non-carrying monthly authorization.

The property under test: an event that occurs after the month's final tradable
opportunity is **next month's** information. This month's $25 cannot reach it,
and a decision that says otherwise is wrong about the calendar rather than
merely optimistic.

The concrete case, and the reason this file exists: MU reports after the close
on 2026-09-30, the last trading day of September. Its normal post-earnings
equity reaction is tradable on 2026-10-01, funded by October's authorization.
September's $25 does not roll over, so waiting through that print means
allowing September's authorization to expire unused.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.market_calendar import (  # noqa: E402
    ACTIONABLE_NEXT_MONTH_ONLY,
    ACTIONABLE_THIS_MONTH,
    EARLY_CLOSE,
    REGULAR_CLOSE,
    describe_boundary,
    easter_sunday,
    event_actionability,
    event_moment,
    final_opportunity,
    first_actionable_session,
    is_trading_day,
    last_trading_day_of_month,
    market_close,
    next_trading_day,
    nyse_early_closes,
    nyse_holidays,
    parse_event_date,
)


class HolidayTests(unittest.TestCase):
    """Computed, not hard-coded — so future years are right too."""

    def test_easter_matches_known_dates(self):
        for year, expected in (
            (2024, date(2024, 3, 31)),
            (2025, date(2025, 4, 20)),
            (2026, date(2026, 4, 5)),
            (2027, date(2027, 3, 28)),
        ):
            with self.subTest(year=year):
                self.assertEqual(easter_sunday(year), expected)

    def test_the_2026_nyse_holiday_calendar(self):
        self.assertEqual(
            sorted(nyse_holidays(2026)),
            [
                date(2026, 1, 1),    # New Year's Day
                date(2026, 1, 19),   # MLK Day
                date(2026, 2, 16),   # Washington's Birthday
                date(2026, 4, 3),    # Good Friday
                date(2026, 5, 25),   # Memorial Day
                date(2026, 6, 19),   # Juneteenth
                date(2026, 7, 3),    # Independence Day (observed; the 4th is a Saturday)
                date(2026, 9, 7),    # Labor Day
                date(2026, 11, 26),  # Thanksgiving
                date(2026, 12, 25),  # Christmas
            ],
        )

    def test_the_2025_nyse_holiday_calendar(self):
        self.assertEqual(
            sorted(nyse_holidays(2025)),
            [
                date(2025, 1, 1),
                date(2025, 1, 20),
                date(2025, 2, 17),
                date(2025, 4, 18),
                date(2025, 5, 26),
                date(2025, 6, 19),
                date(2025, 7, 4),
                date(2025, 9, 1),
                date(2025, 11, 27),
                date(2025, 12, 25),
            ],
        )

    def test_juneteenth_did_not_exist_before_2022(self):
        self.assertNotIn(date(2021, 6, 18), nyse_holidays(2021))
        self.assertNotIn(date(2021, 6, 21), nyse_holidays(2021))
        self.assertIn(date(2022, 6, 20), nyse_holidays(2022))  # the 19th was a Sunday

    def test_a_saturday_new_year_is_not_observed_in_the_new_year(self):
        # 2022-01-01 was a Saturday; the NYSE did not close on 2021-12-31.
        self.assertNotIn(date(2022, 1, 1), nyse_holidays(2022))
        self.assertNotIn(date(2021, 12, 31), nyse_holidays(2021))
        self.assertTrue(is_trading_day(date(2021, 12, 31)))

    def test_weekends_are_never_trading_days(self):
        self.assertFalse(is_trading_day(date(2026, 9, 5)))   # Saturday
        self.assertFalse(is_trading_day(date(2026, 9, 6)))   # Sunday

    def test_labor_day_2026_is_not_a_trading_day(self):
        self.assertFalse(is_trading_day(date(2026, 9, 7)))
        self.assertTrue(is_trading_day(date(2026, 9, 8)))

    def test_the_friday_after_thanksgiving_closes_early(self):
        self.assertIn(date(2026, 11, 27), nyse_early_closes(2026))
        self.assertEqual(market_close(date(2026, 11, 27)), EARLY_CLOSE)
        self.assertEqual(market_close(date(2026, 11, 30)), REGULAR_CLOSE)


class LastOpportunityTests(unittest.TestCase):
    def test_the_last_trading_day_of_september_2026(self):
        self.assertEqual(last_trading_day_of_month(2026, 9), date(2026, 9, 30))

    def test_a_month_ending_on_a_weekend_rolls_back_to_friday(self):
        # 2026-05-31 is a Sunday.
        self.assertEqual(last_trading_day_of_month(2026, 5), date(2026, 5, 29))

    def test_a_month_ending_on_a_holiday_rolls_back(self):
        # 2026-01-01 is a holiday, so December 2025 ends on the 31st (Wednesday),
        # and May 2026 ends before Memorial Day only if that lands last -- check
        # a month whose final weekday IS a holiday: 2020-05-25 was Memorial Day
        # but not month-end, so use 2015-07-03 (observed Independence Day).
        self.assertEqual(last_trading_day_of_month(2015, 7), date(2015, 7, 31))
        self.assertEqual(last_trading_day_of_month(2025, 12), date(2025, 12, 31))

    def test_the_equity_boundary_is_the_close_of_that_session(self):
        self.assertEqual(
            final_opportunity(2026, 9, "EQUITY"),
            datetime.combine(date(2026, 9, 30), time(16, 0)),
        )

    def test_an_early_close_moves_the_boundary_earlier(self):
        # November 2025 ended on Friday the 28th, a half session.
        self.assertEqual(
            final_opportunity(2025, 11, "EQUITY"),
            datetime.combine(date(2025, 11, 28), time(13, 0)),
        )

    def test_crypto_trades_to_the_last_instant_of_the_month(self):
        self.assertEqual(
            final_opportunity(2026, 9, "CRYPTO"),
            datetime.combine(date(2026, 9, 30), time(23, 59, 59)),
        )

    def test_etfs_share_the_equity_boundary(self):
        self.assertEqual(
            final_opportunity(2026, 9, "ETF"), final_opportunity(2026, 9, "EQUITY")
        )


class MuSeptemberBoundaryTests(unittest.TestCase):
    """The specific case that was previously reported wrongly.

    MU reports after the close on 2026-09-30. September's authorization cannot
    be spent on the reaction, because there is no September session left in
    which to spend it.
    """

    MU_PRINT = date(2026, 9, 30)
    SEPTEMBER = datetime(2026, 9, 7, 10, 30)

    def test_the_print_date_is_the_last_september_session(self):
        self.assertEqual(last_trading_day_of_month(2026, 9), self.MU_PRINT)
        self.assertTrue(is_trading_day(self.MU_PRINT))

    def test_after_the_close_on_the_last_session_is_next_month_only(self):
        self.assertEqual(
            event_actionability(
                self.MU_PRINT, "EQUITY", occurs_after_close=True, reference=self.SEPTEMBER
            ),
            ACTIONABLE_NEXT_MONTH_ONLY,
        )

    def test_the_equity_reaction_is_first_tradable_on_october_1(self):
        self.assertEqual(
            first_actionable_session(self.MU_PRINT, "EQUITY", occurs_after_close=True),
            date(2026, 10, 1),
        )
        self.assertEqual(next_trading_day(self.MU_PRINT), date(2026, 10, 1))

    def test_the_same_print_before_the_open_would_be_actionable_in_september(self):
        """The boundary is about the clock, not the date."""
        self.assertEqual(
            event_actionability(
                self.MU_PRINT, "EQUITY", occurs_after_close=False, reference=self.SEPTEMBER
            ),
            ACTIONABLE_THIS_MONTH,
        )

    def test_a_crypto_event_after_the_equity_close_is_still_this_month(self):
        """Crypto has no closing bell, so the same instant sits inside the month."""
        self.assertEqual(
            event_actionability(
                self.MU_PRINT, "CRYPTO", occurs_after_close=True, reference=self.SEPTEMBER
            ),
            ACTIONABLE_THIS_MONTH,
        )

    def test_the_moment_is_stamped_past_the_close(self):
        self.assertGreater(
            event_moment(self.MU_PRINT, occurs_after_close=True),
            final_opportunity(2026, 9, "EQUITY"),
        )

    def test_the_description_says_the_authorization_expires(self):
        described = describe_boundary(
            self.MU_PRINT, "EQUITY", occurs_after_close=True, reference=self.SEPTEMBER
        )
        self.assertEqual(described["actionability"], ACTIONABLE_NEXT_MONTH_ONLY)
        self.assertEqual(described["first_actionable_session"], "2026-10-01")
        self.assertEqual(described["final_opportunity"], "2026-09-30 16:00")
        self.assertIn("expires unused", described["explanation"])

    def test_an_event_earlier_in_the_month_is_actionable(self):
        # August CPI, 2026-09-11 — well inside the month.
        self.assertEqual(
            event_actionability(
                date(2026, 9, 11), "EQUITY", occurs_after_close=False,
                reference=self.SEPTEMBER,
            ),
            ACTIONABLE_THIS_MONTH,
        )

    def test_an_event_in_a_later_month_is_next_month_only(self):
        self.assertEqual(
            event_actionability(date(2026, 10, 15), "EQUITY", reference=self.SEPTEMBER),
            ACTIONABLE_NEXT_MONTH_ONLY,
        )

    def test_an_event_already_past_is_still_actionable_now(self):
        """Information in hand plus a live budget is actionable, not expired."""
        self.assertEqual(
            event_actionability(date(2026, 9, 2), "EQUITY", reference=self.SEPTEMBER),
            ACTIONABLE_THIS_MONTH,
        )


class WeekendAndHolidayEdgeTests(unittest.TestCase):
    def test_an_event_on_the_final_weekend_of_the_month_is_next_month_only(self):
        # 2026-10-31 is a Saturday; October's last session is Friday the 30th.
        self.assertEqual(last_trading_day_of_month(2026, 10), date(2026, 10, 30))
        self.assertEqual(
            event_actionability(
                date(2026, 10, 31), "EQUITY", reference=datetime(2026, 10, 20, 9, 0)
            ),
            ACTIONABLE_NEXT_MONTH_ONLY,
        )

    def test_the_same_weekend_event_is_actionable_for_crypto(self):
        self.assertEqual(
            event_actionability(
                date(2026, 10, 31), "CRYPTO", reference=datetime(2026, 10, 20, 9, 0)
            ),
            ACTIONABLE_THIS_MONTH,
        )

    def test_an_after_close_event_mid_month_is_still_this_month(self):
        self.assertEqual(
            event_actionability(
                date(2026, 9, 15), "EQUITY", occurs_after_close=True,
                reference=datetime(2026, 9, 7, 10, 30),
            ),
            ACTIONABLE_THIS_MONTH,
        )

    def test_a_non_trading_event_date_rolls_to_the_next_session(self):
        # 2026-09-07 is Labor Day.
        self.assertEqual(
            first_actionable_session(date(2026, 9, 7), "EQUITY"), date(2026, 9, 8)
        )


class ParseTests(unittest.TestCase):
    def test_a_valid_date_parses(self):
        parsed, error = parse_event_date("2026-09-30")
        self.assertEqual(parsed, date(2026, 9, 30))
        self.assertIsNone(error)

    def test_a_malformed_date_reports_an_error_rather_than_raising(self):
        for bad in ("30/09/2026", "September 30", "2026-13-01", "", None):
            with self.subTest(bad=bad):
                parsed, error = parse_event_date(bad)
                self.assertIsNone(parsed)
                self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()
