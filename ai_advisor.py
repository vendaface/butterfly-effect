"""
AI Advisor — uses Claude API to enhance the balance forecast with:

  1. Monthly category summarization of 13-month Monarch history (local, no API call)
  2. Seasonal pattern detection: "April always runs higher due to tax prep + insurance"
  3. Predicted one-off expenses for the current horizon based on prior-year patterns
  4. Full insights package:
     - Plain-English narrative
     - Minimal transfer recommendation with source account selection
     - Surplus routing suggestions tied to Monarch savings goals
     - Risk flags (e.g., Brown tuition 10-month gap detection)

Standalone usage:
  python ai_advisor.py   (reads insights.json and prints a summary)
"""

import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import anthropic
import openai
from google import genai as google_genai
from google.genai import types as google_types
import yaml
from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = Path(__file__).parent / "config.yaml"
INSIGHTS_FILE = Path(__file__).parent / "insights.json"
_USER_CONTEXT_FILE = Path(__file__).parent / "user_context.md"

# ── Local summarization (no API call) ─────────────────────────────────────────


def summarize_by_month_category(transactions: list[dict]) -> dict:
    """
    Summarize transactions by (YYYY-MM, category) for compact AI input.

    Returns:
      {"2025-04": {"Food & Drink": -850.0, "Income": 10828.0, ...}, ...}

    Monarch transaction shape:
      txn["date"]                      # "2026-03-20"
      txn["amount"]                    # float, negative = expense
      txn["category"]["name"]          # str (may be None)
      txn["merchant"]["name"]          # str (fallback label)
    """
    summary: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for txn in transactions:
        txn_date = str(txn.get("date", ""))[:7]  # "YYYY-MM"
        if not txn_date or len(txn_date) < 7:
            continue

        # Category label: prefer Monarch category, fall back to merchant name
        cat = None
        cat_obj = txn.get("category")
        if isinstance(cat_obj, dict):
            cat = cat_obj.get("name")
        if not cat:
            merch = txn.get("merchant")
            if isinstance(merch, dict):
                cat = merch.get("name")
        cat = cat or "Uncategorized"

        amount = float(txn.get("amount", 0))
        summary[txn_date][cat] += amount

    # Convert to plain dicts, sort by month
    return {
        month: {k: round(v, 2) for k, v in cats.items()}
        for month, cats in sorted(summary.items())
    }


def _compact_accounts(all_accounts: list[dict]) -> list[dict]:
    """Return a compact view of accounts for the AI prompt."""
    result = []
    for acct in all_accounts:
        name = acct.get("displayName") or acct.get("name") or "Unknown"
        bal = acct.get("currentBalance") or acct.get("displayBalance") or 0
        acct_type = ""
        t = acct.get("type")
        if isinstance(t, dict):
            acct_type = t.get("name", "")
        elif isinstance(t, str):
            acct_type = t
        entry: dict = {"name": name, "type": acct_type}
        if acct_type == "credit":
            # Use a distinct field name so the AI does not confuse the statement
            # balance (amount owed) with the scheduled monthly payment amount.
            # Payment amounts live in the MONARCH RECURRING section.
            entry["statement_balance_owed"] = round(float(bal), 2)
        else:
            entry["balance"] = round(float(bal), 2)
        result.append(entry)
    return result


def _compact_goals(goals: list[dict]) -> list[dict]:
    """Return a compact view of savings goals for the AI prompt."""
    result = []
    for g in goals:
        name = g.get("name") or g.get("title") or "Unnamed Goal"
        current = g.get("currentAmount") or g.get("balance") or 0
        target = g.get("amount") or g.get("targetAmount") or 0
        monthly = g.get("monthlyContribution") or g.get("contributionAmount") or 0
        target_date = g.get("targetDate") or g.get("plannedCompletionDate") or ""
        result.append({
            "name": name,
            "current": round(float(current), 2),
            "target": round(float(target), 2),
            "monthly_contribution": round(float(monthly), 2),
            "target_date": str(target_date)[:10] if target_date else "",
        })
    return result


def _compact_recurring(recurring: list[dict]) -> list[dict]:
    """Return a compact view of recurring items for the AI prompt."""
    result = []
    for r in recurring:
        name = r.get("name") or r.get("description") or "Unknown"
        amount = r.get("amount")
        freq = r.get("frequency") or ""
        result.append({
            "name": name,
            "amount": round(float(amount), 2) if amount is not None else None,
            "frequency": freq,
        })
    return sorted(result, key=lambda x: abs(x["amount"] or 0), reverse=True)


def _compact_forecast(forecast_data: dict, max_days: int = 45) -> list[dict]:
    """Strip forecast days to only what the AI needs (date, balance, event names)."""
    result = []
    for day in forecast_data.get("days", [])[:max_days]:
        events = [
            {
                "description": e["description"],
                "amount": e["amount"],
                "source": e["source"],
            }
            for e in day.get("events", [])
        ]
        result.append({
            "date": day["date"],
            "balance": day["balance"],
            "below_threshold": day.get("below_threshold", False),
            "events": events,
        })
    return result


# ── AI Provider support ───────────────────────────────────────────────────────

# Per-provider cost table: model-id-prefix → (input_$/1M, output_$/1M)
_COST_TABLE: dict[str, tuple[float, float]] = {
    "claude-opus-4":    (15.00, 75.00),
    "claude-sonnet-4":  (3.00,  15.00),
    "claude-haiku-4":   (0.80,   4.00),
    "gpt-4o-mini":      (0.15,   0.60),
    "gpt-4o":           (2.50,  10.00),
    "gpt-4-turbo":      (10.00, 30.00),
    "o3":               (10.00, 40.00),
    "o1":               (15.00, 60.00),
    "gemini-2.0-flash": (0.10,   0.40),
    "gemini-1.5-flash": (0.075,  0.30),
    "gemini-1.5-pro":   (1.25,   5.00),
}


def _model_cost(model: str, in_tok: int, out_tok: int) -> float:
    """Return estimated cost in USD for the given model and token counts."""
    for prefix, (cin, cout) in _COST_TABLE.items():
        if model.startswith(prefix):
            return round((in_tok * cin + out_tok * cout) / 1_000_000, 4)
    return 0.0


def _call_anthropic(model: str, user_prompt: str, _config: dict) -> tuple[str, int, int]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key.startswith("sk-ant-..."):
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Add it to your .env file.\n"
            "Get your key at: https://console.anthropic.com/"
        )
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model, max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text.strip(), msg.usage.input_tokens, msg.usage.output_tokens


def _call_openai(model: str, user_prompt: str, _config: dict) -> tuple[str, int, int]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set. Add it to your .env file.\n"
            "Get your key at: https://platform.openai.com/api-keys"
        )
    client = openai.OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model, max_tokens=2048,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
    )
    raw = resp.choices[0].message.content or ""
    return raw.strip(), resp.usage.prompt_tokens, resp.usage.completion_tokens


def _call_google(model: str, user_prompt: str, _config: dict) -> tuple[str, int, int]:
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY not set. Add it to your .env file.\n"
            "Get your key at: https://aistudio.google.com/app/apikey"
        )
    client = google_genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config=google_types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            response_mime_type="application/json",
            max_output_tokens=2048,
        ),
    )
    usage = resp.usage_metadata
    in_tok  = usage.prompt_token_count     if usage else 0
    out_tok = usage.candidates_token_count if usage else 0
    return resp.text.strip(), in_tok, out_tok


_PROVIDER_DISPATCH = {
    "anthropic": _call_anthropic,
    "openai":    _call_openai,
    "google":    _call_google,
}


# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a personal finance advisor analyzing a household checking account.
You have access to 13 months of transaction history, all account balances,
savings goals, and a 45-day deterministic balance forecast.

IMPORTANT — CREDIT CARD DATA: In the ACCOUNT BALANCES section, credit accounts show
'statement_balance_owed' (the total currently owed on the card), NOT the monthly
payment amount. Always use the MONARCH RECURRING section for scheduled payment amounts.
Never cite statement_balance_owed as if it were a payment amount in risk flags or narrative.

IMPORTANT: The user prompt will contain a "USER CORRECTIONS & CONTEXT" section.
This contains facts the user has personally verified. These override any inferences
you might make from the transaction data. Always follow these corrections exactly.

Your job:
1. Identify seasonal patterns and one-off expenses from prior-year data that
   are NOT already in the recurring transactions list (e.g., annual tax prep,
   insurance renewals, property tax, registration fees, club memberships).
2. Predict specific expenses likely to occur in the next 45 days based on
   the same period last year. Only include high-confidence predictions.
3. Recommend the MINIMUM transfer amount needed to keep checking above the
   buffer threshold — accounting for upcoming paychecks so you don't over-transfer.
4. Identify the best source account for the transfer (prefer savings over HELOC).
5. Flag any projected surpluses and suggest specific goal/debt allocations.
6. Note any payment plan patterns you detect (e.g., a tuition payment that
   stops for a month then resumes — the "10-month plan" pattern).

Always respond with valid JSON only. No prose outside the JSON structure.
Schema:
{
  "narrative": "2-3 sentence plain-English summary of the 45-day outlook",
  "predicted_expenses": [
    {
      "date": "YYYY-MM-DD",
      "description": "what it is",
      "amount": -1200.00,
      "confidence": "high | medium | low"
    }
  ],
  "transfer_recommendation": {
    "amount": 2500.00,
    "source_account": "Savings (...6299)",
    "transfer_by": "YYYY-MM-DD",
    "reasoning": "explain why this amount and timing",
    "is_minimal": true
  },
  "surplus_alerts": [
    {
      "period": "late April",
      "estimated_surplus": 3400.00,
      "suggested_allocation": "Put $2,000 toward HELOC principal, $1,400 to Emergency Fund"
    }
  ],
  "risk_flags": [
    "Brown tuition 10-month plan: payments end April 3, resume ~September — forecast gap is expected"
  ],
  "seasonal_notes": "April historically runs $800 above average due to tax prep and insurance renewals"
}

If there are no transfer recommendations needed, set transfer_recommendation to null.
If there are no surplus alerts, set surplus_alerts to [].
If there are no risk flags, set risk_flags to [].
"""


def _build_user_prompt(
    forecast_data: dict,
    monthly_summary: dict,
    all_accounts: list[dict],
    goals: list[dict],
    recurring: list[dict],
    config: dict,
) -> str:
    today = date.today().isoformat()
    horizon = config.get("forecast", {}).get("horizon_days", 45)
    buffer = config.get("forecast", {}).get("buffer_threshold", 1500)
    current_balance = forecast_data.get("current_balance", 0)
    transfer_recs = forecast_data.get("transfer_recommendations", [])

    compact_accounts = _compact_accounts(all_accounts)
    compact_goals = _compact_goals(goals)
    compact_recurring = _compact_recurring(recurring)
    compact_forecast = _compact_forecast(forecast_data, max_days=horizon)

    # Load user corrections — injected early so they take priority
    user_context_section = []
    if _USER_CONTEXT_FILE.exists():
        ctx = _USER_CONTEXT_FILE.read_text().strip()
        if ctx:
            user_context_section = [
                "",
                "=== USER CORRECTIONS & CONTEXT ===",
                ctx,
                "",
            ]

    lines = [
        f"Today: {today}",
        f"Checking balance: ${current_balance:,.2f}",
        f"Forecast horizon: {horizon} days",
        f"Buffer threshold: ${buffer:,.2f}",
    ] + user_context_section + [
        "",
        "=== ACCOUNT BALANCES ===",
        json.dumps(compact_accounts, indent=2),
        "",
        "=== SAVINGS GOALS ===",
        json.dumps(compact_goals, indent=2) if compact_goals else "(none found — goals page may not have loaded)",
        "",
        "=== DETERMINISTIC FORECAST (next {} days) ===".format(horizon),
        json.dumps(compact_forecast, indent=2),
        "",
        "=== DETERMINISTIC TRANSFER RECOMMENDATIONS ===",
        json.dumps(transfer_recs, indent=2) if transfer_recs else "(none — balance stays above threshold)",
        "",
        "=== MONTHLY SPENDING SUMMARY (13 months, by category) ===",
        json.dumps(monthly_summary, indent=2),
        "",
        "=== MONARCH RECURRING ({} items, sorted by size) ===".format(len(compact_recurring)),
        json.dumps(compact_recurring, indent=2),
    ]

    return "\n".join(lines)


def get_ai_insights(
    forecast_data: dict,
    transactions: list[dict],
    all_accounts: list[dict],
    recurring: list[dict],
    goals: list[dict],
    config: dict,
) -> dict:
    """
    Call the configured AI provider to generate insights for the balance forecast.

    Supports: anthropic (Claude), openai (GPT), google (Gemini).
    Provider is read from config.ai.provider (default: "anthropic").

    Returns a dict matching the JSON schema in _SYSTEM_PROMPT, plus:
      "generated_at": ISO timestamp string
      "token_usage":  {"input_tokens", "output_tokens", "cost_usd"}
    """
    ai_cfg   = config.get("ai", {})
    provider = (ai_cfg.get("provider") or "anthropic").lower().strip()
    model    = (ai_cfg.get("model") or "claude-sonnet-4-5").strip()

    if provider not in _PROVIDER_DISPATCH:
        raise RuntimeError(
            f"Unknown AI provider '{provider}'. "
            f"Valid values: {', '.join(_PROVIDER_DISPATCH)}"
        )

    monthly_summary = summarize_by_month_category(transactions)
    user_prompt = _build_user_prompt(
        forecast_data=forecast_data,
        monthly_summary=monthly_summary,
        all_accounts=all_accounts,
        goals=goals,
        recurring=recurring,
        config=config,
    )

    raw, in_tok, out_tok = _PROVIDER_DISPATCH[provider](model, user_prompt, config)

    # Strip markdown code fences if present (some models wrap JSON in ```)
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        insights = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Model returned invalid JSON: {e}\n\nRaw response:\n{raw}")

    insights["generated_at"] = datetime.now().isoformat()
    insights["token_usage"] = {
        "input_tokens":  in_tok,
        "output_tokens": out_tok,
        "cost_usd":      _model_cost(model, in_tok, out_tok),
    }

    return insights


# ── Standalone usage ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not INSIGHTS_FILE.exists():
        print(f"No insights.json found at {INSIGHTS_FILE}")
        print("Run: python ai_daily.py")
        sys.exit(1)

    insights = json.loads(INSIGHTS_FILE.read_text())
    generated = insights.get("generated_at", "unknown")
    print(f"\n{'='*60}")
    print(f"AI Insights (generated: {generated})")
    print(f"{'='*60}")
    print(f"\nNarrative:\n  {insights.get('narrative', '')}")

    predicted = insights.get("predicted_expenses", [])
    if predicted:
        print(f"\nPredicted expenses ({len(predicted)}):")
        for p in predicted:
            conf = p.get("confidence", "?")
            amt = p.get("amount", 0)
            sign = "+" if amt > 0 else ""
            print(f"  {p.get('date', '?')}  {sign}${abs(amt):,.2f}  [{conf}]  {p.get('description', '')}")

    rec = insights.get("transfer_recommendation")
    if rec:
        print(f"\nTransfer recommendation:")
        print(f"  ${rec.get('amount', 0):,.2f} from {rec.get('source_account', '?')} by {rec.get('transfer_by', '?')}")
        print(f"  Reasoning: {rec.get('reasoning', '')}")

    surpluses = insights.get("surplus_alerts", [])
    if surpluses:
        print(f"\nSurplus alerts:")
        for s in surpluses:
            print(f"  {s.get('period', '?')}: ${s.get('estimated_surplus', 0):,.2f}")
            print(f"    → {s.get('suggested_allocation', '')}")

    flags = insights.get("risk_flags", [])
    if flags:
        print(f"\nRisk flags:")
        for f in flags:
            print(f"  ⚠ {f}")

    notes = insights.get("seasonal_notes", "")
    if notes:
        print(f"\nSeasonal notes:\n  {notes}")
