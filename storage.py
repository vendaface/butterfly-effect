"""
storage.py — disk I/O for application data files.

Owns:
  - Atomic file write primitive
  - JSON schema validation helper
  - All JSON data-file loaders (scenarios, overrides, skips, amounts, insights)
  - Corrections parsing / writing (user_context.md)
  - Path constants for every runtime data file
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

# ── Path constants ─────────────────────────────────────────────────────────────

from paths import APP_DATA_DIR   # noqa: E402

_BASE = APP_DATA_DIR

_INSIGHTS_FILE               = _BASE / "insights.json"
_USER_CONTEXT_FILE           = _BASE / "user_context.md"
_USER_CONTEXT_TEMPLATE       = "# AI Corrections\n\n"   # default when file doesn't exist yet
_PAYMENT_OVERRIDES_FILE      = _BASE / "payment_overrides.json"
_PAYMENT_SKIPS_FILE          = _BASE / "payment_skips.json"
_PAYMENT_MONTHLY_AMOUNTS_FILE = _BASE / "payment_monthly_amounts.json"
_SCENARIOS_FILE               = _BASE / "scenarios.json"
_ACCOUNTS_CACHE_FILE          = _BASE / "monarch_accounts_cache.json"
_DISMISSED_SUGGESTIONS_FILE   = _BASE / "dismissed_suggestions.json"
_PAYMENT_DAY_OVERRIDES_FILE   = _BASE / "payment_day_overrides.json"
_MONARCH_RAW_CACHE_FILE       = _BASE / "monarch_raw_cache.json"

# ── Regex patterns for user_context.md parsing ────────────────────────────────

# New flat format:  - [YYYY-MM-DD] [Type] text
_CORRECTION_LINE_RE = re.compile(
    r'^- \[(\d{4}-\d{2}-\d{2})\] \[(Correction|Known Fact|Note)\] (.+)$'
)
# Old dated format: - [YYYY-MM-DD] text  (no type tag)
_OLD_DATED_LINE_RE = re.compile(r'^- \[(\d{4}-\d{2}-\d{2})\] (.+)$')


# ── Atomic write ───────────────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically.

    Writes to a sibling .tmp file first, then uses os.replace() to swap it
    into place — so readers always see a complete file even if the process
    is interrupted mid-write.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)   # owner-only before rename
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # also fix if destination already existed with looser perms
    except OSError:
        pass


# ── Schema validation ─────────────────────────────────────────────────────────

def _check_list_schema(
    path: Path,
    data: object,
    required_str_keys: tuple[str, ...] = (),
    required_num_keys: tuple[str, ...] = (),
) -> list[dict]:
    """Validate that *data* is a list of dicts with the expected key types.

    Returns a filtered list containing only records that pass validation.
    Logs a warning (to stdout) for each dropped record — never raises.
    """
    if not isinstance(data, list):
        print(f"[schema] {path.name}: expected a list, got {type(data).__name__} — ignoring file")
        return []
    good = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            print(f"[schema] {path.name}[{i}]: expected dict, got {type(item).__name__} — skipping")
            continue
        bad = False
        for k in required_str_keys:
            if not isinstance(item.get(k), str) or not item[k].strip():
                print(f"[schema] {path.name}[{i}]: missing/invalid string field '{k}' — skipping")
                bad = True
                break
        if not bad:
            for k in required_num_keys:
                if not isinstance(item.get(k), (int, float)):
                    print(f"[schema] {path.name}[{i}]: missing/invalid numeric field '{k}' — skipping")
                    bad = True
                    break
        if not bad:
            good.append(item)
    return good


# ── Corrections (user_context.md) ─────────────────────────────────────────────

def _parse_corrections() -> list[dict]:
    """Parse user_context.md into a list of correction dicts.

    Supports both:
      New flat format:  - [YYYY-MM-DD] [Type] text
      Old section format: lines under ## Corrections / ## Known Facts / ## Notes
    """
    if not _USER_CONTEXT_FILE.exists():
        return []
    lines = _USER_CONTEXT_FILE.read_text().splitlines()
    results: list[dict] = []
    cur_type = "Correction"  # tracks section for old-format migration
    for i, raw in enumerate(lines):
        if raw.startswith("## Corrections"):
            cur_type = "Correction"; continue
        elif raw.startswith("## Known Facts"):
            cur_type = "Known Fact"; continue
        elif raw.startswith("## Notes"):
            cur_type = "Note"; continue
        elif raw.startswith("## "):
            cur_type = "Correction"; continue
        m = _CORRECTION_LINE_RE.match(raw)
        if m:
            results.append({"id": f"c{i}", "date": m.group(1),
                            "type": m.group(2), "text": m.group(3), "raw": raw})
            continue
        m2 = _OLD_DATED_LINE_RE.match(raw)
        if m2:
            results.append({"id": f"c{i}", "date": m2.group(1),
                            "type": cur_type, "text": m2.group(2), "raw": raw})
    return results


def _write_corrections(corrections: list[dict]) -> None:
    """Write corrections list back to user_context.md in new flat format."""
    file_lines = ["# AI Corrections", ""]
    for c in corrections:
        file_lines.append(f"- [{c['date']}] [{c['type']}] {c['text']}")
    _atomic_write(_USER_CONTEXT_FILE, "\n".join(file_lines) + "\n")


# ── JSON data-file loaders ─────────────────────────────────────────────────────

def _load_scenarios() -> list[dict]:
    """Load user-defined scenario events from scenarios.json."""
    if not _SCENARIOS_FILE.exists():
        return []
    try:
        data = json.loads(_SCENARIOS_FILE.read_text())
        return _check_list_schema(
            _SCENARIOS_FILE, data,
            required_str_keys=("date", "description"),
            required_num_keys=("amount",),
        )
    except Exception:
        return []


def _load_payment_overrides() -> dict:
    """Load {name_lower: {name, amount, note, updated}} from payment_overrides.json."""
    if not _PAYMENT_OVERRIDES_FILE.exists():
        return {}
    try:
        data = json.loads(_PAYMENT_OVERRIDES_FILE.read_text())
        if not isinstance(data, dict):
            print(f"[schema] {_PAYMENT_OVERRIDES_FILE.name}: expected a dict — ignoring file")
            return {}
        good = {}
        for k, v in data.items():
            if not isinstance(v, dict):
                print(f"[schema] {_PAYMENT_OVERRIDES_FILE.name}[{k!r}]: expected dict value — skipping")
                continue
            if not isinstance(v.get("name"), str) or not isinstance(v.get("amount"), (int, float)):
                print(f"[schema] {_PAYMENT_OVERRIDES_FILE.name}[{k!r}]: missing name/amount — skipping")
                continue
            good[k] = v
        return good
    except Exception:
        return {}


def _load_payment_skips() -> list:
    """Load [{name, month, note}] from payment_skips.json."""
    if not _PAYMENT_SKIPS_FILE.exists():
        return []
    try:
        data = json.loads(_PAYMENT_SKIPS_FILE.read_text())
        return _check_list_schema(
            _PAYMENT_SKIPS_FILE, data,
            required_str_keys=("name", "month"),
        )
    except Exception:
        return []


def _load_payment_monthly_amounts() -> list:
    """Load [{name, month, amount, note}] from payment_monthly_amounts.json."""
    if not _PAYMENT_MONTHLY_AMOUNTS_FILE.exists():
        return []
    try:
        data = json.loads(_PAYMENT_MONTHLY_AMOUNTS_FILE.read_text())
        return _check_list_schema(
            _PAYMENT_MONTHLY_AMOUNTS_FILE, data,
            required_str_keys=("name",),
            required_num_keys=("amount",),
        )
    except Exception:
        return []


def _load_insights() -> dict | None:
    """Load insights.json if it exists. Returns None if missing or unreadable."""
    if not _INSIGHTS_FILE.exists():
        return None
    try:
        return json.loads(_INSIGHTS_FILE.read_text())
    except Exception:
        return None


def _load_dismissed_suggestions() -> list:
    """Load list of dismissed suggestion fingerprint strings from dismissed_suggestions.json."""
    if not _DISMISSED_SUGGESTIONS_FILE.exists():
        return []
    try:
        data = json.loads(_DISMISSED_SUGGESTIONS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_dismissed_suggestions(dismissed: list) -> None:
    """Persist dismissed suggestion fingerprints atomically."""
    _atomic_write(_DISMISSED_SUGGESTIONS_FILE, json.dumps(dismissed, indent=2))


def _load_payment_day_overrides() -> dict:
    """Load {name_lower: {name, day, note}} from payment_day_overrides.json."""
    if not _PAYMENT_DAY_OVERRIDES_FILE.exists():
        return {}
    try:
        data = json.loads(_PAYMENT_DAY_OVERRIDES_FILE.read_text())
        if not isinstance(data, dict):
            return {}
        good = {}
        for k, v in data.items():
            if not isinstance(v, dict):
                continue
            if not isinstance(v.get("name"), str):
                continue
            if not isinstance(v.get("day"), int) or not (1 <= v["day"] <= 28):
                continue
            good[k] = v
        return good
    except Exception:
        return {}


# ── Monarch raw data disk cache ───────────────────────────────────────────────

def _load_monarch_raw_cache() -> dict | None:
    """Load raw Monarch data (balance, transactions, recurring, fetched_at) from disk.

    Returns None if the file is missing, unreadable, or structurally invalid.
    The returned dict always contains a 'fetched_at' ISO timestamp so callers
    can determine how old the data is.
    """
    if not _MONARCH_RAW_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(_MONARCH_RAW_CACHE_FILE.read_text())
        if not isinstance(data, dict):
            return None
        required = ("fetched_at", "balance", "transactions", "recurring")
        if not all(k in data for k in required):
            return None
        return data
    except Exception:
        return None


def _save_monarch_raw_cache(balance, transactions: list, recurring: list) -> None:
    """Persist raw Monarch data to disk atomically with a fetched_at timestamp."""
    payload = {
        "fetched_at":   datetime.now().isoformat(),
        "balance":      balance,
        "transactions": transactions,
        "recurring":    recurring,
    }
    _atomic_write(_MONARCH_RAW_CACHE_FILE, json.dumps(payload))
