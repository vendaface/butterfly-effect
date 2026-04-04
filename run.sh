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

# ── Status file & server for startup screen progress ─────────────────────────
# startup.html polls http://127.0.0.1:5003/ via <script> tag (JSONP — bypasses
# file:// CORS) and reads window.__BF_STATUS to show live progress.
STATUS_FILE="/tmp/butterfly-status-$$.json"
STATUS_SERVER_PID=""

write_status() {
  # Usage: write_status STAGE PCT DETAIL [STEP] [TOTAL]
  # STAGE: starting | venv | pip | playwright | playwright_done | starting
  printf '{"stage":"%s","pct":%d,"detail":"%s","step":%d,"total":%d}\n' \
    "$1" "$2" "${3//\"/\\\"}" "${4:-0}" "${5:-0}" > "$STATUS_FILE"
}

_start_status_server() {
  # Minimal stdlib HTTP server — no pip packages needed, runs before pip install.
  "$PYTHON" - "$STATUS_FILE" <<'PYEOF' &
import http.server, sys, pathlib
sf = sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        try:    body = ('window.__BF_STATUS=' + pathlib.Path(sf).read_text().strip() + ';').encode()
        except: body = b'window.__BF_STATUS=null;'
        self.send_response(200)
        self.send_header('Content-Type', 'application/javascript; charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)
try:
    http.server.HTTPServer(('127.0.0.1', 5003), H).serve_forever()
except Exception:
    pass
PYEOF
  STATUS_SERVER_PID=$!
}

_stop_status_server() {
  [ -n "${STATUS_SERVER_PID:-}" ] && kill "$STATUS_SERVER_PID" 2>/dev/null || true
  rm -f "$STATUS_FILE"
}
trap '_stop_status_server' EXIT

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
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Python 3.11 or higher is required."
  echo ""
  if [ "$OS" = "darwin" ]; then
    echo "  Install via Homebrew:"
    echo "    brew install python@3.12"
    echo ""
    echo "  Or download from: https://python.org/downloads"
  else
    echo "  Ubuntu / Debian (including Linux Mint):"
    echo "    sudo apt update && sudo apt install python3.12 python3.12-venv"
    echo ""
    echo "  Fedora / RHEL:"
    echo "    sudo dnf install python3.12"
    echo ""
    echo "  Arch:"
    echo "    sudo pacman -S python"
  fi
  echo ""
  echo "  After installing, run ./run.sh again."
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  exit 1
fi

# On Debian/Ubuntu-based systems, python3-venv is a separate package.
# Check for it now so we can show a helpful error rather than a cryptic crash.
if [ "$OS" = "linux" ] && ! "$PYTHON" -m venv --without-pip /tmp/butterfly-venv-check &>/dev/null; then
  rm -rf /tmp/butterfly-venv-check 2>/dev/null || true
  _open_startup "e=novenv&os=$OS"
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Python venv module is missing."
  echo "  Install it with:"
  echo ""
  echo "    sudo apt install python3-venv"
  echo ""
  echo "  Then run ./run.sh again."
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  exit 1
fi
rm -rf /tmp/butterfly-venv-check 2>/dev/null || true

# ── Detect which setup steps are needed (for accurate step counter) ───────────
VENV="$SCRIPT_DIR/.venv"
PLAYWRIGHT_CACHE="$HOME/.cache/butterfly-effect/playwright"

_venv_ok=true
[ ! -x "$VENV/bin/python" ] && _venv_ok=false
if [ "$_venv_ok" = true ] && ! "$VENV/bin/pip" --version &>/dev/null 2>&1; then
  _venv_ok=false
fi
# Detect pip 25.x / Python 3.14 installation bug: packages appear installed
# (RECORD exists) but sub-package .py files were never written to disk.
# Rebuilding the venv with pip 26+ fixes this. Check all known affected packages.
if [ "$_venv_ok" = true ]; then
  if "$VENV/bin/pip" show icalendar &>/dev/null 2>&1 \
      && ! "$VENV/bin/python" -c "from icalendar import Calendar" &>/dev/null 2>&1; then
    echo "  Detected broken icalendar install (pip 25.x + Python 3.14 bug). Rebuilding venv..."
    _venv_ok=false
  fi
fi
if [ "$_venv_ok" = true ]; then
  if "$VENV/bin/pip" show websockets &>/dev/null 2>&1 \
      && ! "$VENV/bin/python" -c "import websockets.frames" &>/dev/null 2>&1; then
    echo "  Detected broken websockets install (pip 25.x + Python 3.14 bug). Rebuilding venv..."
    _venv_ok=false
  fi
fi

_need_playwright=false
{ [ ! -d "$PLAYWRIGHT_CACHE" ] || [ -z "$(ls -A "$PLAYWRIGHT_CACHE" 2>/dev/null)" ]; } \
  && _need_playwright=true

TOTAL_STEPS=2              # pip check + starting server always happen
[ "$_venv_ok" = false ]    && TOTAL_STEPS=$((TOTAL_STEPS+1))
$_need_playwright          && TOTAL_STEPS=$((TOTAL_STEPS+1))
STEP=0

# ── Open startup page and start live-progress status server ───────────────────
write_status "starting" 2 "Starting Butterfly Effect…" 0 $TOTAL_STEPS
_start_status_server
sleep 0.3   # give Python status server a moment to bind port 5003
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
# (_venv_ok and VENV already set above in step detection)
if [ "$_venv_ok" = false ]; then
  STEP=$((STEP+1))
  write_status "venv" 5 "Creating Python environment…" $STEP $TOTAL_STEPS
  echo "Creating virtual environment at $VENV ..."
  rm -rf "$VENV"
  "$PYTHON" -m venv "$VENV"
  # Upgrade pip before installing packages. pip 25.x bundled with Python 3.14.0
  # has a bug where packages that ship pre-compiled .pyc files in their wheels
  # (e.g. icalendar 7.x) don't get their .py source files written to disk.
  # We use the system Python's working pip (not the venv's bundled pip) to
  # upgrade, which is the same mechanism that created the venv.
  _py_short=$("$PYTHON" -c "import sys; v=sys.version_info; print(f'python{v.major}.{v.minor}')" 2>/dev/null || echo "python3")
  if "$PYTHON" -m pip install --upgrade pip \
      --target "$VENV/lib/$_py_short/site-packages" --quiet 2>/dev/null; then
    echo "  pip upgraded to $("$VENV/bin/pip" --version 2>/dev/null | awk '{print $2}')."
  else
    echo "  pip upgrade skipped (will use bundled version)."
  fi
fi
source "$VENV/bin/activate"

# ── Dependencies ──────────────────────────────────────────────────────────────
STEP=$((STEP+1))
write_status "pip" 18 "Installing Python packages…" $STEP $TOTAL_STEPS
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
write_status "pip" 42 "Python packages ready" $STEP $TOTAL_STEPS

# ── Playwright browser ────────────────────────────────────────────────────────
# PLAYWRIGHT_CACHE and _need_playwright set above in step detection.
export PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_CACHE"

if $_need_playwright; then
  STEP=$((STEP+1))
  write_status "playwright" 47 "Downloading Chromium browser (~150 MB)…" $STEP $TOTAL_STEPS
  echo ""
  echo "Installing Chromium browser (first time only, ~150 MB)..."

  # ── Debug: confirm playwright Python package is reachable ──
  echo "  [debug] PLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_BROWSERS_PATH"
  echo "  [debug] playwright version: $(python -m playwright --version 2>&1 || echo 'NOT FOUND')"

  mkdir -p "$PLAYWRIGHT_CACHE"

  # Capture all playwright output to a log file so we can show it on failure
  # and poll it for progress. Do NOT use a background tail pipeline — that
  # approach was fragile under set -e and misbehaved when stdout isn't a TTY.
  _PW_LOG="/tmp/butterfly-pw-$$.log"
  : > "$_PW_LOG"
  python -m playwright install chromium >"$_PW_LOG" 2>&1 &
  _PW_PID=$!
  echo "  [debug] playwright install PID=$_PW_PID, log=$_PW_LOG"

  # Poll loop: show heartbeat dots and update startup screen with any progress.
  # 'wait PID || var=$?' is safe under set -e; bare 'wait PID; var=$?' is NOT.
  printf "  Downloading"
  while kill -0 "$_PW_PID" 2>/dev/null; do
    printf "."
    # Scan log for the most recent "X.X MiB / Y.Y MiB" download progress line
    _prog=$(grep -oE '[0-9]+\.[0-9]+ MiB / [0-9]+\.[0-9]+ MiB' "$_PW_LOG" 2>/dev/null | tail -1 || true)
    if [ -n "$_prog" ]; then
      _mb=$(printf '%s' "$_prog" | awk '{print $1}')
      _mbt=$(printf '%s' "$_prog" | awk '{print $4}')
      _dp=$(awk "BEGIN{p=int($_mb*100/$_mbt+0.5);print(p>100?100:p)}" 2>/dev/null || echo 0)
      _op=$(( 47 + _dp * 44 / 100 ))
      write_status "playwright" "$_op" "Downloading Chromium: $_mb of $_mbt MB" $STEP $TOTAL_STEPS
    fi
    sleep 2
  done
  echo ""

  # Collect exit code safely — 'wait PID || var=$?' won't trigger set -e
  _PW_EXIT=0
  wait "$_PW_PID" || _PW_EXIT=$?

  # Always dump the playwright output — essential for diagnosing failures
  echo "  [debug] playwright exited with code: $_PW_EXIT"
  echo "  [debug] playwright log ($(wc -l < "$_PW_LOG" 2>/dev/null || echo '?') lines):"
  cat "$_PW_LOG" || true
  rm -f "$_PW_LOG"

  if [ $_PW_EXIT -ne 0 ]; then
    echo ""
    echo "WARNING: Chromium browser install failed (exit code $_PW_EXIT)."
    echo "  The app will still start, but 'Connect to Monarch' may not work."
    echo "  To retry manually: PLAYWRIGHT_BROWSERS_PATH=$PLAYWRIGHT_CACHE python -m playwright install chromium"
  else
    write_status "playwright_done" 93 "Browser download complete" $STEP $TOTAL_STEPS
    echo "  Browser install complete."
  fi
fi

# ── Launch ────────────────────────────────────────────────────────────────────
STEP=$((STEP+1))
write_status "starting" 96 "Starting server…" $STEP $TOTAL_STEPS
echo "Starting Balance Forecast at http://localhost:5002"
"$PYTHON" server.py &
SERVER_PID=$!
wait $SERVER_PID
