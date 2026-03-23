"""
Monarch Money data layer — Playwright-based.

Uses a real Chromium browser (via Playwright) so that Cloudflare's TLS fingerprinting
passes. Intercepts the GraphQL calls the Monarch web app makes natively.

Browser state (cookies/localStorage) is persisted to browser_state.json so you only
log in once. If the session expires the browser window will appear for re-login.

Standalone usage:
  python monarch_client.py --list-accounts
  python monarch_client.py --check
"""

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
import os

load_dotenv()

BROWSER_STATE_FILE = Path(__file__).parent / "browser_state.json"
GRAPHQL_URL = "https://api.monarch.com/graphql"
LOGIN_URL = "https://app.monarchmoney.com/login"
DASHBOARD_URL = "https://app.monarchmoney.com/dashboard"
ACCOUNTS_URL = "https://app.monarchmoney.com/accounts"
TRANSACTIONS_URL = "https://app.monarchmoney.com/transactions"
RECURRING_URL = "https://app.monarchmoney.com/recurring"
GOALS_URL = "https://app.monarchmoney.com/goals"

# JS injected into the visible Playwright window during data collection
_OVERLAY_JS = """(function() {
  // Only cover Monarch data-collection pages — never the login page or MFA screens.
  // This script is registered as an init script before any navigation, so the URL check
  // is critical to prevent blocking the user from completing login/2FA.
  var url = window.location.href;
  if (url.indexOf('/dashboard')    === -1 &&
      url.indexOf('/accounts')     === -1 &&
      url.indexOf('/transactions') === -1 &&
      url.indexOf('/recurring')    === -1) return;
  if (document.getElementById('__bf_overlay__')) return;
  var el = document.createElement('div');
  el.id = '__bf_overlay__';
  el.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;'
    + 'background:rgba(0,0,0,0.82);z-index:2147483647;display:flex;'
    + 'align-items:center;justify-content:center;pointer-events:all;cursor:default;';
  el.innerHTML = '<div style="text-align:center;color:#fff;font-family:system-ui,sans-serif;'
    + 'max-width:360px;padding:40px;background:rgba(255,255,255,0.07);'
    + 'border-radius:16px;border:1px solid rgba(255,255,255,0.15);">'
    + '<div style="font-size:2rem;margin-bottom:14px;">\u23f3</div>'
    + '<div style="font-size:1.05rem;font-weight:600;margin-bottom:10px;">'
    + 'Fetching your data from Monarch\u2026</div>'
    + '<div style="font-size:0.85rem;opacity:0.7;line-height:1.6;">'
    + 'Please keep this window open.<br>It will close automatically when done.</div>'
    + '</div>';
  document.body.appendChild(el);
})();"""

# Static loading page shown immediately in the blank Chrome tab before any navigation.
# set_content() is a local DOM operation (no network request), so this appears in ~10 ms —
# eliminating the blank-window gap between Chrome opening and the first page.goto() completing.
_LOADING_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: 100%; height: 100%; }
body { background: #111; display: flex; align-items: center; justify-content: center;
       font-family: system-ui, -apple-system, sans-serif; }
.card { text-align: center; color: #fff; max-width: 360px; padding: 40px;
        background: rgba(255,255,255,0.07); border-radius: 16px;
        border: 1px solid rgba(255,255,255,0.15); }
.icon { font-size: 2rem; margin-bottom: 14px; }
.title { font-size: 1.05rem; font-weight: 600; margin-bottom: 10px; }
.sub { font-size: 0.85rem; opacity: 0.7; line-height: 1.6; }
</style></head>
<body><div class="card">
  <div class="icon">&#x23F3;</div>
  <div class="title">Fetching your data from Monarch&hellip;</div>
  <div class="sub">Please keep this window open.<br>It will close automatically when done.</div>
</div></body></html>"""

# ── GraphQL response collector ────────────────────────────────────────────────

class GraphQLCollector:
    """Collects and parses GraphQL responses intercepted from the browser."""

    def __init__(self, debug: bool = False):
        self._responses: list[dict] = []
        self.debug = debug

    def make_handler(self):
        """Return a sync handler that schedules async work; safe for page.on()."""
        import asyncio

        async def _handle(response):
            url = response.url
            # In debug mode print ALL URLs so we can see what the browser loads
            if self.debug:
                print(f"  [net] {response.status} {url[:100]}")
            if "monarch.com" not in url:
                return
            try:
                body = await response.json()
            except Exception:
                return
            if self.debug:
                keys = list(body.keys()) if isinstance(body, dict) else type(body).__name__
                print(f"    → JSON keys: {keys}")
            if isinstance(body, dict) and "data" in body:
                self._responses.append(body["data"])

        def _sync_handler(response):
            asyncio.get_event_loop().create_task(_handle(response))

        return _sync_handler

    def flush(self) -> list[dict]:
        collected = list(self._responses)
        self._responses.clear()
        return collected

    def find_accounts(self) -> list[dict]:
        # Priority 1: accounts nested inside accountTypeSummaries (has names + balances)
        for data in self._responses:
            ats = data.get("accountTypeSummaries")
            if isinstance(ats, list):
                all_accts = []
                for grp in ats:
                    grp_accts = grp.get("accounts", [])
                    if isinstance(grp_accts, list):
                        all_accts.extend(grp_accts)
                if all_accts:
                    return all_accts

        # Priority 2: top-level 'accounts' key with displayBalance or currentBalance
        for data in self._responses:
            accts = data.get("accounts")
            if isinstance(accts, list) and accts and isinstance(accts[0], dict):
                if "displayBalance" in accts[0] or "currentBalance" in accts[0]:
                    return accts

        # Priority 3: any list whose items have displayBalance (avoids savings goals)
        for data in self._responses:
            for val in data.values():
                if (isinstance(val, list) and val
                        and isinstance(val[0], dict)
                        and "displayBalance" in val[0]):
                    return val

        return []

    def find_transactions(self) -> list[dict]:
        for data in self._responses:
            for val in data.values():
                if isinstance(val, dict):
                    results = val.get("results", [])
                    if results and isinstance(results[0], dict) and "amount" in results[0]:
                        return results
                if isinstance(val, list) and val and isinstance(val[0], dict) and "amount" in val[0]:
                    return val
        return []

    def find_all_accounts(self) -> list[dict]:
        """Return ALL accounts (not just checking) for AI source-account selection."""
        return self.find_accounts()

    def find_goals(self) -> list[dict]:
        """Return Monarch savings goals from intercepted GraphQL responses."""
        for data in self._responses:
            for key, val in data.items():
                if "goal" in key.lower() and isinstance(val, list) and val:
                    return val
                if isinstance(val, dict):
                    for subkey, subval in val.items():
                        if "goal" in subkey.lower() and isinstance(subval, list) and subval:
                            return subval
        return []

    def find_recurring(self) -> list[dict]:
        """Return recurring stream objects (unwrapped from the {stream: {...}} wrapper)."""
        candidates = []
        for data in self._responses:
            for key, val in data.items():
                if "recurring" in key.lower() and isinstance(val, list) and val:
                    candidates.append(val)
                elif isinstance(val, dict):
                    for subkey, subval in val.items():
                        if "recurring" in subkey.lower() and isinstance(subval, list) and subval:
                            candidates.append(subval)
        # Prefer the largest list (the full recurring page response)
        if not candidates:
            return []
        best = max(candidates, key=len)
        # Unwrap {stream: {...}, __typename: ...} wrapper if present
        first = best[0]
        if isinstance(first, dict) and "stream" in first and isinstance(first["stream"], dict):
            return [item["stream"] for item in best if isinstance(item.get("stream"), dict)]
        return best


# ── Browser session ───────────────────────────────────────────────────────────

async def _ensure_logged_in(page, context) -> bool:
    """Navigate to dashboard; return True if already logged in, False if login needed."""
    await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(2000)
    return "login" not in page.url


async def _inject_overlay(page):
    """
    Inject a full-page blocking overlay into the visible Chrome window.
    Called after login and before data-collection navigations so the user
    cannot accidentally close or interact with the browser while data is fetched.
    Registered as an init script so it re-appears automatically on every goto().
    Non-fatal — overlay is UX sugar, not required for data correctness.
    """
    try:
        await page.add_init_script(_OVERLAY_JS)  # persists across future navigations
        await page.evaluate(_OVERLAY_JS)          # apply to the current page immediately
    except Exception:
        pass


async def _do_login(page, context):
    """
    Interactive login: opens the Monarch login page and waits for the user to sign in.
    If MONARCH_EMAIL / MONARCH_PASSWORD happen to be in .env they are pre-filled as a
    convenience; otherwise the user types their credentials directly in the Chrome window.
    The user always clicks Sign In and handles 2FA themselves.
    Script resumes automatically once the dashboard URL is detected.
    """
    email = os.getenv("MONARCH_EMAIL", "")
    password = os.getenv("MONARCH_PASSWORD", "")

    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)

    # Pre-fill if credentials are available; do NOT auto-submit — let user click Sign In and handle 2FA
    try:
        if email:
            await page.locator('input[type="email"], input[name="email"]').first.fill(email)
        if password:
            await page.locator('input[type="password"]').first.fill(password)
    except Exception:
        pass  # If pre-fill fails, user can type manually

    print()
    print("=" * 60)
    print("  Monarch login window is open.")
    print("  1. Click 'Sign In' in the browser")
    print("  2. Complete 2FA if prompted")
    print("  Script continues automatically once you reach the dashboard.")
    print("=" * 60)

    try:
        # 3-minute window — plenty of time for 2FA
        await page.wait_for_url("**dashboard**", timeout=180000)
    except Exception:
        raise RuntimeError(
            "Login timed out (3 min). Please try connecting again."
        )

    # Cover the page immediately — user is on the dashboard but data collection
    # hasn't started yet. Overlay prevents accidental clicks or window closure.
    # add_init_script registration here persists across all subsequent goto() calls.
    await _inject_overlay(page)

    # Save browser state — all future runs will be headless
    await context.storage_state(path=str(BROWSER_STATE_FILE))
    print("✓ Session saved — future runs will be headless.")


async def _fetch_all(checking_account_id: str, history_days: int, _retried: bool = False):
    from playwright.async_api import async_playwright

    has_saved_state = BROWSER_STATE_FILE.exists()

    async with async_playwright() as pw:
        # Use saved state if available (headless), otherwise show browser for login
        launch_kwargs = {
            "headless": has_saved_state,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        }
        browser = await pw.chromium.launch(**launch_kwargs)

        context_kwargs = {}
        if has_saved_state:
            context_kwargs["storage_state"] = str(BROWSER_STATE_FILE)

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        # Show a loading page immediately in the blank tab before any network navigation.
        # set_content() is local (~10 ms), so the user sees something the instant Chrome opens
        # rather than staring at a blank white tab for the 3–20 s that goto() can take.
        if not has_saved_state:
            try:
                await page.set_content(_LOADING_HTML)
            except Exception:
                pass  # non-fatal — falls back to blank tab

        # Register overlay for all future navigations to whitelisted Monarch pages.
        # The URL whitelist in _OVERLAY_JS ensures login and MFA pages are never covered.
        if not has_saved_state:
            await page.add_init_script(_OVERLAY_JS)

        collector = GraphQLCollector()
        page.on("response", collector.make_handler())

        logged_in = await _ensure_logged_in(page, context)

        if not logged_in:
            if has_saved_state:
                # Saved session expired — delete and relaunch visibly (one retry only)
                BROWSER_STATE_FILE.unlink(missing_ok=True)
                await browser.close()
                if _retried:
                    raise RuntimeError("Monarch session expired and re-login failed. Please reconnect via Settings.")
                print("Session expired. Relaunching browser for re-login...")
                return await _fetch_all(checking_account_id, history_days, _retried=True)
            await _do_login(page, context)

        # ── Collect accounts (bank accounts live on /accounts, not dashboard) ──
        collector.flush()
        await page.goto(ACCOUNTS_URL, wait_until="load", timeout=30000)
        await page.wait_for_timeout(4000)
        dash_data = collector.flush()

        # ── Collect transactions ──────────────────────────────────────────────
        start_date = (datetime.now() - timedelta(days=history_days)).strftime("%Y-%m-%d")
        await page.goto(
            f"{TRANSACTIONS_URL}?startDate={start_date}",
            wait_until="load",
            timeout=30000,
        )
        await page.wait_for_timeout(4000)
        txn_data = collector.flush()

        # ── Collect recurring ─────────────────────────────────────────────────
        await page.goto(RECURRING_URL, wait_until="load", timeout=30000)
        await page.wait_for_timeout(4000)
        recur_data = collector.flush()

        await browser.close()

    # Merge all collected data for searching
    all_data_responses = dash_data + txn_data + recur_data
    collector._responses = all_data_responses

    # ── Extract current balance ───────────────────────────────────────────────
    accounts = collector.find_accounts()
    balance = None
    for acct in accounts:
        acct_id = str(acct.get("id", ""))
        if acct_id == str(checking_account_id):
            balance = float(acct.get("currentBalance") or acct.get("displayBalance") or 0)
            break

    if balance is None:
        # Fall back: print what we got so user can identify the right ID
        print("\nCould not find account ID. Available accounts from GraphQL responses:")
        for acct in accounts:
            bal = acct.get("currentBalance") or acct.get("displayBalance")
            print(f"  id={acct.get('id')}  name={acct.get('displayName') or acct.get('name')}  balance={bal}")
        raise RuntimeError(
            f"Account ID '{checking_account_id}' not found in Monarch data. "
            "Update monarch.checking_account_id in config.yaml."
        )

    transactions = collector.find_transactions()
    recurring = collector.find_recurring()

    return balance, transactions, recurring


async def _fetch_all_extended(checking_account_id: str, history_days: int, _retried: bool = False):
    """
    Extended version of _fetch_all that also scrapes the goals page and returns
    all account balances (not just checking).

    Returns: (balance, transactions, recurring, all_accounts, goals)
    """
    from playwright.async_api import async_playwright

    has_saved_state = BROWSER_STATE_FILE.exists()

    async with async_playwright() as pw:
        launch_kwargs = {
            "headless": has_saved_state,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        }
        browser = await pw.chromium.launch(**launch_kwargs)

        context_kwargs = {}
        if has_saved_state:
            context_kwargs["storage_state"] = str(BROWSER_STATE_FILE)

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        collector = GraphQLCollector()
        page.on("response", collector.make_handler())

        logged_in = await _ensure_logged_in(page, context)

        if not logged_in:
            if has_saved_state:
                BROWSER_STATE_FILE.unlink(missing_ok=True)
                await browser.close()
                if _retried:
                    raise RuntimeError("Monarch session expired and re-login failed. Please reconnect via Settings.")
                print("Session expired. Relaunching browser for re-login...")
                return await _fetch_all_extended(checking_account_id, history_days, _retried=True)
            await _do_login(page, context)

        # ── Collect accounts ──────────────────────────────────────────────────
        collector.flush()
        await page.goto(ACCOUNTS_URL, wait_until="load", timeout=30000)
        await page.wait_for_timeout(4000)
        dash_data = collector.flush()

        # ── Collect transactions ──────────────────────────────────────────────
        start_date = (datetime.now() - timedelta(days=history_days)).strftime("%Y-%m-%d")
        await page.goto(
            f"{TRANSACTIONS_URL}?startDate={start_date}",
            wait_until="load",
            timeout=30000,
        )
        await page.wait_for_timeout(4000)
        txn_data = collector.flush()

        # ── Collect recurring ─────────────────────────────────────────────────
        await page.goto(RECURRING_URL, wait_until="load", timeout=30000)
        await page.wait_for_timeout(4000)
        recur_data = collector.flush()

        # ── Collect goals ─────────────────────────────────────────────────────
        await page.goto(GOALS_URL, wait_until="load", timeout=30000)
        await page.wait_for_timeout(4000)
        goals_data = collector.flush()

        await browser.close()

    # Merge all collected data
    all_data_responses = dash_data + txn_data + recur_data + goals_data
    collector._responses = all_data_responses

    # ── Extract current balance ───────────────────────────────────────────────
    accounts = collector.find_accounts()
    all_accounts = collector.find_all_accounts()
    balance = None
    for acct in accounts:
        acct_id = str(acct.get("id", ""))
        if acct_id == str(checking_account_id):
            balance = float(acct.get("currentBalance") or acct.get("displayBalance") or 0)
            break

    if balance is None:
        print("\nCould not find account ID. Available accounts:")
        for acct in accounts:
            bal = acct.get("currentBalance") or acct.get("displayBalance")
            print(f"  id={acct.get('id')}  name={acct.get('displayName') or acct.get('name')}  balance={bal}")
        raise RuntimeError(
            f"Account ID '{checking_account_id}' not found in Monarch data. "
            "Update monarch.checking_account_id in config.yaml."
        )

    # Re-load goals from goals_data only (separate search)
    collector._responses = goals_data
    goals = collector.find_goals()

    # Restore full responses for transactions + recurring
    collector._responses = all_data_responses
    transactions = collector.find_transactions()
    recurring = collector.find_recurring()

    return balance, transactions, recurring, all_accounts, goals


async def _fetch_accounts_async() -> list[dict]:
    """
    Lightweight accounts-only fetch. Navigates only to the accounts page
    (~15 s with a saved browser session). Returns list of account dicts.
    """
    from playwright.async_api import async_playwright

    has_saved_state = BROWSER_STATE_FILE.exists()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=has_saved_state,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context_kwargs = {}
        if has_saved_state:
            context_kwargs["storage_state"] = str(BROWSER_STATE_FILE)
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        # Register overlay before any navigation (same early-injection pattern as _fetch_all)
        if not has_saved_state:
            await page.add_init_script(_OVERLAY_JS)

        collector = GraphQLCollector()
        page.on("response", collector.make_handler())

        logged_in = await _ensure_logged_in(page, context)
        if not logged_in:
            collector.flush()
            await _do_login(page, context)
            await page.wait_for_load_state("load", timeout=15000)
            await page.wait_for_timeout(4000)
        else:
            collector.flush()
            await page.goto(ACCOUNTS_URL, wait_until="load", timeout=30000)
            await page.wait_for_timeout(4000)

        acct_data = collector.flush()
        await browser.close()

    collector._responses = acct_data
    return collector.find_accounts()


async def _list_accounts_async(debug: bool = False):
    from playwright.async_api import async_playwright

    has_saved_state = BROWSER_STATE_FILE.exists()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=has_saved_state,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context_kwargs = {}
        if has_saved_state:
            context_kwargs["storage_state"] = str(BROWSER_STATE_FILE)
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        collector = GraphQLCollector(debug=debug)
        page.on("response", collector.make_handler())

        logged_in = await _ensure_logged_in(page, context)
        if not logged_in:
            if has_saved_state:
                BROWSER_STATE_FILE.unlink(missing_ok=True)
                await browser.close()
                return await _list_accounts_async()
            # Fresh login — clear noise captured during login page load, then
            # wait for GraphQL calls that fired on the dashboard after redirect
            collector.flush()
            await _do_login(page, context)
            # Already on dashboard after login — just let network settle
            await page.wait_for_load_state("load", timeout=15000)
            await page.wait_for_timeout(4000)
        else:
            # Headless run with saved session — navigate to accounts page to trigger
            # the GraphQL query that returns actual bank account data
            collector.flush()
            await page.goto(ACCOUNTS_URL, wait_until="load", timeout=30000)
            await page.wait_for_timeout(4000)

        acct_data = collector.flush()
        await browser.close()

    collector._responses = acct_data
    if debug:
        print(f"\n[debug] Total GraphQL responses captured: {len(acct_data)}")
        for i, d in enumerate(acct_data):
            print(f"  response[{i}] keys: {list(d.keys())}")
            if "accounts" in d:
                accts = d["accounts"]
                if isinstance(accts, list) and accts:
                    print(f"    accounts[0] full: {accts[0]}")
            if "accountTypeSummaries" in d:
                ats = d["accountTypeSummaries"]
                if isinstance(ats, list) and ats:
                    print(f"    accountTypeSummaries[0] keys: {list(ats[0].keys())}")
                    for grp in ats[:3]:
                        accts_in_grp = grp.get("accounts", [])
                        if accts_in_grp:
                            print(f"      type={grp.get('type')} accounts[0]: {accts_in_grp[0]}")
    accounts = collector.find_accounts()
    if not accounts:
        print("No accounts found in GraphQL responses.")
        if not debug:
            print("Re-run with --debug to see what was captured:")
            print("  python monarch_client.py --list-accounts --debug")
        return

    print(f"\n{'ID':<30} {'Name':<35} Balance")
    print("-" * 75)
    for a in accounts:
        bal = a.get("currentBalance") or a.get("displayBalance")
        acct_type = (a.get("type") or {}).get("name", "") if isinstance(a.get("type"), dict) else str(a.get("type", ""))
        print(
            f"{str(a.get('id', '')):<30} "
            f"{(a.get('displayName') or a.get('name') or ''):<35} "
            f"{str(bal):<15} {acct_type}"
        )


# ── Public API ────────────────────────────────────────────────────────────────

def get_data(checking_account_id: str, history_days: int = 45):
    """
    Main entry point for other modules.
    Returns (current_balance: float, transactions: list, recurring: list)
    """
    return asyncio.run(_fetch_all(checking_account_id, history_days))


def get_full_data(checking_account_id: str, history_days: int = 395):
    """
    Extended fetch for AI analysis. Scrapes accounts, transactions (13 months
    by default), recurring, and savings goals in a single browser session.

    Returns: (balance, transactions, recurring, all_accounts, goals)
      - all_accounts: list of all Monarch accounts with balances (for source-account selection)
      - goals: list of Monarch savings goals (for surplus routing)
    """
    return asyncio.run(_fetch_all_extended(checking_account_id, history_days))


def get_accounts() -> list[dict]:
    """
    Lightweight fetch: returns all Monarch accounts (id, name, balance, type).
    Uses the saved browser session (~15 s). No transaction history fetched.
    Called by the settings page to populate the primary account dropdown.
    """
    return asyncio.run(_fetch_accounts_async())


def list_accounts(debug: bool = False):
    asyncio.run(_list_accounts_async(debug=debug))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monarch Money client (Playwright)")
    parser.add_argument("--list-accounts", action="store_true", help="Print all accounts with IDs")
    parser.add_argument("--list-recurring", action="store_true", help="Print ALL recurring items (to identify duplicates)")
    parser.add_argument("--check", action="store_true", help="Print balance + recent txns + recurring")
    parser.add_argument("--debug", action="store_true", help="Print all intercepted network responses")
    args = parser.parse_args()

    config_path = Path(__file__).parent / "config.yaml"
    config = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    account_id = config.get("monarch", {}).get("checking_account_id", "")

    if args.list_accounts:
        list_accounts(debug=args.debug)
    elif args.list_recurring:
        if not account_id or account_id == "PASTE_ACCOUNT_ID_HERE":
            print("Set monarch.checking_account_id in config.yaml first.")
        else:
            _, _, recurring = get_data(account_id)
            print(f"\nAll recurring items ({len(recurring)}), sorted by name:\n")
            print(f"{'Name':<45} {'Amount':>10}  {'Freq':<15}  {'Base date':<12}  Type")
            print("-" * 100)
            for r in sorted(recurring, key=lambda x: (x.get("name") or "").lower()):
                name = r.get("name") or r.get("description") or "?"
                amt = float(r.get("amount") or 0)
                freq = r.get("frequency") or "?"
                base = str(r.get("baseDate") or "")[:10]
                rtype = r.get("recurringType") or "?"
                print(f"{name:<45} ${amt:>9,.2f}  {freq:<15}  {base:<12}  {rtype}")
    else:
        if not account_id or account_id == "PASTE_ACCOUNT_ID_HERE":
            print("Set monarch.checking_account_id in config.yaml first.")
            print("Run: python monarch_client.py --list-accounts")
        else:
            balance, txns, recurring = get_data(account_id)
            print(f"\nCurrent balance: ${balance:,.2f}")
            print(f"Recent transactions: {len(txns)}")
            for t in txns[:8]:
                amt = float(t.get("amount", 0))
                sign = "-" if amt < 0 else "+"
                name = (t.get("merchant") or {}).get("name") or t.get("description") or ""
                acct = (t.get("account") or {}).get("displayName") or ""
                print(f"  {t.get('date', '')}  {sign}${abs(amt):,.2f}  {name}  [{acct}]")
            print(f"Recurring items: {len(recurring)}")
            for r in recurring[:5]:
                print(f"  {r.get('name') or r.get('description') or ''}  ${r.get('amount', 0):,.2f}")
