"""
Balance forecasting engine.

Combines:
  1. Monarch recurring transactions  → projected future payments
  2. Google Calendar events          → one-off items (tuition, planned transfers, etc.)

Returns a day-by-day balance projection and transfer recommendations.
"""

from datetime import date, timedelta
from typing import Optional


def _next_dates_for_recurring(item: dict, today: date, horizon: date) -> list[date]:
    """
    Determine which dates within [today, horizon] a recurring item will hit.
    Uses baseDate as the schedule anchor, then advances by frequency to find
    the first occurrence on or after today.
    """
    freq = (item.get("frequency") or "").upper()
    base_raw = item.get("baseDate") or (item.get("nextForecastedTransaction") or {}).get("date")
    if not base_raw:
        return []

    try:
        anchor = date.fromisoformat(str(base_raw)[:10])
    except ValueError:
        return []

    import calendar as _calendar

    def _advance_monthly(d: date) -> date:
        """Advance date by one calendar month."""
        month = d.month + 1
        year = d.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        try:
            return d.replace(year=year, month=month)
        except ValueError:
            last_day = _calendar.monthrange(year, month)[1]
            return d.replace(year=year, month=month, day=last_day)

    dates = []

    if freq in ("YEARLY", "ANNUALLY"):
        # Advance by years until >= today
        current = anchor
        while current < today:
            try:
                current = current.replace(year=current.year + 1)
            except ValueError:
                current = current.replace(year=current.year + 1, day=28)
        if current <= horizon:
            dates.append(current)
        return dates

    if freq == "QUARTERLY":
        # Advance by 3 months until >= today
        current = anchor
        while current < today:
            for _ in range(3):
                current = _advance_monthly(current)
        if current <= horizon:
            dates.append(current)
        return dates

    if freq == "BIMONTHLY":
        # Every 2 months — advance by 2 calendar months until >= today
        current = anchor
        while current < today:
            current = _advance_monthly(_advance_monthly(current))
        while current <= horizon:
            dates.append(current)
            current = _advance_monthly(_advance_monthly(current))
        return dates

    if freq == "MONTHLY":
        # Advance anchor to the first occurrence >= today
        current = anchor
        while current < today:
            current = _advance_monthly(current)
        while current <= horizon:
            dates.append(current)
            current = _advance_monthly(current)
        return dates

    if freq == "TWICE_A_MONTH":
        # Approximate: every ~15 days; advance to >= today first
        current = anchor
        while current < today:
            current += timedelta(days=15)
        while current <= horizon:
            dates.append(current)
            current += timedelta(days=15)
        return dates

    # Weekly / biweekly / fixed-day-step frequencies
    step_map = {
        "WEEKLY": timedelta(weeks=1),
        "BIWEEKLY": timedelta(weeks=2),
    }
    step = step_map.get(freq)
    if step is None:
        # Unknown frequency — use anchor once if it falls in window
        if today <= anchor <= horizon:
            return [anchor]
        return []

    # Advance anchor to first occurrence >= today efficiently
    current = anchor
    if current < today and step.days > 0:
        days_behind = (today - current).days
        periods = (days_behind + step.days - 1) // step.days
        current = current + step * periods

    while current <= horizon:
        if current >= today:
            dates.append(current)
        current += step

    return dates


def _amount_for_recurring(item: dict) -> Optional[float]:
    """
    Extract the signed amount from a Monarch recurring stream.
    Monarch amounts are already signed: negative = expense, positive = income.
    """
    amt = item.get("amount")
    if amt is None:
        return None
    return float(amt)


def _dedup_events(
    monarch_events: list[dict],
    calendar_events: list[dict],
    tolerance_days: int = 5,
    amount_tolerance_pct: float = 0.10,
) -> list[dict]:
    """
    Remove calendar events that are likely duplicates of Monarch recurring items.

    A calendar event is considered a duplicate of a Monarch event when:
      - Both have the same sign (both debits or both credits)
      - Their amounts are within amount_tolerance_pct of each other (default 10%)
      - Their dates are within tolerance_days of each other (default 5)

    The wider 10% tolerance handles cases where the same bill shows different
    amounts in Monarch vs the calendar (e.g. monthly fluctuations in insurance,
    utility, or mortgage interest allocation).
    """
    combined = list(monarch_events)
    for cal_evt in calendar_events:
        if cal_evt["amount"] is None:
            # No parseable amount — always include (shown as $? in UI)
            combined.append(cal_evt)
            continue
        is_dup = False
        cal_amt = cal_evt["amount"]
        for m_evt in monarch_events:
            if m_evt["amount"] is None:
                continue
            m_amt = m_evt["amount"]
            # Must have same sign (both negative or both positive)
            if (cal_amt < 0) != (m_amt < 0):
                continue
            larger = max(abs(m_amt), abs(cal_amt))
            same_amount = larger == 0 or abs(m_amt - cal_amt) / larger <= amount_tolerance_pct
            same_date = abs((m_evt["date"] - cal_evt["date"]).days) <= tolerance_days
            if same_amount and same_date:
                is_dup = True
                break
        if not is_dup:
            combined.append(cal_evt)
    return combined


def build_forecast(
    current_balance: float,
    recurring_transactions: list[dict],
    calendar_events: list[dict],
    predicted_events: list[dict] | None = None,
    buffer_threshold: float = 500.0,
    horizon_days: int = 45,
    payment_skips: list[dict] | None = None,
    payment_monthly_amounts: list[dict] | None = None,
) -> dict:
    """
    Build a day-by-day balance forecast.

    Returns:
    {
      "days": [
        {
          "date": "2026-03-20",
          "balance": 4200.00,
          "events": [{"description": ..., "amount": ..., "source": ...}],
          "below_threshold": bool,
        },
        ...
      ],
      "transfer_recommendations": [
        {
          "transfer_by": "2026-03-25",
          "amount": 1500.00,
          "reason": "Balance hits $-300 on 2026-03-28 (Apple Card -850)",
        },
        ...
      ],
      "current_balance": float,
      "buffer_threshold": float,
    }
    """
    today = date.today()
    horizon = today + timedelta(days=horizon_days)

    # Build Monarch recurring events within horizon
    monarch_events = []
    for item in recurring_transactions:
        dates = _next_dates_for_recurring(item, today, horizon)
        amt = _amount_for_recurring(item)
        name = item.get("name") or item.get("merchant", {}).get("name") or "Unknown"
        for d in dates:
            monarch_events.append({
                "date": d,
                "description": name,
                "amount": amt,
                "source": "monarch",
            })

    # Filter out per-month skips before the balance walk
    if payment_skips:
        skip_set = {(s["name"].lower(), s["month"]) for s in payment_skips}
        monarch_events = [
            e for e in monarch_events
            if (e["description"].lower(), e["date"].isoformat()[:7]) not in skip_set
        ]

    # Apply per-occurrence amount overrides (single-date edits)
    if payment_monthly_amounts:
        # New records use "date" (YYYY-MM-DD) for exact match; legacy use "month" (YYYY-MM)
        date_amt_map = {
            (s["name"].lower(), s["date"]): float(s["amount"])
            for s in payment_monthly_amounts if s.get("date")
        }
        month_amt_map = {
            (s["name"].lower(), s["month"]): float(s["amount"])
            for s in payment_monthly_amounts if s.get("month") and not s.get("date")
        }
        new_events = []
        for e in monarch_events:
            date_key  = (e["description"].lower(), e["date"].isoformat())
            month_key = (e["description"].lower(), e["date"].isoformat()[:7])
            if date_key in date_amt_map:
                new_events.append(dict(e, amount=date_amt_map[date_key]))
            elif month_key in month_amt_map:
                new_events.append(dict(e, amount=month_amt_map[month_key]))
            else:
                new_events.append(e)
        monarch_events = new_events

    # Deduplicate calendar events against Monarch recurring
    all_events = _dedup_events(monarch_events, calendar_events)

    # Inject AI-predicted events (not deduped — they represent additional spend)
    if predicted_events:
        for pe in predicted_events:
            if isinstance(pe.get("date"), str):
                try:
                    pe = dict(pe)
                    pe["date"] = date.fromisoformat(pe["date"])
                except ValueError:
                    continue
            if isinstance(pe.get("date"), date) and today <= pe["date"] <= horizon:
                all_events.append({
                    "date": pe["date"],
                    "description": pe.get("description", "AI predicted"),
                    "amount": pe.get("amount"),
                    "source": pe.get("source", "predicted"),
                    "scenario_id": pe.get("id"),
                })

    # Build day-by-day map
    event_by_day: dict[date, list[dict]] = {}
    for evt in all_events:
        event_by_day.setdefault(evt["date"], []).append(evt)

    # Walk forward
    days = []
    running_balance = current_balance
    current = today

    while current <= horizon:
        day_events = event_by_day.get(current, [])
        for evt in day_events:
            if evt["amount"] is not None:
                running_balance += evt["amount"]

        days.append({
            "date": current.isoformat(),
            "balance": round(running_balance, 2),
            "events": [
                {
                    "description": e["description"],
                    "amount": e["amount"],
                    "source": e["source"],
                    "scenario_id": e.get("scenario_id"),
                }
                for e in day_events
            ],
            "below_threshold": running_balance < buffer_threshold,
        })
        current += timedelta(days=1)

    # Generate transfer recommendations: find each contiguous "dip" below threshold
    recommendations = []
    in_dip = False
    dip_start_idx = None

    for i, day in enumerate(days):
        if day["below_threshold"] and not in_dip:
            in_dip = True
            dip_start_idx = i
        elif not day["below_threshold"] and in_dip:
            in_dip = False
            _add_recommendation(recommendations, days, dip_start_idx, i - 1, buffer_threshold)

    if in_dip:
        _add_recommendation(recommendations, days, dip_start_idx, len(days) - 1, buffer_threshold)

    return {
        "days": days,
        "transfer_recommendations": recommendations,
        "current_balance": round(current_balance, 2),
        "buffer_threshold": buffer_threshold,
    }


def _add_recommendation(recommendations, days, start_idx, end_idx, buffer_threshold):
    """Build one transfer recommendation for a dip window."""
    # Worst balance in the dip
    min_balance = min(days[i]["balance"] for i in range(start_idx, end_idx + 1))
    # Amount needed to keep balance at buffer_threshold throughout
    transfer_amount = round(buffer_threshold - min_balance, 2)
    if transfer_amount <= 0:
        return

    dip_date = days[start_idx]["date"]

    # Suggest transferring 3 business days before the dip starts (same-day fallback: dip date itself)
    transfer_by_date = date.fromisoformat(dip_date) - timedelta(days=3)
    if transfer_by_date < date.today():
        transfer_by_date = date.fromisoformat(dip_date)

    # Identify the triggering events on the dip start date
    triggers = [
        f"{e['description']} ({'+' if (e['amount'] or 0) > 0 else ''}${e['amount']:,.2f})"
        for e in days[start_idx]["events"]
        if e["amount"] is not None
    ]
    trigger_str = ", ".join(triggers) if triggers else "scheduled payments"

    recommendations.append({
        "transfer_by": transfer_by_date.isoformat(),
        "amount": transfer_amount,
        "reason": f"Balance drops to ${min_balance:,.2f} around {dip_date} due to {trigger_str}",
    })
