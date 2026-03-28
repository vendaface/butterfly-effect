"""
main.py — entry point for the PyInstaller-bundled Butterfly Effect app.

When running from source, use `python server.py` or `./run.sh` instead.
This wrapper:
  1. Starts the Flask server immediately in the main thread
  2. Opens startup.html in the browser after a short delay (polls /_ping,
     auto-redirects to the dashboard once Flask is ready)
  3. Downloads the Playwright Chromium browser in the background on first
     run — doesn't block startup; ready long before the user needs it
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

# ── Resource path (works both bundled and from source) ─────────────────────
BASE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))

# ── Playwright browser cache — stored in user's home, survives app updates ─
PLAYWRIGHT_CACHE = Path.home() / '.cache' / 'butterfly-effect' / 'playwright'


def _ensure_playwright_browser():
    """Install Chromium on first launch if it's not already present."""
    # If any chromium directory exists under the cache, assume it's installed.
    if PLAYWRIGHT_CACHE.exists() and any(PLAYWRIGHT_CACHE.iterdir()):
        return
    print("First run: downloading Chromium browser (one-time, ~150 MB)...")
    PLAYWRIGHT_CACHE.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': str(PLAYWRIGHT_CACHE)}
    try:
        subprocess.run(
            [sys.executable, '-m', 'playwright', 'install', 'chromium'],
            env=env, check=True
        )
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: Failed to install Chromium browser: {exc}", file=sys.stderr)
        sys.exit(1)


def _open_startup(port: int):
    """Write a temp copy of startup.html with the port injected, then open it."""
    src = BASE_DIR / 'startup.html'
    if not src.exists():
        # Fallback: just open the app directly
        webbrowser.open(f'http://localhost:{port}')
        return

    html = src.read_text(encoding='utf-8')
    # Inject port so the startup page knows where to ping
    html = html.replace('</head>',
        f'<script>window.__BF_PORT={port};</script></head>', 1)

    fd, tmp = tempfile.mkstemp(prefix='butterfly-startup-', suffix='.html')
    try:
        os.chmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(html)
        webbrowser.open(f'file://{tmp}')
        # Keep the temp file alive long enough for the browser to read it
        time.sleep(5)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _run_flask(port: int):
    """Start Flask in this thread (blocking)."""
    import socket
    # Check port availability before starting Flask so we can show a clear
    # error instead of letting Flask print a cryptic message and crash-loop.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(('127.0.0.1', port)) == 0:
            print(
                f'\nERROR: Port {port} is already in use.\n'
                f'The app may already be running at http://localhost:{port}\n'
                f'If not, run:  lsof -ti :{port} | xargs kill -9\n',
                file=sys.stderr,
            )
            sys.exit(1)
    # Import here so PyInstaller can trace the dependency
    from server import app
    from config import _load_config
    config = _load_config()
    debug = config.get('app', {}).get('debug', False)
    print(f'Butterfly Effect running at http://localhost:{port}')
    app.run(host='127.0.0.1', port=port, debug=debug, use_reloader=False)


def main():
    # Point Playwright at our dedicated cache directory
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(PLAYWRIGHT_CACHE)

    from config import _load_config
    config = _load_config()
    port = config.get('app', {}).get('port', 5002)

    # Download Chromium in the background — don't block Flask startup.
    # The user won't need it until they click "Connect to Monarch", by
    # which point the ~150 MB download will almost certainly be done.
    browser_thread = threading.Thread(target=_ensure_playwright_browser, daemon=True)
    browser_thread.start()

    # Open the startup page after a short pause so Flask has time to bind.
    # startup.html polls /_ping and auto-redirects once the server is up.
    def _delayed_open():
        time.sleep(0.75)
        _open_startup(port)
    startup_thread = threading.Thread(target=_delayed_open, daemon=True)
    startup_thread.start()

    # Run Flask in the main thread (blocking — keeps the process alive).
    _run_flask(port)


if __name__ == '__main__':
    main()
