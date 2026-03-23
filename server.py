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
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

from config import (
    _CONFIG_PATH,
    _ENV_PATH,
    _SENSITIVE_ENV_KEYS,
    _deep_merge,
    _env_key_status,
    _is_first_run,
    _load_config,
    _read_env_value,
    _save_config,
    _setup_status,
    _update_env_key,
)
from forecast_builder import (
    _cache,
    _clear_all_cache,
    _clear_forecast_cache,
    _friendly_error,
    _get_forecast_data,
    _monarch_raw,
)
from storage import (
    _ACCOUNTS_CACHE_FILE,
    _DISMISSED_SUGGESTIONS_FILE,
    _INSIGHTS_FILE,
    _PAYMENT_MONTHLY_AMOUNTS_FILE,
    _PAYMENT_OVERRIDES_FILE,
    _PAYMENT_SKIPS_FILE,
    _SCENARIOS_FILE,
    _USER_CONTEXT_FILE,
    _USER_CONTEXT_TEMPLATE,
    _atomic_write,
    _load_dismissed_suggestions,
    _load_insights,
    _load_payment_monthly_amounts,
    _load_payment_overrides,
    _load_payment_skips,
    _load_scenarios,
    _parse_corrections,
    _save_dismissed_suggestions,
    _write_corrections,
)

app = Flask(__name__)

# AI analysis background-run state
_ai_running: bool = False
_ai_run_log: list = []


# ── Dashboard ──────────────────────────────────────────────────────────────────

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
    _clear_all_cache()   # force full Monarch re-fetch on next load
    return redirect(url_for("index"))


# ── API: forecast ──────────────────────────────────────────────────────────────

@app.route("/api/forecast")
def api_forecast():
    config = _load_config()
    try:
        data = _get_forecast_data(config)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: AI insights ───────────────────────────────────────────────────────────

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
        key_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai":    "OPENAI_API_KEY",
            "google":    "GOOGLE_API_KEY",
        }
        api_key_set = _env_key_status(key_map.get(provider, "ANTHROPIC_API_KEY")) == "configured"
        return jsonify({
            "status":   "not_generated",
            "ai_ready": ai_enabled and api_key_set,
            "message":  "Run 'python ai_daily.py' to generate AI insights.",
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

    # Filter suggested_actions: remove already-applied and dismissed suggestions
    suggestions = insights.get("suggested_actions", [])
    if suggestions:
        skips_set   = {(s["name"].lower(), s["month"]) for s in _load_payment_skips()}
        amounts_set = {(r["name"].lower(), r.get("month", "")) for r in _load_payment_monthly_amounts()}
        dismissed   = set(_load_dismissed_suggestions())

        def _sug_fingerprint(s: dict) -> str:
            t = s.get("type", "")
            n = (s.get("transaction_name") or "").lower()
            if t in ("skip", "override"):
                return f"{t}:{n}:{s.get('month', '')}"
            return f"{t}:{n}"

        def _already_applied(s: dict) -> bool:
            t = s.get("type", "")
            n = (s.get("transaction_name") or "").lower()
            if t == "skip":     return (n, s.get("month", "")) in skips_set
            if t == "override": return (n, s.get("month", "")) in amounts_set
            return False

        insights["suggested_actions"] = [
            s for s in suggestions
            if not _already_applied(s) and _sug_fingerprint(s) not in dismissed
        ]

    return jsonify(insights)


# ── API: AI suggested actions ─────────────────────────────────────────────────

@app.route("/api/ai-suggestions/apply", methods=["POST"])
def api_ai_suggestions_apply():
    """
    Apply an AI-suggested forecast action.
    Body: {"suggestion": {"type": "skip|override|suppress", "transaction_name": "...",
                          "month": "YYYY-MM", "amount": -123.00}}
    """
    body  = request.get_json(force=True) or {}
    s     = body.get("suggestion", {})
    stype = (s.get("type") or "").strip()
    name  = (s.get("transaction_name") or "").strip()
    if not name or not stype:
        return jsonify({"error": "suggestion.type and suggestion.transaction_name required"}), 400

    if stype == "skip":
        month = (s.get("month") or "").strip()
        if not month:
            return jsonify({"error": "month required for skip"}), 400
        skips = _load_payment_skips()
        skips = [x for x in skips
                 if not (x["name"].lower() == name.lower() and x["month"] == month)]
        skips.append({"name": name, "month": month, "note": "Applied from AI suggestion"})
        _atomic_write(_PAYMENT_SKIPS_FILE, json.dumps(skips, indent=2))

    elif stype == "override":
        month  = (s.get("month") or "").strip()
        amount = s.get("amount")
        if not month or amount is None:
            return jsonify({"error": "month and amount required for override"}), 400
        records = _load_payment_monthly_amounts()
        records = [r for r in records
                   if not (r["name"].lower() == name.lower() and r.get("month") == month)]
        records.append({"name": name, "month": month, "amount": float(amount),
                        "note": "Applied from AI suggestion"})
        _atomic_write(_PAYMENT_MONTHLY_AMOUNTS_FILE, json.dumps(records, indent=2))

    elif stype == "suppress":
        overrides = _load_payment_overrides()
        overrides[name.lower()] = {
            "name":    name,
            "amount":  0.0,
            "note":    "Suppressed via AI suggestion",
            "updated": datetime.now().strftime("%Y-%m-%d"),
        }
        _atomic_write(_PAYMENT_OVERRIDES_FILE, json.dumps(overrides, indent=2))

    else:
        return jsonify({"error": f"unknown suggestion type: {stype!r}"}), 400

    _clear_forecast_cache()
    return jsonify({"ok": True})


@app.route("/api/ai-suggestions/dismiss", methods=["POST"])
def api_ai_suggestions_dismiss():
    """
    Dismiss a suggested action so it won't reappear (even after re-running AI).
    Body: {"fingerprint": "skip:brown university:2026-05"}
    """
    body = request.get_json(force=True) or {}
    fp   = (body.get("fingerprint") or "").strip()
    if not fp:
        return jsonify({"error": "fingerprint required"}), 400
    dismissed = _load_dismissed_suggestions()
    if fp not in dismissed:
        dismissed.append(fp)
        _save_dismissed_suggestions(dismissed)
    return jsonify({"ok": True})


# ── API: user context / corrections ───────────────────────────────────────────

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
        text  = (body.get("text") or "").strip()
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

    today  = date.today().isoformat()
    bullet = f"- [{today}] {text}"

    if _USER_CONTEXT_FILE.exists():
        content = _USER_CONTEXT_FILE.read_text()
    else:
        content = _USER_CONTEXT_TEMPLATE

    if "## Corrections" in content:
        content = content.replace("## Corrections\n", f"## Corrections\n{bullet}\n", 1)
    else:
        content = content.rstrip() + f"\n\n## Corrections\n{bullet}\n"

    _atomic_write(_USER_CONTEXT_FILE, content)
    return jsonify({"ok": True, "bullet": bullet})


# ── API: payment overrides ─────────────────────────────────────────────────────

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
            "name":    name,
            "amount":  float(amount),   # sign preserved from client (inflow stays positive)
            "note":    note,
            "updated": datetime.now().strftime("%Y-%m-%d"),
        }

    _atomic_write(_PAYMENT_OVERRIDES_FILE, json.dumps(overrides, indent=2))
    _clear_forecast_cache()   # recompute forecast with new override (Monarch data reused)
    return jsonify({"ok": True})


# ── API: payment skips ─────────────────────────────────────────────────────────

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

    body  = request.get_json(force=True) or {}
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

    _atomic_write(_PAYMENT_SKIPS_FILE, json.dumps(skips, indent=2))
    _clear_forecast_cache()
    return jsonify({"ok": True})


# ── API: payment monthly amounts ───────────────────────────────────────────────

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
    date_val = (body.get("date") or "").strip()
    month    = (body.get("month") or "").strip()
    key_field = "date" if date_val else "month"
    key_value = date_val if date_val else month
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

    _atomic_write(_PAYMENT_MONTHLY_AMOUNTS_FILE, json.dumps(records, indent=2))
    _clear_forecast_cache()
    return jsonify({"ok": True})


# ── API: scenarios ─────────────────────────────────────────────────────────────

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
    body   = request.get_json(force=True) or {}
    action = body.get("action", "add")
    scenarios = _load_scenarios()

    if action == "clear":
        scenarios = []
    elif action == "delete":
        sid = body.get("id")
        scenarios = [s for s in scenarios if s.get("id") != sid]
    else:  # add
        date_str    = (body.get("date") or "").strip()
        description = (body.get("description") or "").strip()
        amount      = body.get("amount")
        if not date_str or not description or amount is None:
            return jsonify({"error": "date, description, and amount are required"}), 400
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "date must be YYYY-MM-DD"}), 400
        frequency = (body.get("frequency") or "one-time").strip()
        scenarios.append({
            "id":          f"s{uuid.uuid4().hex[:8]}",
            "date":        date_str,
            "description": description,
            "amount":      float(amount),   # positive = inflow, negative = outflow
            "frequency":   frequency,
            "created":     datetime.now().strftime("%Y-%m-%d"),
        })

    _atomic_write(_SCENARIOS_FILE, json.dumps(scenarios, indent=2))
    _clear_forecast_cache()   # recompute forecast with updated scenarios (Monarch data reused)
    return jsonify({"ok": True})


# ── Settings page ──────────────────────────────────────────────────────────────

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
            "token_usage":  insights.get("token_usage"),
        },
        user_context=_USER_CONTEXT_FILE.read_text() if _USER_CONTEXT_FILE.exists() else "",
        setup_mode=setup_mode,
        setup_status=status,
    )


@app.route("/api/settings/forecast", methods=["POST"])
def api_settings_forecast():
    body = request.get_json(force=True) or {}
    try:
        horizon     = int(body.get("horizon_days", 45))
        buffer_val  = float(body.get("buffer_threshold", 1500))
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
        "horizon_days":     horizon,
        "buffer_threshold": buffer_val,
        "exclude_recurring": exclude_list,
    }})
    _save_config(config)
    _clear_forecast_cache()   # recompute with new forecast settings (Monarch data reused)
    return jsonify({"ok": True})


@app.route("/api/settings/ai", methods=["POST"])
def api_settings_ai():
    body = request.get_json(force=True) or {}
    try:
        enabled        = bool(body.get("enabled", True))
        provider       = (body.get("provider") or "anthropic").strip()
        model          = (body.get("model") or "claude-sonnet-4-5").strip()
        history_months = int(body.get("history_months", 13))
        max_age_hours  = int(body.get("insights_max_age_hours", 26))
        api_key        = (body.get("anthropic_api_key") or "").strip()
        openai_key     = (body.get("openai_api_key") or "").strip()
        google_key     = (body.get("google_api_key") or "").strip()
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400

    config = _load_config()
    config = _deep_merge(config, {"ai": {
        "enabled":              enabled,
        "provider":             provider,
        "model":                model,
        "history_months":       history_months,
        "insights_max_age_hours": max_age_hours,
    }})
    _save_config(config)

    if api_key:    _update_env_key("ANTHROPIC_API_KEY", api_key)
    if openai_key: _update_env_key("OPENAI_API_KEY",    openai_key)
    if google_key: _update_env_key("GOOGLE_API_KEY",    google_key)

    _clear_forecast_cache()   # AI settings don't need Monarch re-fetch
    return jsonify({"ok": True})


@app.route("/api/settings/monarch", methods=["POST"])
def api_settings_monarch():
    body       = request.get_json(force=True) or {}
    account_id = (body.get("checking_account_id") or "").strip()

    if account_id:
        config = _load_config()
        # Look up a friendly account name from the 24-hour accounts cache (if available)
        acct_name = account_id   # fallback: display raw ID
        if _ACCOUNTS_CACHE_FILE.exists():
            try:
                cached_accts = json.loads(_ACCOUNTS_CACHE_FILE.read_text())
                match = next(
                    (a for a in cached_accts if str(a.get("id", "")) == str(account_id)),
                    None,
                )
                if match:
                    acct_name = match.get("name", account_id)
            except Exception:
                pass
        config = _deep_merge(config, {"monarch": {
            "checking_account_id":   account_id,
            "checking_account_name": acct_name,
        }})
        _save_config(config)
        _clear_all_cache()   # new account = need fresh Monarch data

    return jsonify({"ok": True})


@app.route("/api/settings/calendar", methods=["POST"])
def api_settings_calendar():
    body    = request.get_json(force=True) or {}
    enabled = bool(body.get("enabled", False))
    ics_url = (body.get("ics_url") or "").strip()
    service = (body.get("service") or "google").strip()

    config = _load_config()
    cal_updates: dict = {"enabled": enabled, "service": service}
    if ics_url:
        cal_updates["ics_url"] = ics_url
    config = _deep_merge(config, {"calendar": cal_updates})
    _save_config(config)
    _clear_forecast_cache()   # calendar refetched on next recompute (Monarch data reused)
    return jsonify({"ok": True})


@app.route("/api/settings/app", methods=["POST"])
def api_settings_app():
    body = request.get_json(force=True) or {}
    try:
        port  = int(body.get("port", 5002))
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
    body    = request.get_json(force=True) or {}
    content = body.get("content", "")
    _atomic_write(_USER_CONTEXT_FILE, content)
    return jsonify({"ok": True})


# ── AI analysis runner ─────────────────────────────────────────────────────────

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
    _key_map   = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "google": "GOOGLE_API_KEY"}
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
                bufsize=1,   # line-buffered so output arrives incrementally
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
            _clear_forecast_cache()   # recompute forecast to pick up new AI predictions

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "message": "AI analysis started"})


@app.route("/api/ai-analysis-status")
def api_ai_analysis_status():
    """Poll this to track the background AI run."""
    insights = _load_insights() or {}
    return jsonify({
        "running":      _ai_running,
        "log":          _ai_run_log[-20:],   # last 20 lines of output
        "generated_at": insights.get("generated_at", ""),
        "token_usage":  insights.get("token_usage"),
    })


# ── Server management ──────────────────────────────────────────────────────────

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
        import time
        import shutil
        time.sleep(0.5)   # let the HTTP response leave first

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
        for pat in ("* [0-9]*.py", "* [0-9]*.sh", "* [0-9]*.command"):
            for f in base.glob(pat):
                try: f.unlink(missing_ok=True)
                except Exception: pass

        # __pycache__ directories
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


# ── Recurring items ────────────────────────────────────────────────────────────

@app.route("/api/recurring-items")
def api_recurring_items():
    """Return the recurring items from the last forecast fetch (cached).
    Returns an empty list with a message if the forecast hasn't been loaded yet."""
    items = _cache.get("_recurring_raw", [])
    if not items:
        return jsonify({
            "items":   [],
            "message": "Refresh the forecast first to load recurring items.",
        })
    result = []
    for r in sorted(items, key=lambda x: (x.get("name") or "").lower()):
        result.append({
            "name":      r.get("name") or r.get("description") or "Unknown",
            "amount":    round(float(r.get("amount") or 0), 2),
            "frequency": r.get("frequency") or "?",
        })
    return jsonify({"items": result})


# ── Monarch accounts ───────────────────────────────────────────────────────────

# Account types eligible as a primary bill-paying account
_HELOC_KEYWORDS = ("equity", "heloc", "line of credit")


def _is_bill_paying_account(a: dict) -> bool:
    """Return True if the Monarch account is eligible as a primary bill-paying account.

    Keeps: depository (all), HELOC (loan with equity/heloc/line-of-credit in name).
    Drops: credit cards, brokerage, real_estate, vehicle, and non-HELOC loans.
    """
    raw_type  = a.get("type") or {}
    type_name = raw_type.get("name", "") if isinstance(raw_type, dict) else str(raw_type)
    if type_name == "depository":
        return True
    if type_name == "loan":
        name_lower = (a.get("displayName") or a.get("name") or "").lower()
        return any(kw in name_lower for kw in _HELOC_KEYWORDS)
    return False   # credit, brokerage, real_estate, vehicle → excluded


def _compact_account(a: dict) -> dict:
    raw_type  = a.get("type") or {}
    type_name = raw_type.get("name", "") if isinstance(raw_type, dict) else str(raw_type)
    is_heloc  = type_name == "loan"
    bal_raw   = a.get("currentBalance") or a.get("displayBalance") or 0
    try:
        bal = round(float(bal_raw), 2)
    except (TypeError, ValueError):
        bal = 0.0
    return {
        "id":      str(a.get("id", "")),
        "name":    a.get("displayName") or a.get("name") or "Unknown",
        "balance": bal,
        "type":    "heloc" if is_heloc else type_name,
    }


@app.route("/api/monarch-accounts")
def api_monarch_accounts():
    """Return bill-paying-eligible Monarch accounts, cached for 24 h.
    Pass ?force=1 to bypass the cache and re-fetch from Monarch."""
    cache_max_age = 24 * 3600
    if not request.args.get("force"):
        if _ACCOUNTS_CACHE_FILE.exists():
            age = datetime.now().timestamp() - _ACCOUNTS_CACHE_FILE.stat().st_mtime
            if age < cache_max_age:
                try:
                    return jsonify(json.loads(_ACCOUNTS_CACHE_FILE.read_text()))
                except Exception:
                    pass   # fall through to re-fetch if cache is corrupt

    if request.args.get("cache_only"):
        return jsonify({"error": "no cache"}), 404

    import monarch_client
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
    _atomic_write(_ACCOUNTS_CACHE_FILE, json.dumps(compact, indent=2))
    return jsonify(compact)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = _load_config()
    port   = config.get("app", {}).get("port", 5002)
    debug  = config.get("app", {}).get("debug", False)
    print(f"Balance Forecast running at http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=debug)
