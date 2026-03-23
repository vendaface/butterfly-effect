"""
Unit tests for forecast.py

Run with:
    python -m pytest test_forecast.py -v
  or
    python -m unittest test_forecast -v
"""

import unittest
from datetime import date, timedelta
from unittest.mock import patch

from forecast import (
    _dedup_events,
    _next_dates_for_recurring,
    _prev_business_day,
    build_forecast,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(freq: str, base: str, amount: float = -100.0, name: str = "Test") -> dict:
    """Build a minimal Monarch-style recurring item."""
    return {"frequency": freq, "baseDate": base, "amount": amount, "name": name}


def _cal(d: date, amount: float, desc: str = "Cal Event") -> dict:
    return {"date": d, "description": desc, "amount": amount, "source": "calendar"}


# Fixed 'today' used across tests that need a stable anchor.
_TODAY = date(2026, 3, 22)
_HORIZON = _TODAY + timedelta(days=45)


# ---------------------------------------------------------------------------
# _prev_business_day
# ---------------------------------------------------------------------------

class TestPrevBusinessDay(unittest.TestCase):

    def test_skips_saturday(self):
        # 2026-03-20 is a Friday. Going back 1 bday from Monday 2026-03-23 skips weekend.
        monday = date(2026, 3, 23)
        self.assertEqual(_prev_business_day(monday, 1), date(2026, 3, 20))

    def test_skips_full_weekend(self):
        # 3 bdays before 2026-03-25 (Wednesday): Wed→Tue→Mon→Fri
        wed = date(2026, 3, 25)
        self.assertEqual(_prev_business_day(wed, 3), date(2026, 3, 20))

    def test_zero_steps(self):
        d = date(2026, 3, 20)
        self.assertEqual(_prev_business_day(d, 0), d)

    def test_multi_week_span(self):
        # 5 bdays back from Friday 2026-03-27 = Friday 2026-03-20
        fri = date(2026, 3, 27)
        self.assertEqual(_prev_business_day(fri, 5), date(2026, 3, 20))


# ---------------------------------------------------------------------------
# _next_dates_for_recurring
# ---------------------------------------------------------------------------

class TestNextDatesRecurring(unittest.TestCase):

    # ── WEEKLY ───────────────────────────────────────────────────────────────

    def test_weekly_multiple_hits(self):
        # Anchor 2026-03-01 (Sunday). From today 2026-03-22, first hit >= today:
        # 03-01 + 3wk = 03-22 → in window; 03-29, 04-05, ... up to horizon 05-06
        anchor = "2026-03-01"
        item = _item("WEEKLY", anchor)
        dates = _next_dates_for_recurring(item, _TODAY, _HORIZON)
        self.assertGreater(len(dates), 1)
        for d in dates:
            self.assertGreaterEqual(d, _TODAY)
            self.assertLessEqual(d, _HORIZON)
        # Consecutive dates should be 7 days apart
        for a, b in zip(dates, dates[1:]):
            self.assertEqual((b - a).days, 7)

    def test_weekly_anchor_before_window(self):
        dates = _next_dates_for_recurring(
            _item("WEEKLY", "2020-01-06"), _TODAY, _HORIZON
        )
        self.assertTrue(all(_TODAY <= d <= _HORIZON for d in dates))

    # ── BIWEEKLY ─────────────────────────────────────────────────────────────

    def test_biweekly_spacing(self):
        dates = _next_dates_for_recurring(
            _item("BIWEEKLY", "2026-03-01"), _TODAY, _HORIZON
        )
        self.assertGreater(len(dates), 0)
        for a, b in zip(dates, dates[1:]):
            self.assertEqual((b - a).days, 14)

    # ── MONTHLY ──────────────────────────────────────────────────────────────

    def test_monthly_basic(self):
        # Anchor on the 5th; first hit >= 2026-03-22 should be 2026-04-05
        dates = _next_dates_for_recurring(
            _item("MONTHLY", "2026-02-05"), _TODAY, _HORIZON
        )
        self.assertIn(date(2026, 4, 5), dates)

    def test_monthly_today_exact_hit(self):
        # Anchor on the 22nd → today itself should be included
        dates = _next_dates_for_recurring(
            _item("MONTHLY", "2026-02-22"), _TODAY, _HORIZON
        )
        self.assertIn(_TODAY, dates)

    def test_monthly_31st_short_month(self):
        # Anchor on Jan 31; advancement through Feb clamps to Feb 28 (non-leap year).
        # After clamping, the engine carries the reduced day forward: Mar becomes 28,
        # not 31. This is a known characteristic — the anchor "slips" permanently
        # once it passes through a short month.
        today_jan = date(2026, 1, 15)
        horizon_mar = date(2026, 3, 31)
        dates = _next_dates_for_recurring(
            _item("MONTHLY", "2025-12-31"), today_jan, horizon_mar
        )
        self.assertIn(date(2026, 1, 31), dates)
        self.assertIn(date(2026, 2, 28), dates)
        # March inherits the clamped day (28), not the original anchor day (31)
        self.assertIn(date(2026, 3, 28), dates)
        self.assertNotIn(date(2026, 3, 31), dates)

    # ── TWICE_A_MONTH ─────────────────────────────────────────────────────────

    def test_twice_a_month_spacing(self):
        dates = _next_dates_for_recurring(
            _item("TWICE_A_MONTH", "2026-03-01"), _TODAY, _HORIZON
        )
        self.assertGreater(len(dates), 0)
        for a, b in zip(dates, dates[1:]):
            self.assertEqual((b - a).days, 15)

    # ── QUARTERLY ─────────────────────────────────────────────────────────────

    def test_quarterly_single_hit(self):
        # Quarterly from 2026-01-01 → next hit >= today (2026-03-22) is 2026-04-01
        dates = _next_dates_for_recurring(
            _item("QUARTERLY", "2026-01-01"), _TODAY, _HORIZON
        )
        self.assertEqual(len(dates), 1)
        self.assertEqual(dates[0], date(2026, 4, 1))

    def test_quarterly_no_hit_in_window(self):
        # Quarterly from 2026-03-20 → next is 2026-06-20, beyond 45-day horizon
        dates = _next_dates_for_recurring(
            _item("QUARTERLY", "2026-03-20"), _TODAY, _HORIZON
        )
        self.assertEqual(dates, [])

    # ── BIMONTHLY ─────────────────────────────────────────────────────────────

    def test_bimonthly_spacing(self):
        # Anchor 2026-01-15 → hits: 2026-03-15, 2026-05-15 ...
        today_early = date(2026, 3, 1)
        horizon_may = date(2026, 5, 31)
        dates = _next_dates_for_recurring(
            _item("BIMONTHLY", "2026-01-15"), today_early, horizon_may
        )
        self.assertIn(date(2026, 3, 15), dates)
        self.assertIn(date(2026, 5, 15), dates)

    # ── YEARLY ────────────────────────────────────────────────────────────────

    def test_yearly_hit(self):
        # Anchor 2026-04-01 → hits today's window (45 days out)
        dates = _next_dates_for_recurring(
            _item("YEARLY", "2025-04-01"), _TODAY, _HORIZON
        )
        self.assertEqual(dates, [date(2026, 4, 1)])

    def test_yearly_no_hit(self):
        # Anchor far future; next occurrence is beyond horizon
        dates = _next_dates_for_recurring(
            _item("YEARLY", "2026-07-01"), _TODAY, _HORIZON
        )
        self.assertEqual(dates, [])

    # ── EDGE CASES ────────────────────────────────────────────────────────────

    def test_no_base_date(self):
        item = {"frequency": "MONTHLY", "amount": -100, "name": "Ghost"}
        dates = _next_dates_for_recurring(item, _TODAY, _HORIZON)
        self.assertEqual(dates, [])

    def test_unknown_frequency_in_window(self):
        # Unknown freq → use anchor once if it falls in the window
        anchor_date = _TODAY + timedelta(days=5)
        item = _item("FORTNIGHTLY", anchor_date.isoformat())
        dates = _next_dates_for_recurring(item, _TODAY, _HORIZON)
        self.assertEqual(dates, [anchor_date])

    def test_unknown_frequency_outside_window(self):
        item = _item("FORTNIGHTLY", "2025-01-01")
        dates = _next_dates_for_recurring(item, _TODAY, _HORIZON)
        self.assertEqual(dates, [])


# ---------------------------------------------------------------------------
# _dedup_events
# ---------------------------------------------------------------------------

class TestDedupEvents(unittest.TestCase):

    def _monarch(self, d: date, amt: float, desc: str = "Rent") -> dict:
        return {"date": d, "description": desc, "amount": amt, "source": "monarch"}

    def test_exact_duplicate_excluded(self):
        m = self._monarch(date(2026, 4, 1), -1500.0, "Rent")
        c = _cal(date(2026, 4, 1), -1500.0, "Rent Payment")
        result = _dedup_events([m], [c])
        # Calendar event is a dup; only Monarch event survives
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "monarch")

    def test_amount_within_tolerance_excluded(self):
        m = self._monarch(date(2026, 4, 1), -1000.0)
        c = _cal(date(2026, 4, 1), -1050.0)   # 5% difference < 10% tolerance
        result = _dedup_events([m], [c])
        self.assertEqual(len(result), 1)

    def test_amount_outside_tolerance_included(self):
        m = self._monarch(date(2026, 4, 1), -1000.0)
        c = _cal(date(2026, 4, 1), -1200.0)   # 20% difference > 10% tolerance
        result = _dedup_events([m], [c])
        self.assertEqual(len(result), 2)

    def test_opposite_sign_included(self):
        # Same absolute amount but one is income, one is expense → not a dup
        m = self._monarch(date(2026, 4, 1), -1000.0)
        c = _cal(date(2026, 4, 1), 1000.0)
        result = _dedup_events([m], [c])
        self.assertEqual(len(result), 2)

    def test_date_within_tolerance_excluded(self):
        m = self._monarch(date(2026, 4, 1), -500.0)
        c = _cal(date(2026, 4, 4), -500.0)   # 3 days apart, within 5-day tolerance
        result = _dedup_events([m], [c])
        self.assertEqual(len(result), 1)

    def test_date_outside_tolerance_included(self):
        m = self._monarch(date(2026, 4, 1), -500.0)
        c = _cal(date(2026, 4, 8), -500.0)   # 7 days apart, outside 5-day tolerance
        result = _dedup_events([m], [c])
        self.assertEqual(len(result), 2)

    def test_no_amount_calendar_always_included(self):
        m = self._monarch(date(2026, 4, 1), -500.0)
        c = _cal(date(2026, 4, 1), None, "Tuition (check amount)")  # no amount
        result = _dedup_events([m], [c])
        self.assertEqual(len(result), 2)

    def test_both_zero_amounts_excluded(self):
        m = self._monarch(date(2026, 4, 1), 0.0)
        c = _cal(date(2026, 4, 1), 0.0)
        result = _dedup_events([m], [c])
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# build_forecast
# ---------------------------------------------------------------------------

class TestBuildForecast(unittest.TestCase):

    def _run(self, balance: float, recurring=None, calendar=None, **kwargs):
        with patch("forecast.date") as mock_date:
            mock_date.today.return_value = _TODAY
            mock_date.fromisoformat = date.fromisoformat
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            return build_forecast(
                current_balance=balance,
                recurring_transactions=recurring or [],
                calendar_events=calendar or [],
                **kwargs,
            )

    def test_empty_forecast_balance_unchanged(self):
        result = self._run(5000.0)
        self.assertEqual(result["current_balance"], 5000.0)
        first = result["days"][0]
        self.assertEqual(first["balance"], 5000.0)
        self.assertEqual(first["events"], [])

    def test_horizon_length(self):
        result = self._run(1000.0, horizon_days=30)
        # Should have 31 days (today inclusive through today+30)
        self.assertEqual(len(result["days"]), 31)

    def test_single_expense_reduces_balance(self):
        recurring = [_item("MONTHLY", _TODAY.isoformat(), -200.0, "Netflix")]
        result = self._run(1000.0, recurring=recurring)
        # Today's balance should be 1000 - 200 = 800
        today_day = result["days"][0]
        self.assertEqual(today_day["balance"], 800.0)
        self.assertEqual(len(today_day["events"]), 1)
        self.assertEqual(today_day["events"][0]["description"], "Netflix")

    def test_income_increases_balance(self):
        recurring = [_item("MONTHLY", _TODAY.isoformat(), 3000.0, "Paycheck")]
        result = self._run(500.0, recurring=recurring)
        today_day = result["days"][0]
        self.assertEqual(today_day["balance"], 3500.0)

    def test_below_threshold_flag(self):
        # Balance starts at 300, threshold=500 → today immediately below
        result = self._run(300.0, buffer_threshold=500.0)
        self.assertTrue(result["days"][0]["below_threshold"])

    def test_above_threshold_no_flag(self):
        result = self._run(1000.0, buffer_threshold=500.0)
        self.assertFalse(result["days"][0]["below_threshold"])

    def test_transfer_recommendation_generated(self):
        # Put a large expense tomorrow, starting balance just above threshold
        tomorrow = _TODAY + timedelta(days=1)
        recurring = [_item("MONTHLY", tomorrow.isoformat(), -2000.0, "Mortgage")]
        result = self._run(600.0, recurring=recurring, buffer_threshold=500.0)
        recs = result["transfer_recommendations"]
        self.assertGreater(len(recs), 0)
        rec = recs[0]
        self.assertIn("transfer_by", rec)
        self.assertIn("amount", rec)
        self.assertGreater(rec["amount"], 0)

    def test_no_recommendation_when_always_above(self):
        result = self._run(50000.0, buffer_threshold=500.0)
        self.assertEqual(result["transfer_recommendations"], [])

    def test_payment_skip_removes_event(self):
        # Monthly payment anchored on today; skip it for this month
        recurring = [_item("MONTHLY", _TODAY.isoformat(), -500.0, "Brown University")]
        skips = [{"name": "Brown University", "month": _TODAY.isoformat()[:7]}]
        result = self._run(1000.0, recurring=recurring, payment_skips=skips)
        today_day = result["days"][0]
        self.assertEqual(today_day["events"], [])
        self.assertEqual(today_day["balance"], 1000.0)

    def test_payment_monthly_amount_override(self):
        # Monthly payment normally -500; override to -300 for this occurrence
        recurring = [_item("MONTHLY", _TODAY.isoformat(), -500.0, "Apple Card")]
        overrides = [{"name": "Apple Card", "date": _TODAY.isoformat(), "amount": -300.0}]
        result = self._run(1000.0, recurring=recurring, payment_monthly_amounts=overrides)
        today_day = result["days"][0]
        self.assertEqual(today_day["balance"], 700.0)

    def test_calendar_event_counted(self):
        cal = [_cal(_TODAY, -250.0, "Tuition")]
        result = self._run(1000.0, calendar=cal)
        today_day = result["days"][0]
        self.assertEqual(today_day["balance"], 750.0)

    def test_calendar_duplicate_deduped(self):
        # Monarch has -500 rent on the 1st; calendar has the same near-match
        first = _TODAY.replace(day=1) if _TODAY.day > 1 else _TODAY + timedelta(days=31 - _TODAY.day + 1)
        # Simpler: put anchor in the future so it's fresh
        future = _TODAY + timedelta(days=10)
        recurring = [_item("MONTHLY", future.isoformat(), -500.0, "Rent")]
        cal = [_cal(future, -490.0, "Rent Check")]  # within 10% amount & 0 day tolerance
        result = self._run(5000.0, recurring=recurring, calendar=cal)
        future_day = next(d for d in result["days"] if d["date"] == future.isoformat())
        # Only one event (monarch), not two
        self.assertEqual(len(future_day["events"]), 1)


if __name__ == "__main__":
    unittest.main()
