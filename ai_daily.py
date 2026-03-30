#!/usr/bin/env python3
"""
Daily AI analysis runner for Balance Forecast.

Fetches 13 months of Monarch data (accounts, transactions, recurring, goals),
builds the deterministic forecast, calls Claude for AI insights, and writes
the result to insights.json for instant dashboard loading.

Usage:
  python ai_daily.py              # run once manually
  python ai_daily.py --dry-run    # print prompt size + estimated cost, no API call

Schedule via cron (6 AM daily):
  See README or run: python -c "import ai_daily; ai_daily.setup_cron()"
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

import ai_advisor
import calendar_client
import forecast as forecast_engine
import monarch_client

load_dotenv()

from paths import APP_DATA_DIR
_CONFIG_PATH   = APP_DATA_DIR / "config.yaml"
_INSIGHTS_FILE = APP_DATA_DIR / "insights.json"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise RuntimeError("config.yaml not found.")
    return yaml.safe_load(_CONFIG_PATH.read_text())


def run(dry_run: bool = False) -> None:
    config = _load_config()

    if not config.get("ai", {}).get("enabled", True):
        print("AI insights disabled in config.yaml (ai.enabled: false). Skipping.")
        return

    account_id = config["monarch"]["checking_account_id"]
    if not account_id or account_id == "PASTE_ACCOUNT_ID_HERE":
        raise RuntimeError("Set monarch.checking_account_id in config.yaml.")

    horizon = config["forecast"]["horizon_days"]
    buffer = config["forecast"]["buffer_threshold"]
    history_months = config.get("ai", {}).get("history_months", 13)
    history_days = history_months * 30

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching {history_months} months of Monarch data...")
    balance, transactions, recurring, all_accounts, goals = monarch_client.get_full_data(
        account_id, history_days=history_days
    )
    print(f"  Balance: ${balance:,.2f}")
    print(f"  Transactions: {len(transactions)}")
    print(f"  Recurring: {len(recurring)}")
    print(f"  Accounts: {len(all_accounts)}")
    print(f"  Goals: {len(goals)}")

    # Calendar events (optional)
    cal_events = []
    if config.get("calendar", {}).get("enabled", True):
        try:
            cal_events = calendar_client.get_events(horizon_days=horizon)
            print(f"  Calendar events: {len(cal_events)}")
        except RuntimeError as e:
            print(f"  Calendar skipped: {e}")

    # Filter out recurring items the user has excluded (credit-card-side duplicates, etc.)
    # NOTE: pass the full (unfiltered) recurring list to get_ai_insights() so Claude sees everything.
    exclude = {n.lower() for n in config.get("forecast", {}).get("exclude_recurring", [])}
    recurring_for_forecast = (
        [r for r in recurring if (r.get("name") or "").lower() not in exclude]
        if exclude else recurring
    )

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Building deterministic forecast...")
    forecast_result = forecast_engine.build_forecast(
        current_balance=balance,
        recurring_transactions=recurring_for_forecast,
        calendar_events=cal_events,
        buffer_threshold=buffer,
        horizon_days=horizon,
    )

    if dry_run:
        # Estimate token usage without calling the API
        monthly_summary = ai_advisor.summarize_by_month_category(transactions)
        prompt = ai_advisor._build_user_prompt(
            forecast_data=forecast_result,
            monthly_summary=monthly_summary,
            all_accounts=all_accounts,
            goals=goals,
            recurring=recurring,
            config=config,
        )
        # Rough token estimate: ~1 token per 4 characters
        estimated_tokens = len(prompt) // 4
        estimated_cost = estimated_tokens / 1_000_000 * 3.0 + 1500 / 1_000_000 * 15.0
        print(f"\n[dry-run] Prompt length: {len(prompt):,} chars (~{estimated_tokens:,} tokens)")
        print(f"[dry-run] Estimated cost: ${estimated_cost:.4f} per run")
        print(f"[dry-run] Monthly cost at 1x/day: ${estimated_cost * 30:.2f}")
        print("[dry-run] No API call made. Remove --dry-run to run for real.")
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Calling Claude for AI insights...")
    try:
        insights = ai_advisor.get_ai_insights(
            forecast_data=forecast_result,
            transactions=transactions,
            all_accounts=all_accounts,
            recurring=recurring,
            goals=goals,
            config=config,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    _INSIGHTS_FILE.write_text(json.dumps(insights, indent=2, default=str))
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✓ Insights written to {_INSIGHTS_FILE.name}")

    # Print summary
    print(f"\nNarrative: {insights.get('narrative', '')[:200]}")
    predicted = insights.get("predicted_expenses", [])
    if predicted:
        print(f"Predicted expenses: {len(predicted)}")
    rec = insights.get("transfer_recommendation")
    if rec:
        print(f"Transfer rec: ${rec.get('amount', 0):,.0f} from {rec.get('source_account', '?')} by {rec.get('transfer_by', '?')}")
    flags = insights.get("risk_flags", [])
    if flags:
        print(f"Risk flags: {len(flags)}")
        for f in flags:
            print(f"  ⚠ {f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily AI analysis for Balance Forecast")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate token usage and cost without making an API call",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)
