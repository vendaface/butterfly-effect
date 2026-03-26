"""
main.py — entry point for the PyInstaller-bundled Butterfly Effect app.

When running from source, use `python server.py` or `./run.sh` instead.
This wrapper:
  1. Locates startup.html (works both bundled and from source)
  2. Installs the Playwright Chromium browser on first run if missing
  3. Writes a temp copy of startup.html with the server port injected
  4. Opens it in the user's default browser
  5. Starts the Flask server in a background thread
  6. Keeps the process alive until the user quits (Ctrl-C or window close)
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

    _ensure_playwright_browser()

    from config import _load_config
    config = _load_config()
    port = config.get('app', {}).get('port', 5002)

    # Open the startup page in a background thread so the browser opens
    # while Flask is starting up in the main thread.
    startup_thread = threading.Thread(target=_open_startup, args=(port,), daemon=True)
    startup_thread.start()

    _run_flask(port)


if __name__ == '__main__':
    main()
