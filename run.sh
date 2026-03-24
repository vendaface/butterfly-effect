#!/usr/bin/env bash
# Balance Forecast — double-click to start
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── OS detection ──────────────────────────────────────────────────────────────
OS="linux"
[[ "$OSTYPE" == "darwin"* ]] && OS="darwin"

# ── Helper: open startup.html with optional query string ──────────────────────
# macOS 'open' treats bare paths with '?' as filenames, so use file:// URL with
# spaces percent-encoded (the only special char likely in a home directory path).
_STARTUP_URL="file://${SCRIPT_DIR// /%20}/startup.html"
_open_startup() {
  open "${_STARTUP_URL}$1" 2>/dev/null || xdg-open "${_STARTUP_URL}$1" 2>/dev/null || true
}

# ── Open startup page immediately (before any slow checks) ────────────────────
_open_startup "?os=$OS"

# ── Python detection ──────────────────────────────────────────────────────────
PYTHON=""
for candidate in python3 python3.13 python3.12 python3.11; do
  if command -v "$candidate" &>/dev/null; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  fi
done
if [ -z "$PYTHON" ]; then
  _open_startup "?e=nopython&os=$OS"
  echo "Error: Python 3.11+ required. See the browser window for install instructions."
  exit 1
fi

# ── Bootstrap config files on first run ──────────────────────────────────────
if [ ! -f config.yaml ]; then
  echo "First run: creating config.yaml from example..."
  cp config.yaml.example config.yaml
fi
if [ ! -f .env ]; then
  echo "First run: creating .env (credentials file)..."
  touch .env
fi

# ── Virtual environment ───────────────────────────────────────────────────────
# Stored outside iCloud Drive to prevent macOS from evicting venv files.
VENV="$HOME/.cache/balance-forecast-venv"
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
  _open_startup "?e=pipfail&os=$OS"
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
python server.py &
SERVER_PID=$!
sleep 2
# startup.html handles the browser redirect on macOS; xdg-open fallback for Linux
xdg-open "http://localhost:5002" 2>/dev/null || true
wait $SERVER_PID
