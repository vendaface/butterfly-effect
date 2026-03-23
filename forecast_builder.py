"""
forecast_builder.py — forecast assembly and cache management.

Owns:
  - In-memory cache dicts (_cache, _monarch_raw)
  - Cache-clear helpers (_clear_forecast_cache, _clear_all_cache)
  - AI prediction helpers (_insights_are_fresh, _matches_recurring, _load_predicted_events)
  - Scenario expansion (_expand_scenario_events)
  - User-friendly error translation (_friendly_error)
  - Core forecast builder (_get_forecast_data) — orchestrates Monarch fetch,
    overrides, calendar, AI/scenario injection, and the forecast engine call
"""

import calendar
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import calendar_client
import forecast as forecast_engine
import monarch_client

from storage import (
    _load_insights,
    _load_payment_day_overrides,
    _load_payment_monthly_amounts,
    _load_payment_overrides,
    _load_payment_skips,
    _load_scenarios,
)

# ── In-memory caches ──────────────────────────────────────────────────────────

_cache: dict = {}         # computed forecast — cleared by settings changes
_monarch_raw: dict = {}   # raw Monarch data (balance, transactions, recurring)
                          # survives settings changes; only reset by /refresh or account change


def _clear_forecast_cache() -> None:
    """Clear computed forecast only. Raw Monarch data kept for fast recompute."""
    _cache.clear()


def _clear_all_cache() -> None:
    """Full reset — clears both forecast and raw Monarch data, forcing a Playwright re-fetch."""
    _cache.clear()
    _monarch_raw.clear()


# ── AI prediction helpers ─────────────────────────────────────────────────────

def _insights_are_fresh(config: dict) -> bool:
    """Return True if insights.json exists and is within the max-age window."""
    insights = _load_insights()
    if not insights:
        return False
    generated_at = insights.get("generated_at", "")
    if not generated_at:
        return False
    try:
        age = datetime.now() - datetime.fromisoformat(generated_at)
        max_age_hours = config.get("ai", {}).get("insights_max_age_hours", 26)
        return age.total_seconds() < max_age_hours * 3600
    except Exception:
        return False


def _matches_recurring(desc: str, amount: float, recurring: list[dict]) -> bool:
    """Return True if a predicted event likely duplicates a Monarch recurring item.

    Requires BOTH conditions to avoid false positives:
      1. Amounts within $1 (tight tolerance — two different $500 bills should not collide)
      2. At least one significant word (≥4 chars) shared between the predicted description
         and the Monarch recurring item name.
    """
    desc_words = {w.lower() for w in desc.split() if len(w) >= 4}
    for r in recurring:
        r_amt = r.get("amount")
        if r_amt is None:
            continue
        if abs(float(r_amt) - amount) > 1.0:
            continue
        r_name = r.get("name") or r.get("description") or ""
        r_words = {w.lower() for w in r_name.split() if len(w) >= 4}
        if desc_words & r_words:
            return True
    return False


def _load_predicted_events(config: dict) -> list[dict]:
    """Load AI-predicted expenses from insights.json when fresh."""
    if not _insights_are_fresh(config):
        return []
    insights = _load_insights()
    if not insights:
        return []
    return insights.get("predicted_expenses", [])


# ── Scenario expansion ────────────────────────────────────────────────────────

def _expand_scenario_events(scenarios: list[dict], horizon_days: int) -> list[dict]:
    """Expand scenario events into individual occurrences within the forecast horizon.

    One-time scenarios pass through unchanged. Recurring scenarios are fanned out
    into one event per occurrence using the same engine as Monarch recurring items.
    """
    today = date.today()
    horizon = today + timedelta(days=horizon_days)
    result = []
    for s in scenarios:
        freq = s.get("frequency") or "one-time"
        if freq == "one-time":
            result.append({**s, "source": "scenario"})
        else:
            item = {"frequency": freq, "baseDate": s["date"]}
            dates = forecast_engine._next_dates_for_recurring(item, today, horizon)
            for d in dates:
                result.append({**s, "date": d.isoformat(), "source": "scenario"})
    return result


# ── Error translation ─────────────────────────────────────────────────────────

def _friendly_error(e: Exception) -> str:
    """Convert common internal exceptions to user-friendly messages."""
    msg = str(e)
    if "checking_account_id" in msg or "Account ID" in msg or "No primary account" in msg:
        return (
            "No primary account selected. "
            "Go to Settings → Monarch Connection, click 'Connect to Monarch', "
            "select your checking account, and save."
        )
    if "login" in msg.lower() or "session" in msg.lower():
        return (
            "Monarch session expired or login failed. "
            "The app will open a browser window to re-authenticate on the next refresh."
        )
    if any(k in msg for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY")) \
            or "anthropic" in msg.lower():
        return (
            "API key is not configured. "
            "Go to Settings → AI Insights, select your AI provider, and add your key."
        )
    return f"Forecast error: {msg}"


# ── Core forecast builder ─────────────────────────────────────────────────────

def _get_forecast_data(config: dict) -> dict:
    """Build and cache the complete forecast.

    Fast path: returns _cache immediately if already populated.
    Slow path: fetches from Monarch via Playwright (30–60 s), applies all
    overrides/exclusions, loads AI predictions + scenarios, calls the forecast
    engine, caches and returns the result.
    """
    # Demo mode: serve pre-built forecast from disk so screenshots work without Monarch.
    # Enable by setting  demo_mode: true  in config.yaml.
    if config.get("demo_mode"):
        _demo = Path(__file__).parent / "demo" / "forecast_data.json"
        if _demo.exists():
            data = json.loads(_demo.read_text())
            _cache["data"] = data
            _cache["ts"] = time.time()
            return data

    if _cache:
        return _cache

    account_id = config["monarch"]["checking_account_id"]
    if not account_id or account_id == "PASTE_ACCOUNT_ID_HERE":
        raise RuntimeError(
            "Set monarch.checking_account_id in config.yaml. "
            "Run: python monarch_client.py --list-accounts"
        )

    horizon = config["forecast"]["horizon_days"]
    buffer  = config["forecast"]["buffer_threshold"]

    if _monarch_raw:
        # Fast path — raw Monarch data cached; skip the slow Playwright fetch entirely.
        # Exclusions, overrides, and scenario/AI events are re-applied fresh below.
        current_balance = _monarch_raw["balance"]
        transactions    = _monarch_raw["transactions"]
        base_recurring  = _monarch_raw["recurring"]
    else:
        # Slow path — fetch from Monarch via Playwright (30–60 s)
        current_balance, transactions, base_recurring = monarch_client.get_data(
            account_id, history_days=horizon
        )
        _monarch_raw.update({
            "balance":      current_balance,
            "transactions": transactions,
            "recurring":    base_recurring,
        })

    # Work from a copy so overrides/exclusions don't mutate the cached raw recurring list
    recurring = list(base_recurring)

    # Filter out recurring items the user has excluded (e.g. credit-card-side duplicates)
    exclude = {n.lower() for n in config.get("forecast", {}).get("exclude_recurring", [])}
    if exclude:
        recurring = [r for r in recurring if (r.get("name") or "").lower() not in exclude]

    # Apply user-specified payment amount overrides (e.g. known credit card statement balance)
    overrides = _load_payment_overrides()
    if overrides:
        patched = []
        for item in recurring:
            key = (item.get("name") or "").lower()
            if key in overrides:
                override_amt = float(overrides[key]["amount"])
                if abs(override_amt) < 0.01:
                    continue  # $0 override = suppress this payment from the forecast entirely
                item = dict(item)   # shallow copy — don't mutate Monarch data
                item["amount"] = override_amt
            patched.append(item)
        recurring = patched

    # Apply user-specified billing-day overrides (e.g. AMEX Gold always hits on the 16th)
    day_overrides = _load_payment_day_overrides()
    if day_overrides:
        day_patched = []
        for item in recurring:
            key = (item.get("name") or "").lower()
            if key in day_overrides:
                target_day = day_overrides[key]["day"]
                item = dict(item)  # shallow copy — don't mutate cached Monarch data
                base_raw = item.get("baseDate")
                if base_raw:
                    try:
                        bd = date.fromisoformat(str(base_raw)[:10])
                        last = calendar.monthrange(bd.year, bd.month)[1]
                        item["baseDate"] = bd.replace(day=min(target_day, last)).isoformat()
                    except Exception:
                        pass
            day_patched.append(item)
        recurring = day_patched

    # Fetch from Google Calendar (optional — set calendar.enabled: false in config to skip)
    cal_enabled = config.get("calendar", {}).get("enabled", True)
    if cal_enabled:
        try:
            cal_events = calendar_client.get_events(horizon_days=horizon)
        except RuntimeError as e:
            cal_events = []
            print(f"[WARN] Calendar fetch skipped: {e}")
    else:
        cal_events = []

    # Load AI-predicted events from insights.json (if fresh)
    predicted_events = _load_predicted_events(config)

    # Drop predicted events that duplicate a recurring item already covered by a payment
    # override. Claude sometimes predicts credit card payments that are already in Monarch
    # recurring — if the user has set an override for that card, the recurring item IS the
    # authoritative entry.
    if predicted_events and overrides:
        override_keywords = set(overrides.keys())   # already lowercase

        def _matches_override(desc: str) -> bool:
            desc_lower = desc.lower()
            return any(kw in desc_lower for kw in override_keywords)

        before = len(predicted_events)
        predicted_events = [p for p in predicted_events
                            if not _matches_override(p.get("description", ""))]
        dropped = before - len(predicted_events)
        if dropped:
            print(f"[INFO] Dropped {dropped} AI-predicted event(s) already covered by payment overrides")

    # Also drop predicted events that duplicate any Monarch recurring item
    # (e.g. AI predicts "Brown University tuition" which is already in Monarch as "Brown University")
    if predicted_events:
        before = len(predicted_events)
        predicted_events = [
            p for p in predicted_events
            if not _matches_recurring(
                p.get("description", ""),
                float(p.get("amount") or 0),
                recurring,
            )
        ]
        dropped = before - len(predicted_events)
        if dropped:
            print(f"[INFO] Dropped {dropped} AI-predicted event(s) already present in Monarch recurring")

    if predicted_events:
        print(f"[INFO] Injecting {len(predicted_events)} AI-predicted events into forecast")

    # Load user scenario events and merge with AI predictions.
    # Scenarios bypass the payment-override dedup filter above (they're intentional one-offs).
    scenario_events = _load_scenarios()
    if scenario_events:
        print(f"[INFO] Injecting {len(scenario_events)} scenario event(s) into forecast")
    scenario_injected = _expand_scenario_events(scenario_events, horizon)
    all_injected = (predicted_events or []) + scenario_injected

    # Store recurring list for Settings → View Recurring Items (before forecast build)
    _cache["_recurring_raw"] = recurring

    # Build forecast
    payment_skips           = _load_payment_skips()
    payment_monthly_amounts = _load_payment_monthly_amounts()
    result = forecast_engine.build_forecast(
        current_balance=current_balance,
        recurring_transactions=recurring,
        calendar_events=cal_events,
        predicted_events=all_injected or None,
        buffer_threshold=buffer,
        horizon_days=horizon,
        payment_skips=payment_skips or None,
        payment_monthly_amounts=payment_monthly_amounts or None,
    )
    result["refreshed_at"] = datetime.now().strftime("%A %b %-d, %Y at %-I:%M %p")
    result["horizon_days"] = horizon
    result["has_ai_predictions"] = bool(predicted_events)

    _cache.update(result)
    return _cache
