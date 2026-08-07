"""Persistent log of approval-group uploads, independent of the in-memory
PENDING_APPROVALS dict (which is wiped on every Railway restart/deploy).

Needed so the 13:00 deadline check can reliably tell "was there a video
uploaded today that's still neither approved nor rejected", even if a
restart happened between upload and check.
"""
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
LOG_PATH = "/data/approval_log.json"
RETENTION_DAYS = 5


def _load() -> dict:
    if not os.path.exists(LOG_PATH):
        return {}
    with open(LOG_PATH) as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    tmp_path = LOG_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, LOG_PATH)


def _prune(data: dict) -> dict:
    cutoff = (datetime.now(BERLIN) - timedelta(days=RETENTION_DAYS)).isoformat()
    return {t: e for t, e in data.items() if e["created_at"] >= cutoff}


def log_created(token: str, model_name: str, file_name: str, va_name: str) -> None:
    data = _prune(_load())
    data[token] = {
        "created_at": datetime.now(BERLIN).isoformat(),
        "model_name": model_name,
        "file_name": file_name,
        "va_name": va_name,
        "resolved": False,
    }
    _save(data)


def set_resolved(token: str, resolved: bool) -> None:
    data = _load()
    if token in data:
        data[token]["resolved"] = resolved
        _save(data)


def unresolved_created_in_last_24h() -> list[dict]:
    """Videos created since the previous deadline check that are still unresolved.

    Using a rolling 24h window (rather than calendar-day matching) means a
    video uploaded after today's 13:00 check rolls into tomorrow's window
    instead of being silently skipped, while a video that's been sitting
    unresolved for longer than 24h only ever counts once (the day it missed
    its own deadline) — matching how the deadline is meant to behave.
    """
    cutoff = (datetime.now(BERLIN) - timedelta(hours=24)).isoformat()
    data = _load()
    return [e for e in data.values() if not e["resolved"] and e["created_at"] >= cutoff]
