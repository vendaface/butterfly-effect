"""
Google Calendar client using the secret iCal URL (no OAuth required).

Supports both one-off events and recurring events (RRULE). Recurring events
are expanded using dateutil.rrule so future occurrences within the forecast
horizon are discovered even when DTSTART is in the past.

Event title convention for the "Bills & Transfers" calendar:
  "$3972.21 - Roundpoint Mortgage"  →  amount = -3972.21 (bare number treated as debit)
  "Mortgage -3200"                  →  amount = -3200.00
  "Savings Transfer +2000"          →  amount = +2000.00
  "AMEX Bill pay"                   →  amount = None (flagged in UI)

Standalone usage:
  python calendar_client.py
"""

import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml
from dateutil.rrule import rrulestr
from icalendar import Calendar


def _load_ics_url() -> str:
    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        raise RuntimeError("config.yaml not found.")
    config = yaml.safe_load(config_path.read_text())
    url = (config.get("calendar", {}).get("ics_url", "") or "").strip()
    if not url or "PASTE_YOUR" in url:
        raise RuntimeError(
            "Set calendar.ics_url in config.yaml.\n"
            "Google Calendar → Settings → [Bills & Transfers] → 'Secret address in iCal format'"
        )
    return url


def _parse_amount(title: str) -> Optional[float]:
    """
    Extract the dollar amount from an event title. Returns negative for debits.

    Handles these common formats:
      "$613.26 - Auto Loan"         →  -613.26   (leading $ with no sign → debit)
      "Mortgage -3200"              →  -3200.00  (trailing signed number)
      "Savings Transfer +2000"      →  +2000.00
      "Apple Card Payment"          →  None      (no number → flagged)
    """
    # Pattern 1a: leading "[+-]$amount description" (e.g. "-$2000 Girls' 529s", "+$3928 Payday")
    match = re.match(r"^([+-])\$?([\d,]+(?:\.\d{1,2})?)(?:\s|$)", title)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        return sign * float(match.group(2).replace(",", ""))

    # Pattern 1b: leading "$amount - description" (e.g. "$613.26 - Auto Loan")
    match = re.match(r"^\$?([\d,]+(?:\.\d{1,2})?)\s*[-–]", title)
    if match:
        return -float(match.group(1).replace(",", ""))

    # Pattern 2: trailing signed number (e.g. "Mortgage -3200" or "Transfer +2000")
    match = re.search(r"([+-])\s*\$?([\d,]+(?:\.\d{1,2})?)\s*$", title)
    if match:
        sign = 1 if match.group(1) == "+" else -1
        return sign * float(match.group(2).replace(",", ""))

    # No sign found — don't assume debit. Show as $? so the user can add an explicit sign.
    return None


def _expand_occurrences(component, today: date, horizon: date) -> list[date]:
    """
    Return all occurrence dates for a VEVENT (single or recurring) in [today, horizon].
    Uses dateutil.rrule to expand RRULE recurrences.
    """
    dt_val = component.get("DTSTART")
    if not dt_val:
        return []

    dtstart = dt_val.dt

    # Normalize to a timezone-naive datetime for dateutil
    if isinstance(dtstart, datetime):
        if dtstart.tzinfo is not None:
            dtstart = dtstart.astimezone(timezone.utc).replace(tzinfo=None)
        dtstart_dt = dtstart
    else:
        # date-only — convert to midnight datetime
        dtstart_dt = datetime(dtstart.year, dtstart.month, dtstart.day)

    rrule_val = component.get("RRULE")

    today_dt = datetime(today.year, today.month, today.day)
    horizon_dt = datetime(horizon.year, horizon.month, horizon.day)

    if rrule_val:
        rrule_str = rrule_val.to_ical().decode()
        try:
            rule = rrulestr(f"RRULE:{rrule_str}", dtstart=dtstart_dt, ignoretz=True)
            return [occ.date() for occ in rule.between(today_dt, horizon_dt, inc=True)]
        except Exception:
            pass  # fall through to single-occurrence check

    # Single (non-recurring) event
    base = dtstart_dt.date()
    return [base] if today <= base <= horizon else []


def get_events(horizon_days: int = 45) -> list[dict]:
    """
    Fetch and parse upcoming calendar events within horizon_days from today.

    Returns list of:
      {
        "date": date,
        "description": str,   # cleaned title (amount suffix stripped)
        "amount": float | None,   # None → unparseable, shown as $? in UI
        "source": "calendar",
      }

    Recurring events (RRULE) are fully expanded, so future occurrences are
    returned even if the original DTSTART is in the past.
    """
    url = _load_ics_url()

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to fetch calendar: {e}")

    cal = Calendar.from_ical(response.content)

    today = date.today()
    horizon = today + timedelta(days=horizon_days)

    events = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        title = str(component.get("SUMMARY", "")).strip()
        amount = _parse_amount(title)

        # Strip the amount portion from the display title.
        # Handles leading formats: "-$2000 Girls' 529s", "+$3928 Payday", "$613.26 - Auto Loan"
        clean = re.sub(r"^[+-]?\$?[\d,]+(?:\.\d{1,2})?\s*(?:[-–]\s*|\s+)", "", title).strip()
        # Also strip trailing formats: "Mortgage -3200", "Transfer +$3000"
        clean = re.sub(r"\s*[+-]\s*\$?[\d,]+(?:\.\d{1,2})?\s*$", "", clean).strip()
        description = clean or title

        for occurrence_date in _expand_occurrences(component, today, horizon):
            events.append({
                "date": occurrence_date,
                "description": description,
                "amount": amount,
                "source": "calendar",
            })

    # Deduplicate within calendar (handles duplicate VEVENTs landing on the same date)
    seen: set = set()
    deduped = []
    for e in events:
        key = (e["date"], e["description"], e["amount"])
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    events = deduped

    events.sort(key=lambda e: e["date"])
    return events


if __name__ == "__main__":
    config_path = Path(__file__).parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    horizon = config.get("forecast", {}).get("horizon_days", 45)

    try:
        events = get_events(horizon)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"\nUpcoming calendar events (next {horizon} days): {len(events)}")
    if not events:
        print("  (none)")
    for e in events:
        amt = f"${e['amount']:+,.2f}" if e["amount"] is not None else "    $?"
        print(f"  {e['date']}  {amt:<12}  {e['description']}")
