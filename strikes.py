"""Persistent strike tracking (survives Railway restarts — stored on the mounted volume).

Strikes reset automatically each month: a person's "current" count is always
computed by filtering entries whose date falls in the current Berlin month,
so no explicit reset job is needed.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
STRIKES_PATH = "/data/strikes.json"


def _load() -> dict:
    if not os.path.exists(STRIKES_PATH):
        return {}
    with open(STRIKES_PATH) as f:
        return json.load(f)


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(STRIKES_PATH), exist_ok=True)
    tmp_path = STRIKES_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, STRIKES_PATH)


def add_strike(person: str, reason: str) -> dict:
    now = datetime.now(BERLIN)
    entry = {
        "date": now.strftime("%Y-%m-%d"),
        "reason": reason,
        "revoked": False,
        "revoked_by": None,
        "revoked_at": None,
        "created_at": now.isoformat(),
    }
    data = _load()
    data.setdefault(person, []).append(entry)
    _save(data)
    return entry


def revoke_strike(person: str, date_str: str | None, revoked_by: str) -> dict | None:
    """Revoke a specific strike by date, or the most recent active one if date_str is None."""
    data = _load()
    entries = data.get(person, [])
    candidates = [e for e in entries if not e["revoked"] and (date_str is None or e["date"] == date_str)]
    if not candidates:
        return None
    target = candidates[-1]
    target["revoked"] = True
    target["revoked_by"] = revoked_by
    target["revoked_at"] = datetime.now(BERLIN).isoformat()
    _save(data)
    return target


LAUNCH_AT_PATH = "/data/strike_launch_at.txt"


def get_or_create_launch_at() -> datetime:
    """The exact moment the strike feature first went live, persisted so restarts
    don't reset it. Used per-check (not per-day): a deadline occurrence whose
    deadline+buffer was already in the past at this moment is skipped forever,
    but any occurrence still upcoming at launch fires normally the same day —
    e.g. deploying at 19:51 skips today's already-passed 13:00 check but still
    lets tonight's 23:00/23:59 checks fire on schedule."""
    if os.path.exists(LAUNCH_AT_PATH):
        with open(LAUNCH_AT_PATH) as f:
            return datetime.fromisoformat(f.read().strip())
    now = datetime.now(BERLIN)
    os.makedirs(os.path.dirname(LAUNCH_AT_PATH), exist_ok=True)
    with open(LAUNCH_AT_PATH, "w") as f:
        f.write(now.isoformat())
    return now


def has_strike_today(person: str) -> bool:
    today = datetime.now(BERLIN).strftime("%Y-%m-%d")
    return any(e["date"] == today for e in _load().get(person, []))


def current_month_strikes(person: str) -> list[dict]:
    month = datetime.now(BERLIN).strftime("%Y-%m")
    entries = _load().get(person, [])
    return [e for e in entries if e["date"].startswith(month) and not e["revoked"]]


def to_eu_date(iso_date: str) -> str:
    """'2026-08-08' -> '08.08.26' — dates are stored as ISO internally (sortable/filterable),
    only converted to the European dotted format for display in messages."""
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%y")


def parse_eu_date(eu_date: str) -> str:
    """'08.08.26' -> '2026-08-08' — accepts the format users see in messages and type back
    into /removestrike."""
    return datetime.strptime(eu_date, "%d.%m.%y").strftime("%Y-%m-%d")
