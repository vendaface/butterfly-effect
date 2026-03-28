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


def _ensure_playwright_browser():
    """Install Chromium on first launch if it's not already present."""
    if PLAYWRIGHT_CACHE.exists() and any(PLAYWRIGHT_CACHE.iterdir()):
        return
    print("First run: downloading Chromium browser (one-time, ~150 MB)...")
    PLAYWRIGHT_CACHE.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(PLAYWRIGHT_CACHE)}
    try:
        if getattr(sys, 'frozen', False):
            # In a PyInstaller bundle sys.executable is the app binary, not
            # Python. The playwright package bundles Node.js + a CLI script:
            #   playwright/driver/node            ← Node.js binary
            #   playwright/driver/package/cli.js  ← Playwright CLI
            driver_dir = Path(sys._MEIPASS) / 'playwright' / 'driver'
            node = driver_dir / 'node'
            cli  = driver_dir / 'package' / 'cli.js'
            cmd = [str(node), str(cli), 'install', 'chromium']
        else:
            cmd = [sys.executable, '-m', 'playwright', 'install', 'chromium']
        subprocess.run(cmd, env=env, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: Failed to install Chromium browser: {exc}", file=sys.stderr)


def _run_flask(port: int):
    """Start Flask in this thread (blocking)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(('127.0.0.1', port)) == 0:
            print(
                f'\nERROR: Port {port} is already in use.\n'
                f'The app may already be running at http://localhost:{port}\n'
                f'If not, run:  lsof -ti :{port} | xargs kill -9\n',
                file=sys.stderr,
            )
            sys.exit(1)
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
  .emoji { font-size: 64px; margin-bottom: 24px; animation: flutter 2s ease-in-out infinite; }
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
    <div class="emoji">🦋</div>
    <h1>Butterfly Effect</h1>
    <p>Loading your forecast<span class="dots"></span></p>
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

    # Download Chromium in the background — needed for Monarch data fetching,
    # not the UI. Will be ready long before the user clicks "Connect to Monarch".
    browser_thread = threading.Thread(target=_ensure_playwright_browser, daemon=True)
    browser_thread.start()

    # Flask runs in a background thread so pywebview can own the main thread
    # (required on macOS).
    flask_thread = threading.Thread(target=lambda: _run_flask(port), daemon=True)
    flask_thread.start()

    # Wait for Flask to be ready before opening the window
    if not _wait_for_flask(port):
        print("ERROR: Flask server did not start in time.", file=sys.stderr)
        sys.exit(1)

    # Open the app in a native macOS window (WKWebView via pywebview).
    # Load the inline loading screen immediately (no white flash), then
    # pre-fetch GET / in the background and navigate once it's cached.
    import webview
    window = webview.create_window(
        'Butterfly Effect',
        html=_LOADING_HTML,
        width=1400,
        height=900,
        min_size=(900, 600),
    )

    def _on_shown():
        preload_thread = threading.Thread(
            target=_preload_and_navigate,
            args=(port, window),
            daemon=True,
        )
        preload_thread.start()

    webview.start(_on_shown)
    # webview.start() blocks until the window is closed
    sys.exit(0)


if __name__ == '__main__':
    main()
