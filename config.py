"""
config.py — application configuration and environment variable management.

Owns:
  - config.yaml loading, merging, and saving
  - .env key reading / writing
  - Setup status checks (is_first_run, setup_status)
  - Path constants for config.yaml and .env
"""

import os
import shutil
from pathlib import Path

import yaml
from dotenv import load_dotenv

from storage import _atomic_write

# ── Path constants ─────────────────────────────────────────────────────────────

from paths import APP_DATA_DIR   # noqa: E402 — after stdlib imports

_BASE = APP_DATA_DIR

_CONFIG_PATH = _BASE / "config.yaml"
_ENV_PATH    = _BASE / ".env"

load_dotenv(_ENV_PATH)

# Keys that must never be sent to the browser as plaintext
_SENSITIVE_ENV_KEYS = {
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
}


# ── config.yaml ───────────────────────────────────────────────────────────────

def _deep_merge(base: dict, updates: dict) -> dict:
    """Recursively merge *updates* into *base*, returning a new dict."""
    result = dict(base)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _load_config() -> dict:
    """Load config.yaml; bootstrap from config.yaml.example on first run."""
    if not _CONFIG_PATH.exists():
        example = _CONFIG_PATH.parent / "config.yaml.example"
        if example.exists():
            shutil.copy(example, _CONFIG_PATH)
        else:
            raise RuntimeError(
                "config.yaml not found. Copy config.yaml.example and fill in values."
            )
    return yaml.safe_load(_CONFIG_PATH.read_text())


def _save_config(config: dict) -> None:
    """Write config dict back to config.yaml atomically."""
    _atomic_write(_CONFIG_PATH, yaml.dump(config, default_flow_style=False, allow_unicode=True))


# ── Setup status ──────────────────────────────────────────────────────────────

def _setup_status() -> dict:
    """Return which required setup items are complete and which are still missing."""
    try:
        account_id = _load_config().get("monarch", {}).get("checking_account_id", "")
    except Exception:
        account_id = ""
    account_ok = bool(account_id and account_id != "PASTE_ACCOUNT_ID_HERE")
    missing = [] if account_ok else ["primary account selection"]
    return {
        "complete":   not missing,
        "missing":    missing,
        "account_ok": account_ok,
    }


def _is_first_run() -> bool:
    """True if minimum required setup is not complete."""
    return not _setup_status()["complete"]


# ── .env management ───────────────────────────────────────────────────────────

def _env_key_status(key: str) -> str:
    """Return 'configured' or 'not_configured' — never the actual value.

    Reads .env directly so it's accurate even if the server started before the
    key was added.
    """
    # Fast path: already in os.environ (set at startup or by _update_env_key)
    if os.getenv(key, "").strip():
        return "configured"
    # Fallback: read .env file directly
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith(f"{key}="):
                val = line[len(f"{key}="):].strip().strip('"').strip("'")
                if val:
                    os.environ[key] = val   # sync into os.environ for future calls
                    return "configured"
    return "not_configured"


def _update_env_key(key: str, value: str) -> None:
    """Update or add key=value in .env; also update os.environ in-memory."""
    lines = _ENV_PATH.read_text().splitlines() if _ENV_PATH.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    _atomic_write(_ENV_PATH, "\n".join(lines) + "\n")
    os.environ[key] = value   # pick up immediately without restart


def _read_env_value(key: str) -> str:
    """Return the actual value of an env key (from os.environ or .env). Empty string if not set."""
    val = os.getenv(key, "").strip()
    if val:
        return val
    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line[len(f"{key}="):].strip().strip('"').strip("'")
    return ""
