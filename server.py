"""
Balance Forecast — local Flask web app.
Run via: python server.py  (or ./run.sh)
"""

import json
import os
import re
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, url_for

import calendar_client
import forecast as forecast_engine
import monarch_client

load_dotenv()

app = Flask(__name__)

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_INSIGHTS_FILE = Path(__file__).parent / "insights.json"
_USER_CONTEXT_FILE = Path(__file__).parent / "user_context.md"
_PAYMENT_OVERRIDES_FILE = Path(__file__).parent / "payment_overrides.json"
_PAYMENT_SKIPS_FILE = Path(__file__).parent / "payment_skips.json"
_PAYMENT_MONTHLY_AMOUNTS_FILE = Path(__file__).parent / "payment_monthly_amounts.json"
_SCENARIOS_FILE = Path(__file__).parent / "scenarios.json"
_ENV_PATH = Path(__file__).parent / ".env"
_ACCOUNTS_CACHE_FILE = Path(__file__).parent / "monarch_accounts_cache.json"
_cache: dict = {}        # computed forecast — cleared by settings changes
_monarch_raw: dict = {}  # raw Monarch data (balance, transactions, recurring)
                         # survives settings changes; only reset by /refresh or account change


def _clear_forecast_cache():
    """Clear computed forecast only. Raw Monarch data kept for fast recompute."""
    _cache.clear()


def _clear_all_cache():
    """Full reset — clears both forecast and raw Monarch data, forcing a Playwright re-fetch."""
    _cache.clear()
    _monarch_raw.clear()

# Keys that must never be sent to the browser as plaintext
_SENSITIVE_ENV_KEYS = {
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
}

# AI analysis background-run state
_ai_running: bool = False
_ai_run_log: list = []

# Regex patterns for parsing user_context.md (new flat format and old section format)
_CORRECTION_LINE_RE = re.compile(
    r'^- \[(\d{4}-\d{2}-\d{2})\] \[(Correction|Known Fact|Note)\] (.+)$'
)
_OLD_DATED_LINE_RE = re.compile(r'^- \[(\d{4}-\d{2}-\d{2})\] (.+)$')


def _parse_corrections() -> list[dict]:
    """Parse user_context.md into a list of correction dicts.

    Supports both:
      New flat format:  - [YYYY-MM-DD] [Type] text
      Old section format: lines under ## Corrections / ## Known Facts / ## Notes
    """
    if not _USER_CONTEXT_FILE.exists():
        return []
    lines = _USER_CONTEXT_FILE.read_text().splitlines()
    results: list[dict] = []
    cur_type = "Correction"  # tracks section for old-format migration
    for i, raw in enumerate(lines):
        if raw.startswith("## Corrections"):
            cur_type = "Correction"; continue
        elif raw.startswith("## Known Facts"):
            cur_type = "Known Fact"; continue
        elif raw.startswith("## Notes"):
            cur_type = "Note"; continue
        elif raw.startswith("## "):
            cur_type = "Correction"; continue
        m = _CORRECTION_LINE_RE.match(raw)
        if m:
            results.append({"id": f"c{i}", "date": m.group(1),
                            "type": m.group(2), "text": m.group(3), "raw": raw})
            continue
        m2 = _OLD_DATED_LINE_RE.match(raw)
        if m2:
            results.append({"id": f"c{i}", "date": m2.group(1),
                            "type": cur_type, "text": m2.group(2), "raw": raw})
    return results


def _write_corrections(corrections: list[dict]) -> None:
    """Write corrections list back to user_context.md in new flat format."""
    file_lines = ["# AI Corrections", ""]
    for c in corrections:
        file_lines.append(f"- [{c['date']}] [{c['type']}] {c['text']}")
    _USER_CONTEXT_FILE.write_text("\n".join(file_lines) + "\n")


def _load_scenarios() -> list[dict]:
    """Load user-defined scenario events from scenarios.json."""
    if not _SCENARIOS_FILE.exists():
        return []
    try:
        return json.loads(_SCENARIOS_FILE.read_text())
    except Exception:
        return []


def _expand_scenario_events(scenarios: list[dict], horizon_days: int) -> list[dict]:
    """
    Expand scenario events into individual occurrences within the forecast horizon.
    One-time scenarios pass through unchanged. Recurring scenarios are fanned out
    into one event per occurrence using the same engine as Monarch recurring items.
    """
    from datetime import date as _date, timedelta as _timedelta
    import forecast as forecast_engine

    today = _date.today()
    horizon = today + _timedelta(days=horizon_days)
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


def _load_payment_overrides() -> dict:
    """Load {name_lower: {name, amount, note, updated}} from payment_overrides.json."""
    if not _PAYMENT_OVERRIDES_FILE.exists():
        return {}
    try:
        return json.loads(_PAYMENT_OVERRIDES_FILE.read_text())
    except Exception:
        return {}


def _load_payment_skips() -> list:
    """Load [{name, month, note}] from payment_skips.json."""
    if not _PAYMENT_SKIPS_FILE.exists():
        return []
    try:
        return json.loads(_PAYMENT_SKIPS_FILE.read_text())
    except Exception:
        return []


def _load_payment_monthly_amounts() -> list:
    """Load [{name, month, amount, note}] from payment_monthly_amounts.json."""
    if not _PAYMENT_MONTHLY_AMOUNTS_FILE.exists():
        return []
    try:
        return json.loads(_PAYMENT_MONTHLY_AMOUNTS_FILE.read_text())
    except Exception:
        return []


def _env_key_status(key: str) -> str:
    """Return 'configured' or 'not_configured' — never the actual value.
    Reads .env directly so it's accurate even if the server started before the key was added."""
    # Fast path: already in os.environ (set at startup or by _update_env_key)
    if os.getenv(key, "").strip():
        return "configured"
    # Fallback: read .env file directly
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith(f"{key}="):
                val = line[len(f"{key}="):].strip().strip('"').strip("'")
                if val:
                    os.environ[key] = val  # sync into os.environ for future calls
                    return "configured"
    return "not_configured"


def _update_env_key(key: str, value: str) -> None:
    """Update or add key=value in .env; also update os.environ in-memory."""
    lines = _ENV_PATH.read_text().splitlines() if _ENV_PATH.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(lines) + "\n")
    os.environ[key] = value  # pick up immediately without restart


def _save_config(config: dict) -> None:
    """Write config dict back to config.yaml."""
    _CONFIG_PATH.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))


def _deep_merge(base: dict, updates: dict) -> dict:
    """Recursively merge updates into base, returning a new dict."""
    result = dict(base)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        # Bootstrap from example rather than crashing (e.g. if user ran server.py directly)
        example = _CONFIG_PATH.parent / "config.yaml.example"
        if example.exists():
            import shutil
            shutil.copy(example, _CONFIG_PATH)
        else:
            raise RuntimeError("config.yaml not found. Copy config.yaml.example and fill in values.")
    return yaml.safe_load(_CONFIG_PATH.read_text())


def _load_insights() -> dict | None:
    """Load insights.json if it exists. Returns None if missing."""
    if not _INSIGHTS_FILE.exists():
        return None
    try:
        return json.loads(_INSIGHTS_FILE.read_text())
    except Exception:
        return None


def _is_first_run() -> bool:
    """True if minimum required setup is not complete."""
    return not _setup_status()["complete"]


def _setup_status() -> dict:
    """Return which required setup items are complete and which are still missing."""
    try:
        account_id = _load_config().get("monarch", {}).get("checking_account_id", "")
    except Exception:
        account_id = ""
    account_ok = bool(account_id and account_id != "PASTE_ACCOUNT_ID_HERE")
    missing = [] if account_ok else ["primary account selection"]
    return {
        "complete":   not missing,
        "missing":    missing,
        "account_ok": account_ok,
    }


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
    # Generic fallback — still show the message but prefix it
    return f"Forecast error: {msg}"


def _get_forecast_data(config: dict) -> dict:
    if _cache:
        return _cache

    account_id = config["monarch"]["checking_account_id"]
    if not account_id or account_id == "PASTE_ACCOUNT_ID_HERE":
        raise RuntimeError(
            "Set monarch.checking_account_id in config.yaml. "
            "Run: python monarch_client.py --list-accounts"
        )

    horizon = config["forecast"]["horizon_days"]
    buffer = config["forecast"]["buffer_threshold"]

    if _monarch_raw:
        # Fast path — raw Monarch data cached; skip the slow Playwright fetch entirely.
        # Exclusions, overrides, and scenario/AI events are re-applied fresh below.
        current_balance = _monarch_raw["balance"]
        transactions    = _monarch_raw["transactions"]
        base_recurring  = _monarch_raw["recurring"]
    else:
        # Slow path — fetch from Monarch via Playwright (30-60 s)
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
                item = dict(item)  # shallow copy — don't mutate Monarch data
                item["amount"] = override_amt
            patched.append(item)
        recurring = patched

    # Fetch from Google Calendar (optional — set calendar.enabled: false in config to skip)
    cal_enabled = config.get("calendar", {}).get("enabled", True)
    if cal_enabled:
        try:
            cal_events = calendar_client.get_events(horizon_days=horizon)
        except RuntimeError as e:
            cal_events = []
            app.logger.warning(f"Calendar fetch skipped: {e}")
    else:
        cal_events = []

    # Load AI-predicted events from insights.json (if fresh)
    predicted_events = _load_predicted_events(config)

    # Drop predicted events that duplicate a recurring item already covered by a payment override.
    # Claude sometimes predicts credit card payments that are already in Monarch recurring — if the
    # user has set an override for that card, the recurring item IS the authoritative entry.
    if predicted_events and overrides:
        override_keywords = set(overrides.keys())  # already lowercase
        def _matches_override(desc: str) -> bool:
            desc_lower = desc.lower()
            return any(kw in desc_lower for kw in override_keywords)
        before = len(predicted_events)
        predicted_events = [p for p in predicted_events if not _matches_override(p.get("description", ""))]
        dropped = before - len(predicted_events)
        if dropped:
            app.logger.info(f"Dropped {dropped} AI-predicted event(s) already covered by payment overrides")

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
            app.logger.info(
                f"Dropped {dropped} AI-predicted event(s) already present in Monarch recurring"
            )

    if predicted_events:
        app.logger.info(f"Injecting {len(predicted_events)} AI-predicted events into forecast")

    # Load user scenario events and merge with AI predictions
    # Scenarios bypass the payment-override dedup filter above (they're intentional one-offs)
    scenario_events = _load_scenarios()
    if scenario_events:
        app.logger.info(f"Injecting {len(scenario_events)} scenario event(s) into forecast")
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


@app.route("/")
def index():
    if _is_first_run():
        return redirect(url_for("settings") + "?setup=1")
    auto_refresh = request.args.get("autorefresh") == "1"
    try:
        config = _load_config()
        data = _get_forecast_data(config)
        return render_template("dashboard.html", data=data, error=None, auto_refresh=auto_refresh)
    except Exception as e:
        return render_template("dashboard.html", data=None, error=_friendly_error(e), auto_refresh=auto_refresh)


@app.route("/refresh")
def refresh():
    _clear_all_cache()  # force full Monarch re-fetch on next load
    return redirect(url_for("index"))


@app.route("/api/forecast")
def api_forecast():
    config = _load_config()
    try:
        data = _get_forecast_data(config)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai-insights")
def api_ai_insights():
    """
    Serve the latest AI insights from insights.json.
    Returns 404 with a helpful message if not yet generated.
    Includes a "status" field: "fresh" | "stale" | "not_generated".
    """
    config = _load_config()
    if not _INSIGHTS_FILE.exists():
        # Determine whether AI is ready to run (enabled + API key present)
        ai_cfg = config.get("ai", {})
        ai_enabled = ai_cfg.get("enabled", False)
        provider = ai_cfg.get("provider", "anthropic")
        key_map = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY"}
        api_key_set = _env_key_status(key_map.get(provider, "ANTHROPIC_API_KEY")) == "configured"
        return jsonify({
            "status": "not_generated",
            "ai_ready": ai_enabled and api_key_set,
            "message": "Run 'python ai_daily.py' to generate AI insights.",
        }), 404

    try:
        insights = json.loads(_INSIGHTS_FILE.read_text())
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    generated_at = insights.get("generated_at", "")
    if generated_at:
        try:
            age = datetime.now() - datetime.fromisoformat(generated_at)
            max_age_hours = config.get("ai", {}).get("insights_max_age_hours", 26)
            insights["status"] = "fresh" if age.total_seconds() < max_age_hours * 3600 else "stale"
        except Exception:
            insights["status"] = "unknown"
    else:
        insights["status"] = "unknown"

    # Also mark stale if user_context.md was modified after insights were generated
    if insights.get("status") == "fresh" and generated_at and _USER_CONTEXT_FILE.exists():
        try:
            ctx_mtime = _USER_CONTEXT_FILE.stat().st_mtime
            gen_ts = datetime.fromisoformat(generated_at).timestamp()
            if ctx_mtime > gen_ts:
                insights["status"] = "stale"
        except Exception:
            pass

    return jsonify(insights)


@app.route("/api/user-context")
def api_user_context():
    """Return the current user_context.md content."""
    if not _USER_CONTEXT_FILE.exists():
        return jsonify({"content": ""})
    return jsonify({"content": _USER_CONTEXT_FILE.read_text()})


@app.route("/api/corrections", methods=["GET"])
def api_get_corrections():
    """Return all correction entries parsed from user_context.md."""
    items = _parse_corrections()
    return jsonify({"corrections": [
        {"id": c["id"], "date": c["date"], "type": c["type"], "text": c["text"], "raw": c["raw"]}
        for c in items
    ]})


@app.route("/api/corrections", methods=["POST"])
def api_set_corrections():
    """
    Add, delete, or clear individual correction lines in user_context.md.
    Add:    {"action": "add",    "text": "...", "type": "Correction|Known Fact|Note"}
    Delete: {"action": "delete", "raw":  "<full original line>"}
    Clear:  {"action": "clear"}
    Returns: {"ok": true}
    """
    body = request.get_json(force=True) or {}
    action = body.get("action", "add")
    corrections = _parse_corrections()

    if action == "clear":
        corrections = []
    elif action == "delete":
        raw_to_del = (body.get("raw") or "").strip()
        corrections = [c for c in corrections if c["raw"].strip() != raw_to_del]
    else:  # add
        text = (body.get("text") or "").strip()
        ctype = (body.get("type") or "Correction").strip()
        if ctype not in ("Correction", "Known Fact", "Note"):
            ctype = "Correction"
        if not text:
            return jsonify({"error": "text is required"}), 400
        today = date.today().isoformat()
        new_raw = f"- [{today}] [{ctype}] {text}"
        corrections.append({"date": today, "type": ctype, "text": text, "raw": new_raw})

    _write_corrections(corrections)
    return jsonify({"ok": True})


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """
    Append a correction to user_context.md.
    Body: {"text": "correction text"}
    Returns: {"ok": true}
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text is required"}), 400

    today = date.today().isoformat()
    bullet = f"- [{today}] {text}"

    if _USER_CONTEXT_FILE.exists():
        content = _USER_CONTEXT_FILE.read_text()
    else:
        content = _USER_CONTEXT_TEMPLATE

    if "## Corrections" in content:
        content = content.replace("## Corrections\n", f"## Corrections\n{bullet}\n", 1)
    else:
        content = content.rstrip() + f"\n\n## Corrections\n{bullet}\n"

    _USER_CONTEXT_FILE.write_text(content)
    return jsonify({"ok": True, "bullet": bullet})


@app.route("/api/payment-overrides", methods=["GET"])
def api_get_payment_overrides():
    """Return the current payment amount overrides from payment_overrides.json."""
    return jsonify(_load_payment_overrides())


@app.route("/api/payment-overrides", methods=["POST"])
def api_set_payment_override():
    """
    Save or clear a payment amount override.
    Body: {"name": "Apple Card", "amount": 5804, "note": "March statement"}
    To clear: {"name": "apple card", "clear": true}
    Returns: {"ok": true}
    """
    body = request.get_json(force=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    overrides = _load_payment_overrides()

    if body.get("clear"):
        overrides.pop(name.lower(), None)
    else:
        amount = body.get("amount")
        if amount is None:
            return jsonify({"error": "amount required"}), 400
        note = (body.get("note") or "").strip()
        overrides[name.lower()] = {
            "name": name,
            "amount": float(amount),  # sign preserved from client (inflow stays positive)
            "note": note,
            "updated": datetime.now().strftime("%Y-%m-%d"),
        }

    _PAYMENT_OVERRIDES_FILE.write_text(json.dumps(overrides, indent=2))
    _clear_forecast_cache()  # recompute forecast with new override (Monarch data reused)
    return jsonify({"ok": True})


@app.route("/api/payment-skips", methods=["GET", "POST"])
def api_payment_skips():
    """
    GET  → return [{name, month, note}]
    POST → add or clear a per-month skip
      Body: {"name": "Brown University", "month": "2026-05", "note": "No May payment"}
      To clear: {"name": "Brown University", "month": "2026-05", "clear": true}
    """
    if request.method == "GET":
        return jsonify(_load_payment_skips())

    body = request.get_json(force=True) or {}
    name  = (body.get("name") or "").strip()
    month = (body.get("month") or "").strip()   # "YYYY-MM"
    if not name or not month:
        return jsonify({"error": "name and month required"}), 400

    skips = _load_payment_skips()

    if body.get("clear"):
        skips = [s for s in skips
                 if not (s["name"].lower() == name.lower() and s["month"] == month)]
    else:
        # Upsert: replace existing (name, month) pair or append
        skips = [s for s in skips
                 if not (s["name"].lower() == name.lower() and s["month"] == month)]
        skips.append({
            "name":  name,
            "month": month,
            "note":  (body.get("note") or "").strip(),
        })

    _PAYMENT_SKIPS_FILE.write_text(json.dumps(skips, indent=2))
    _clear_forecast_cache()
    return jsonify({"ok": True})


@app.route("/api/payment-monthly-amounts", methods=["GET", "POST"])
def api_payment_monthly_amounts():
    """
    GET  → [{name, date, amount, note}]
    POST → upsert or clear a per-occurrence amount override
      Body: {"name": "Apple Card", "date": "2026-05-07", "amount": 3200, "note": "..."}
      To clear: {"name": "Apple Card", "date": "2026-05-07", "clear": true}
      Legacy "month" field (YYYY-MM) still accepted for backward compat.
    """
    if request.method == "GET":
        return jsonify(_load_payment_monthly_amounts())

    body  = request.get_json(force=True) or {}
    name  = (body.get("name") or "").strip()
    # Accept "date" (YYYY-MM-DD, new) or "month" (YYYY-MM, legacy)
    date  = (body.get("date") or "").strip()
    month = (body.get("month") or "").strip()
    key_field = "date" if date else "month"
    key_value = date if date else month
    if not name or not key_value:
        return jsonify({"error": "name and date required"}), 400

    records = _load_payment_monthly_amounts()

    if body.get("clear"):
        records = [r for r in records
                   if not (r["name"].lower() == name.lower() and r.get(key_field) == key_value)]
    else:
        amount = body.get("amount")
        if amount is None:
            return jsonify({"error": "amount required"}), 400
        records = [r for r in records
                   if not (r["name"].lower() == name.lower() and r.get(key_field) == key_value)]
        records.append({
            "name":    name,
            key_field: key_value,
            "amount":  float(amount),   # sign preserved from client
            "note":    (body.get("note") or "").strip(),
        })

    _PAYMENT_MONTHLY_AMOUNTS_FILE.write_text(json.dumps(records, indent=2))
    _clear_forecast_cache()
    return jsonify({"ok": True})


@app.route("/api/scenarios", methods=["GET"])
def api_get_scenarios():
    """Return the current list of user scenario events."""
    return jsonify(_load_scenarios())


@app.route("/api/scenarios", methods=["POST"])
def api_set_scenarios():
    """
    Add, delete, or clear scenario events.
    Add:    {"action": "add",    "date": "YYYY-MM-DD", "description": "...", "amount": 10000, "note": "..."}
    Delete: {"action": "delete", "id": "<event_id>"}
    Clear:  {"action": "clear"}
    Returns: {"ok": true}
    """
    body = request.get_json(force=True) or {}
    action = body.get("action", "add")
    scenarios = _load_scenarios()

    if action == "clear":
        scenarios = []
    elif action == "delete":
        sid = body.get("id")
        scenarios = [s for s in scenarios if s.get("id") != sid]
    else:  # add
        date_str = (body.get("date") or "").strip()
        description = (body.get("description") or "").strip()
        amount = body.get("amount")
        if not date_str or not description or amount is None:
            return jsonify({"error": "date, description, and amount are required"}), 400
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        frequency = (body.get("frequency") or "one-time").strip()
        scenarios.append({
            "id": f"s{int(datetime.now().timestamp() * 1000)}",
            "date": date_str,
            "description": description,
            "amount": float(amount),  # positive = inflow, negative = outflow
            "frequency": frequency,
            "created": datetime.now().strftime("%Y-%m-%d"),
        })

    _SCENARIOS_FILE.write_text(json.dumps(scenarios, indent=2))
    _clear_forecast_cache()  # recompute forecast with updated scenarios (Monarch data reused)
    return jsonify({"ok": True})


# ── Settings page ─────────────────────────────────────────────────────────────

def _read_env_value(key: str) -> str:
    """Return the actual value of an env key (from os.environ or .env file). Empty string if not set."""
    val = os.getenv(key, "").strip()
    if val:
        return val
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line[len(f"{key}="):].strip().strip('"').strip("'")
    return ""


@app.route("/settings")
def settings():
    try:
        config = _load_config()
    except Exception:
        config = {}   # template uses | default() filters; won't crash on first run
    status = _setup_status()
    setup_mode = request.args.get("setup") == "1" or not status["complete"]
    insights = _load_insights() or {}
    return render_template(
        "settings.html",
        config=config,
        env_status={k: _env_key_status(k) for k in _SENSITIVE_ENV_KEYS},
        insights_meta={
            "generated_at": insights.get("generated_at", ""),
            "token_usage": insights.get("token_usage"),
        },
        user_context=_USER_CONTEXT_FILE.read_text() if _USER_CONTEXT_FILE.exists() else "",
        setup_mode=setup_mode,
        setup_status=status,
    )


@app.route("/api/settings/forecast", methods=["POST"])
def api_settings_forecast():
    body = request.get_json(force=True) or {}
    try:
        horizon = int(body.get("horizon_days", 45))
        buffer_val = float(body.get("buffer_threshold", 1500))
        exclude_raw = body.get("exclude_recurring", "")
        exclude_list = [ln.strip() for ln in exclude_raw.splitlines() if ln.strip()]
        if not (1 <= horizon <= 365):
            return jsonify({"error": "horizon_days must be 1–365"}), 400
        if buffer_val < 0:
            return jsonify({"error": "buffer_threshold must be ≥ 0"}), 400
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400

    config = _load_config()
    config = _deep_merge(config, {"forecast": {
        "horizon_days": horizon,
        "buffer_threshold": buffer_val,
        "exclude_recurring": exclude_list,
    }})
    _save_config(config)
    _clear_forecast_cache()  # recompute with new forecast settings (Monarch data reused)
    return jsonify({"ok": True})


@app.route("/api/settings/ai", methods=["POST"])
def api_settings_ai():
    body = request.get_json(force=True) or {}
    try:
        enabled = bool(body.get("enabled", True))
        provider = (body.get("provider") or "anthropic").strip()
        model = (body.get("model") or "claude-sonnet-4-5").strip()
        history_months = int(body.get("history_months", 13))
        max_age_hours = int(body.get("insights_max_age_hours", 26))
        api_key     = (body.get("anthropic_api_key") or "").strip()
        openai_key  = (body.get("openai_api_key") or "").strip()
        google_key  = (body.get("google_api_key") or "").strip()
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400

    config = _load_config()
    config = _deep_merge(config, {"ai": {
        "enabled": enabled,
        "provider": provider,
        "model": model,
        "history_months": history_months,
        "insights_max_age_hours": max_age_hours,
    }})
    _save_config(config)

    if api_key:    _update_env_key("ANTHROPIC_API_KEY", api_key)
    if openai_key: _update_env_key("OPENAI_API_KEY",    openai_key)
    if google_key: _update_env_key("GOOGLE_API_KEY",    google_key)

    _clear_forecast_cache()  # AI settings don't need Monarch re-fetch
    return jsonify({"ok": True})


@app.route("/api/settings/monarch", methods=["POST"])
def api_settings_monarch():
    body = request.get_json(force=True) or {}
    account_id = (body.get("checking_account_id") or "").strip()

    if account_id:
        config = _load_config()
        # Look up a friendly account name from the 24-hour accounts cache (if available)
        acct_name = account_id  # fallback: display raw ID
        if _ACCOUNTS_CACHE_FILE.exists():
            try:
                cached_accts = json.loads(_ACCOUNTS_CACHE_FILE.read_text())
                match = next((a for a in cached_accts if str(a.get("id", "")) == str(account_id)), None)
                if match:
                    acct_name = match.get("name", account_id)
            except Exception:
                pass
        config = _deep_merge(config, {"monarch": {
            "checking_account_id": account_id,
            "checking_account_name": acct_name,
        }})
        _save_config(config)
        _clear_all_cache()  # new account = need fresh Monarch data

    return jsonify({"ok": True})


@app.route("/api/settings/calendar", methods=["POST"])
def api_settings_calendar():
    body = request.get_json(force=True) or {}
    enabled = bool(body.get("enabled", False))
    ics_url = (body.get("ics_url") or "").strip()
    service = (body.get("service") or "google").strip()

    config = _load_config()
    cal_updates: dict = {"enabled": enabled, "service": service}
    if ics_url:
        cal_updates["ics_url"] = ics_url
    config = _deep_merge(config, {"calendar": cal_updates})
    _save_config(config)
    _clear_forecast_cache()  # calendar refetched on next recompute (Monarch data reused)
    return jsonify({"ok": True})


@app.route("/api/settings/app", methods=["POST"])
def api_settings_app():
    body = request.get_json(force=True) or {}
    try:
        port = int(body.get("port", 5002))
        debug = bool(body.get("debug", False))
        if not (1024 <= port <= 65535):
            return jsonify({"error": "port must be 1024–65535"}), 400
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400

    config = _load_config()
    config = _deep_merge(config, {"app": {"port": port, "debug": debug}})
    _save_config(config)
    return jsonify({"ok": True, "restart_required": True})


@app.route("/api/settings/user-context", methods=["POST"])
def api_settings_user_context():
    body = request.get_json(force=True) or {}
    content = body.get("content", "")
    _USER_CONTEXT_FILE.write_text(content)
    return jsonify({"ok": True})


# ── AI analysis runner ────────────────────────────────────────────────────────

@app.route("/api/run-ai-analysis", methods=["POST"])
def api_run_ai_analysis():
    """Start ai_daily.py in a background thread. Returns immediately."""
    global _ai_running, _ai_run_log

    # Pre-flight: reject early with plain-language errors before spinning up a thread
    if _is_first_run():
        status = _setup_status()
        missing_str = ", ".join(status["missing"])
        return jsonify({"ok": False, "error": (
            f"Setup incomplete — still needed: {missing_str}. "
            "Go to Settings → Monarch Connection to finish setup."
        )}), 400

    try:
        ai_provider = _load_config().get("ai", {}).get("provider", "anthropic")
    except Exception:
        ai_provider = "anthropic"
    _key_map = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY"}
    _label_map = {"anthropic": "Anthropic", "openai": "OpenAI", "google": "Google"}
    required_key = _key_map.get(ai_provider, "ANTHROPIC_API_KEY")
    if _env_key_status(required_key) == "not_configured":
        return jsonify({"ok": False, "error": (
            "API key is not configured. "
            "Go to Settings → AI Insights, select your AI provider, and add your key."
        )}), 400

    if _ai_running:
        return jsonify({"ok": False, "error": "AI analysis is already running"}), 409

    # Set running flag and clear log BEFORE starting the thread so that the very
    # first client poll (which may fire within milliseconds) sees running=True and
    # doesn't prematurely declare the analysis complete with stale insights.
    _ai_running = True
    _ai_run_log = []

    def _run():
        global _ai_running, _ai_run_log
        try:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).parent / "ai_daily.py")],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,  # line-buffered so output arrives incrementally
                cwd=str(Path(__file__).parent),
            )
            # Kill the process after 15 minutes if it hasn't finished
            def _kill_on_timeout():
                _ai_run_log.append("⚠ Analysis timed out after 15 minutes and was stopped.")
                proc.kill()
            timer = threading.Timer(900, _kill_on_timeout)
            timer.start()
            try:
                for line in proc.stdout:          # reads one line at a time as they arrive
                    line = line.rstrip()
                    if line:
                        _ai_run_log.append(line)
                proc.wait()
            finally:
                timer.cancel()
        finally:
            _ai_running = False
            _clear_forecast_cache()  # recompute forecast to pick up new AI predictions

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "AI analysis started"})


@app.route("/api/ai-analysis-status")
def api_ai_analysis_status():
    """Poll this to track the background AI run."""
    insights = _load_insights() or {}
    return jsonify({
        "running": _ai_running,
        "log": _ai_run_log[-20:],  # last 20 lines of output
        "generated_at": insights.get("generated_at", ""),
        "token_usage": insights.get("token_usage"),
    })


@app.route("/api/ping")
def api_ping():
    """Lightweight liveness check used by the client after a server restart."""
    return jsonify({"ok": True})


@app.route("/api/restart-server", methods=["POST"])
def api_restart_server():
    """Schedule a server restart 1.5 s after this response is sent."""
    def _restart():
        import time
        time.sleep(1.5)
        subprocess.run(
            [str(Path(__file__).parent / "server.sh"), "restart"],
            cwd=str(Path(__file__).parent),
        )
    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/factory-reset", methods=["POST"])
def api_factory_reset():
    """
    Delete all runtime data files and restart the server into first-run/setup mode.
    Does NOT delete the Python venv (needed for restart via server.sh).
    """
    def _do_reset():
        import time, shutil
        time.sleep(0.5)  # let the HTTP response leave first

        base = Path(__file__).parent

        # Glob patterns catch exact names AND macOS numbered duplicates
        # e.g. "config 3.yaml", "browser_state 2.json", "user_context 2.md"

        # All JSON data files (payment_overrides, scenarios, accounts cache, etc.)
        for f in base.glob("*.json"):
            try: f.unlink(missing_ok=True)
            except Exception: pass

        # config.yaml + "config 3.yaml" etc. — does NOT match config.yaml.example
        for f in base.glob("config*.yaml"):
            try: f.unlink(missing_ok=True)
            except Exception: pass

        # .env + ".env 2" etc.
        for f in base.glob(".env*"):
            try: f.unlink(missing_ok=True)
            except Exception: pass

        # user_context.md + "user_context 2.md" etc.
        for f in base.glob("user_context*"):
            try: f.unlink(missing_ok=True)
            except Exception: pass

        # .server.pid, .server.log + numbered duplicates
        for f in base.glob(".server*"):
            try: f.unlink(missing_ok=True)
            except Exception: pass

        # macOS numbered duplicates of source/script files
        # Pattern "* [0-9]*.<ext>" matches "server 2.py", "run 2.sh" etc.
        # but never matches "server.py", "run.sh" (no space before the dot).
        for pat in ("* [0-9]*.py", "* [0-9]*.sh", "* [0-9]*.command"):
            for f in base.glob(pat):
                try: f.unlink(missing_ok=True)
                except Exception: pass

        # __pycache__ directories — delete .pyc files individually first (more
        # reliable than rmtree when files may be locked), then remove the dirs.
        for pyc in list(base.rglob("*.pyc")):
            try: pyc.unlink(missing_ok=True)
            except Exception: pass
        for cache_dir in list(base.rglob("__pycache__")):
            try: shutil.rmtree(str(cache_dir), ignore_errors=True)
            except Exception: pass

        _clear_all_cache()

        time.sleep(1.0)
        subprocess.run(
            [str(base / "server.sh"), "restart"],
            cwd=str(base),
        )

    threading.Thread(target=_do_reset, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/recurring-items")
def api_recurring_items():
    """Return the recurring items from the last forecast fetch (cached).
    Returns an empty list with a message if the forecast hasn't been loaded yet."""
    items = _cache.get("_recurring_raw", [])
    if not items:
        return jsonify({
            "items": [],
            "message": "Refresh the forecast first to load recurring items.",
        })
    result = []
    for r in sorted(items, key=lambda x: (x.get("name") or "").lower()):
        result.append({
            "name": r.get("name") or r.get("description") or "Unknown",
            "amount": round(float(r.get("amount") or 0), 2),
            "frequency": r.get("frequency") or "?",
        })
    return jsonify({"items": result})


# Account types eligible as a primary bill-paying account
_HELOC_KEYWORDS = ("equity", "heloc", "line of credit")


def _is_bill_paying_account(a: dict) -> bool:
    """Return True if the Monarch account is eligible as a primary bill-paying account.
    Keeps: depository (all), HELOC (loan with equity/heloc/line-of-credit in name).
    Drops: credit cards (no available-credit data), brokerage, real_estate, vehicle,
           and non-HELOC loans (mortgage, auto, student).
    """
    raw_type = a.get("type") or {}
    type_name = raw_type.get("name", "") if isinstance(raw_type, dict) else str(raw_type)
    if type_name == "depository":
        return True
    if type_name == "loan":
        name_lower = (a.get("displayName") or a.get("name") or "").lower()
        return any(kw in name_lower for kw in _HELOC_KEYWORDS)
    return False  # credit, brokerage, real_estate, vehicle → excluded


def _compact_account(a: dict) -> dict:
    raw_type = a.get("type") or {}
    type_name = raw_type.get("name", "") if isinstance(raw_type, dict) else str(raw_type)
    is_heloc = type_name == "loan"
    bal_raw = a.get("currentBalance") or a.get("displayBalance") or 0
    try:
        bal = round(float(bal_raw), 2)
    except (TypeError, ValueError):
        bal = 0.0
    return {
        "id": str(a.get("id", "")),
        "name": a.get("displayName") or a.get("name") or "Unknown",
        "balance": bal,
        "type": "heloc" if is_heloc else type_name,
    }


@app.route("/api/monarch-accounts")
def api_monarch_accounts():
    """Return bill-paying-eligible Monarch accounts, cached for 24 h.
    Pass ?cache_only=1 to get a fast 404 instead of a slow fetch when no cache exists."""
    cache_max_age = 24 * 3600
    if not request.args.get("force"):
        if _ACCOUNTS_CACHE_FILE.exists():
            age = datetime.now().timestamp() - _ACCOUNTS_CACHE_FILE.stat().st_mtime
            if age < cache_max_age:
                try:
                    return jsonify(json.loads(_ACCOUNTS_CACHE_FILE.read_text()))
                except Exception:
                    pass  # fall through to re-fetch if cache is corrupt

    if request.args.get("cache_only"):
        return jsonify({"error": "no cache"}), 404

    try:
        accounts = monarch_client.get_accounts()
    except Exception as e:
        msg = str(e)
        if "login" in msg.lower() or "session" in msg.lower():
            msg = (
                "Could not connect to Monarch — session may have expired. "
                "Try refreshing the forecast to re-authenticate."
            )
        return jsonify({"error": msg}), 500

    compact = [_compact_account(a) for a in accounts if _is_bill_paying_account(a)]
    _ACCOUNTS_CACHE_FILE.write_text(json.dumps(compact, indent=2))
    return jsonify(compact)


if __name__ == "__main__":
    config = _load_config()
    port = config.get("app", {}).get("port", 5002)
    debug = config.get("app", {}).get("debug", False)
    print(f"Balance Forecast running at http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=debug)
