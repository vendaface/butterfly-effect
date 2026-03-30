"""
paths.py — canonical application data directory.

All user data files (config.yaml, .env, *.json, user_context.md,
browser_state.json) are stored in APP_DATA_DIR regardless of where the
app is installed or run from.

Zero extra dependencies — safe to import from any module without risk
of circular imports.
"""

import os
import platform
from pathlib import Path

if platform.system() == 'Darwin':
    APP_DATA_DIR = Path.home() / 'Library' / 'Application Support' / 'Butterfly Effect'
else:
    # Linux: follow XDG Base Directory specification
    _xdg = os.environ.get('XDG_DATA_HOME', '')
    APP_DATA_DIR = (Path(_xdg) if _xdg else Path.home() / '.local' / 'share') / 'butterfly-effect'

APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
