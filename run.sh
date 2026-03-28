#!/usr/bin/env bash
# Balance Forecast — double-click to start
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── OS detection ──────────────────────────────────────────────────────────────
OS="linux"
[[ "$OSTYPE" == "darwin"* ]] && OS="darwin"

# ── Clean up leftover startup temp files from previous runs ──────────────────
rm -f /tmp/butterfly-startup-*.html 2>/dev/null || true

# ── Helper: open startup.html with optional hash params ───────────────────────
# Copy to a unique temp path on each call so macOS 'open' always opens a fresh
# tab — it would focus an existing tab if the same file:// path were reused,
# preventing hash params (error codes etc.) from being seen by the page.
_open_startup() {
  # $1 = optional param string WITHOUT leading #, e.g. "e=nopython&os=darwin"
  # macOS 'open' silently drops URL fragments for file:// URLs, so we can't
  # pass error codes via location.hash. Instead we inject them directly into
  # the HTML as a JS variable before the browser ever opens the file.
  local params="${1:-}"
  local tmp="/tmp/butterfly-startup-$$.html"
  cp "$SCRIPT_DIR/startup.html" "$tmp"
  chmod 600 "$tmp" 2>/dev/null || true   # readable only by the current user
  if [ -n "$params" ]; then
    # Escape & → \& so sed doesn't expand it as "matched text" in replacement.
    local safe="${params//&/\\&}"
    sed -i.bak "s|</head>|<script>window.__BF_PARAMS='${safe}';</script></head>|" "$tmp"
    rm -f "${tmp}.bak"
  fi
  open "file://${tmp}" 2>/dev/null || xdg-open "file://${tmp}" 2>/dev/null || true
}

# ── Python detection ──────────────────────────────────────────────────────────
# Check Python BEFORE opening any browser page so that on failure we open the
# error URL as the very first (fresh) tab — no existing tab to conflict with.
PYTHON=""
for candidate in python3 python3.13 python3.12 python3.11; do
  if command -v "$candidate" &>/dev/null; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  fi
done
# Allow forcing the no-python error path for testing: SIMULATE_NO_PYTHON=1 ./run.sh
[ "${SIMULATE_NO_PYTHON:-}" = "1" ] && PYTHON=""

if [ -z "$PYTHON" ]; then
  _open_startup "e=nopython&os=$OS"
  echo "Error: Python 3.11+ required. See the browser window for install instructions."
  exit 1
fi

# ── Open startup page (Python confirmed present) ──────────────────────────────
_open_startup

# ── Application data directory ────────────────────────────────────────────────
if [ "$OS" = "darwin" ]; then
  APP_DATA="$HOME/Library/Application Support/Butterfly Effect"
else
  APP_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/butterfly-effect"
fi
mkdir -p "$APP_DATA"

# ── One-time migration: move existing data files to Application Support ───────
for f in config.yaml .env browser_state.json insights.json user_context.md \
          payment_overrides.json payment_skips.json payment_monthly_amounts.json \
          payment_day_overrides.json scenarios.json monarch_accounts_cache.json \
          dismissed_suggestions.json; do
  if [ -f "$SCRIPT_DIR/$f" ] && [ ! -f "$APP_DATA/$f" ]; then
    mv "$SCRIPT_DIR/$f" "$APP_DATA/$f"
    echo "Migrated $f to Application Support"
  fi
done

# ── Bootstrap config files on first run ──────────────────────────────────────
if [ ! -f "$APP_DATA/config.yaml" ]; then
  echo "First run: creating config.yaml..."
  cp "$SCRIPT_DIR/config.yaml.example" "$APP_DATA/config.yaml"
fi
if [ ! -f "$APP_DATA/.env" ]; then
  echo "First run: creating .env (credentials file)..."
  touch "$APP_DATA/.env"
fi

# ── Virtual environment ───────────────────────────────────────────────────────
VENV="$SCRIPT_DIR/.venv"
_venv_ok=true
if [ ! -x "$VENV/bin/python" ]; then
  _venv_ok=false
elif ! "$VENV/bin/pip" --version &>/dev/null 2>&1; then
  _venv_ok=false
fi

if [ "$_venv_ok" = false ]; then
  echo "Creating virtual environment at $VENV ..."
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV"
fi
source "$VENV/bin/activate"

# ── Dependencies ──────────────────────────────────────────────────────────────
printf "Checking dependencies"
pip install -q -r requirements.txt &
PIP_PID=$!
while kill -0 "$PIP_PID" 2>/dev/null; do printf "."; sleep 1; done
wait "$PIP_PID"; PIP_EXIT=$?
if [ $PIP_EXIT -ne 0 ]; then
  echo " failed."
  _open_startup "e=pipfail&os=$OS"
  echo "Error: dependency install failed. See the browser window for details."
  exit 1
fi
echo " done."

# ── Playwright browser ────────────────────────────────────────────────────────
if ! python -c "from playwright.sync_api import sync_playwright; sync_playwright().__enter__().chromium.executable_path" &>/dev/null 2>&1; then
  printf "Installing browser (first time only, may take a minute)"
  playwright install chromium &>/dev/null &
  PW_PID=$!
  while kill -0 "$PW_PID" 2>/dev/null; do printf "."; sleep 2; done
  wait "$PW_PID"
  echo " done."
fi

# ── Launch ────────────────────────────────────────────────────────────────────
echo "Starting Balance Forecast at http://localhost:5002"
"$PYTHON" server.py &
SERVER_PID=$!
wait $SERVER_PID
