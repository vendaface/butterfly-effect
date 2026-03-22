#!/usr/bin/env bash
# Balance Forecast — double-click to start
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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
  echo "Error: Python 3.11 or higher is required."
  echo "Install it from https://www.python.org/downloads/ or: brew install python@3.12"
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
VENV=".venv-balance-forecast"
if [ ! -d "$VENV" ]; then
  echo "Creating virtual environment..."
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
  echo "Error: could not install dependencies. Check that your Python environment is healthy."
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
echo "Opening browser..."
open "http://localhost:5002" 2>/dev/null || xdg-open "http://localhost:5002" 2>/dev/null || true
wait $SERVER_PID
