"""
main.py — entry point for the PyInstaller-bundled Butterfly Effect app.

When running from source, use `python server.py` or `./run.sh` instead.
This wrapper:
  1. Downloads the Playwright Chromium browser in the background on first
     run (needed for Monarch data fetching, not the UI)
  2. Starts the Flask server in a background thread
  3. Waits for Flask to be ready, then opens a native macOS window via
     pywebview (WKWebView) — no external browser required
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Resource path (works both bundled and from source) ─────────────────────
BASE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))

# ── Playwright browser cache — stored in user's home, survives app updates ─
PLAYWRIGHT_CACHE = Path.home() / '.cache' / 'butterfly-effect' / 'playwright'


def _bootstrap_data_dir():
    """Create config.yaml and .env in APP_DATA_DIR if they don't exist yet.

    Mirrors what run.sh does for the source-based launch so the bundled
    app works correctly on first run without any shell script.
    """
    from paths import APP_DATA_DIR
    config = APP_DATA_DIR / 'config.yaml'
    env    = APP_DATA_DIR / '.env'
    if not config.exists():
        example = BASE_DIR / 'config.yaml.example'
        if example.exists():
            import shutil
            shutil.copy(example, config)
    if not env.exists():
        env.touch(mode=0o600)


# Set to True once Chromium is confirmed present; False if install failed.
# server.py reads this to decide whether to attempt a synchronous install.
_browser_ready: bool = False


def _install_chromium(env: dict) -> bool:
    """Run playwright install chromium. Returns True on success."""
    PLAYWRIGHT_CACHE.mkdir(parents=True, exist_ok=True)
    if getattr(sys, 'frozen', False):
        # In a PyInstaller bundle sys.executable is the app binary, not
        # Python. The playwright package bundles Node.js + a CLI script:
        #   playwright/driver/node            ← Node.js binary
        #   playwright/driver/package/cli.js  ← Playwright CLI
        driver_dir = Path(sys._MEIPASS) / 'playwright' / 'driver'
        node = driver_dir / 'node'
        cli  = driver_dir / 'package' / 'cli.js'
        if node.exists():
            try:
                node.chmod(node.stat().st_mode | 0o111)  # ensure executable bit
            except Exception:
                pass
        cmd = [str(node), str(cli), 'install', 'chromium']
        # When Node.js runs inside a PyInstaller bundle, V8's JIT compiler
        # fails to reserve virtual memory for its CodeRange because PyInstaller
        # has already claimed the contiguous region it needs. --jitless disables
        # the JIT entirely, avoiding the reservation and the resulting SIGTRAP.
        env = {**env, 'NODE_OPTIONS': '--jitless'}
    else:
        cmd = [sys.executable, '-m', 'playwright', 'install', 'chromium']
    try:
        subprocess.run(cmd, env=env, check=True)
        return True
    except Exception as exc:
        print(f"ERROR: Failed to install Chromium browser: {exc}", file=sys.stderr)
        return False


def _ensure_playwright_browser():
    """Install Chromium on first launch if it's not already present."""
    global _browser_ready
    if PLAYWRIGHT_CACHE.exists() and any(PLAYWRIGHT_CACHE.iterdir()):
        _browser_ready = True
        return
    print("First run: downloading Chromium browser (one-time, ~150 MB)...")
    env = {**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(PLAYWRIGHT_CACHE)}
    _browser_ready = _install_chromium(env)


def _run_flask(port: int):
    """Start Flask in this thread (blocking). Port check is done in main()."""
    from server import app
    from config import _load_config
    config = _load_config()
    debug = config.get('app', {}).get('debug', False)
    print(f'Butterfly Effect running at http://localhost:{port}')
    app.run(host='127.0.0.1', port=port, debug=debug, use_reloader=False)


def _wait_for_flask(port: int, timeout: float = 15.0) -> bool:
    """Poll /_ping until Flask is actually serving responses, not just bound."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f'http://127.0.0.1:{port}/_ping', timeout=1)
            return True
        except Exception:
            time.sleep(0.1)
    return False


# ── Inline loading screen shown immediately while the forecast computes ────
# pywebview (WKWebView) shows a blank white screen while waiting for a slow
# HTTP response. We sidestep this by loading a self-contained HTML string
# first, then navigating to the real URL once the forecast is cached.
_LOADING_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Butterfly Effect</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body {
    height: 100%; width: 100%;
    background: #0d1117;
    display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', sans-serif;
    color: #e6edf3;
  }
  .wrap { text-align: center; }
  .icon { width: 72px; height: 72px; margin: 0 auto 24px; animation: flutter 2s ease-in-out infinite; }
  @keyframes flutter {
    0%, 100% { transform: translateY(0) rotate(-5deg); }
    50%       { transform: translateY(-12px) rotate(5deg); }
  }
  h1 { font-size: 28px; font-weight: 600; letter-spacing: -0.5px; margin-bottom: 10px; }
  p  { font-size: 15px; color: #8b949e; }
  .dots::after {
    content: '';
    animation: dots 1.5s steps(4, end) infinite;
  }
  @keyframes dots {
    0%   { content: ''; }
    25%  { content: '.'; }
    50%  { content: '..'; }
    75%  { content: '...'; }
    100% { content: ''; }
  }
</style>
</head>
<body>
  <div class="wrap">
    <svg class="icon" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <defs>
        <linearGradient id="lh-ul" x1="50" y1="54" x2="5"  y2="24" gradientUnits="userSpaceOnUse">
          <stop offset="0%"   stop-color="#1a6fff"/>
          <stop offset="100%" stop-color="#5ee7d4"/>
        </linearGradient>
        <linearGradient id="lh-ur" x1="50" y1="54" x2="95" y2="24" gradientUnits="userSpaceOnUse">
          <stop offset="0%"   stop-color="#1a6fff"/>
          <stop offset="100%" stop-color="#30d158"/>
        </linearGradient>
        <linearGradient id="lh-ll" x1="50" y1="60" x2="22" y2="88" gradientUnits="userSpaceOnUse">
          <stop offset="0%"   stop-color="#005ec0"/>
          <stop offset="100%" stop-color="#34c759"/>
        </linearGradient>
        <linearGradient id="lh-lr" x1="50" y1="60" x2="78" y2="88" gradientUnits="userSpaceOnUse">
          <stop offset="0%"   stop-color="#005ec0"/>
          <stop offset="100%" stop-color="#5ee7d4"/>
        </linearGradient>
      </defs>
      <path d="M 50,48 C 40,25 12,12 5,26 C 1,40 14,56 50,60 Z"   fill="url(#lh-ul)"/>
      <path d="M 50,48 C 60,25 88,12 95,26 C 99,40 86,56 50,60 Z"  fill="url(#lh-ur)"/>
      <path d="M 50,60 C 38,64 20,72 22,84 C 24,94 40,92 50,66 Z"  fill="url(#lh-ll)"/>
      <path d="M 50,60 C 62,64 80,72 78,84 C 76,94 60,92 50,66 Z"  fill="url(#lh-lr)"/>
      <ellipse cx="50" cy="54" rx="3" ry="22" fill="#0d1229"/>
      <path d="M 49,32 Q 38,17 33,9" stroke="#0d1229" stroke-width="1.5" fill="none" stroke-linecap="round"/>
      <circle cx="33" cy="9" r="1.8" fill="#1a6fff"/>
      <path d="M 51,32 Q 62,17 67,9" stroke="#0d1229" stroke-width="1.5" fill="none" stroke-linecap="round"/>
      <circle cx="67" cy="9" r="1.8" fill="#30d158"/>
    </svg>
    <h1>Butterfly Effect</h1>
    <p>Loading your forecast<span class="dots"></span></p>
    <p id="lh-sub" style="font-size:13px;color:#636366;margin-top:8px;min-height:1.2em;"></p>
    <script>
      // After 8 s without navigation, hint that a first-run Chromium download
      // may be happening silently in the background (non-blocking to the UI).
      setTimeout(function(){
        var el=document.getElementById('lh-sub');
        if(el) el.textContent='Downloading Chromium for Monarch data (first run, ~150\u202fMB)\u2026';
      }, 8000);
    </script>
  </div>
</body>
</html>"""


def _preload_and_navigate(port: int, window) -> None:
    """Pre-fetch GET / to warm the forecast cache, then navigate the window."""
    import urllib.request
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=60)
    except Exception:
        pass  # Even on error, navigate — Flask will show its own error page
    window.load_url(f'http://localhost:{port}/')


def main():
    _bootstrap_data_dir()

    # Point Playwright at our dedicated cache directory
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(PLAYWRIGHT_CACHE)

    from config import _load_config
    config = _load_config()
    port = config.get('app', {}).get('port', 5002)

    # Check port before starting anything so we can exit cleanly if it's taken.
    # Done here (not in _run_flask) so the window never opens on a port conflict.
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
        if _s.connect_ex(('127.0.0.1', port)) == 0:
            print(
                f'\nERROR: Port {port} is already in use.\n'
                f'The app may already be running at http://localhost:{port}\n'
                f'If not, run:  lsof -ti :{port} | xargs kill -9\n',
                file=sys.stderr,
            )
            sys.exit(1)

    # Download Chromium in the background — needed for Monarch data fetching,
    # not the UI. Will be ready long before the user clicks "Connect to Monarch".
    threading.Thread(target=_ensure_playwright_browser, daemon=True).start()

    # Flask runs in a background thread so pywebview can own the main thread
    # (required on macOS).
    threading.Thread(target=lambda: _run_flask(port), daemon=True).start()

    # Open the window IMMEDIATELY — no waiting for Flask.
    # This means the dock icon starts bouncing as soon as Python finishes
    # initializing, with no 1–2 s delay while Flask spins up.
    # The inline loading screen is shown in the window from the first frame;
    # _wait_for_flask + the forecast pre-fetch run in a background thread.
    import webview
    window = webview.create_window(
        'Butterfly Effect',
        html=_LOADING_HTML,
        width=1400,
        height=900,
        min_size=(900, 600),
    )

    def _on_shown():
        # pywebview already runs _on_shown in its own thread; no extra thread
        # needed here.  Calling window.load_url() from a sub-thread fails
        # silently on WKWebView, leaving the loading screen stuck forever.
        if not _wait_for_flask(port):
            print("ERROR: Flask server did not start in time.", file=sys.stderr)
            # Navigate anyway — Flask may have started but /_ping timed out;
            # the user will see an error page rather than a frozen butterfly.
        _preload_and_navigate(port, window)

    webview.start(_on_shown)
    # webview.start() blocks until the window is closed
    sys.exit(0)


if __name__ == '__main__':
    main()
