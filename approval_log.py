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


def log_created(
    token: str, model_name: str, file_name: str, va_name: str, *,
    content_type: str = "", niche: str = "", topic_id: int | None = None,
    slots: list | None = None, behave_target: dict | None = None,
) -> None:
    """content_type/niche/topic_id/slots/behave_target let a restart-orphaned
    approval be fully reconstructed from Drive later (see telegram_bot.py
    _recover_pending_from_log) — without them only model/file/va survive,
    enough for the deadline check but not enough to resend the video."""
    data = _prune(_load())
    data[token] = {
        "created_at": datetime.now(BERLIN).isoformat(),
        "model_name": model_name,
        "file_name": file_name,
        "va_name": va_name,
        "content_type": content_type,
        "niche": niche,
        "topic_id": topic_id,
        "slots": slots or [],
        "behave_target": behave_target,
        "resolved": False,
    }
    _save(data)


def set_resolved(token: str, resolved: bool) -> None:
    data = _load()
    if token in data:
        data[token]["resolved"] = resolved
        _save(data)


def get_entry(token: str) -> dict | None:
    return _load().get(token)


def overdue_unresolved(min_review_hours: int, deadline_hour: int, deadline_minute: int, buffer_minutes: int) -> list[dict]:
    """Videos whose *effective* deadline has passed.

    The effective deadline is the LATER of:
      (a) the next daily deadline_hour:deadline_minute (+buffer) on/after upload, or
      (b) upload time + min_review_hours

    (b) exists so a video a VA uploads right before the daily cutoff (e.g. 12:55)
    or very late at night (e.g. 19:00, rolling the "next 13:00" too close) still
    gives Bjarne a fair minimum window, instead of near-zero or an unreasonably
    short one — he's not at fault for when a VA happens to deliver.
    """
    now = datetime.now(BERLIN)
    data = _load()
    overdue = []
    for e in data.values():
        if e["resolved"]:
            continue
        created = datetime.fromisoformat(e["created_at"])
        daily_cutoff = created.replace(hour=deadline_hour, minute=deadline_minute, second=0, microsecond=0)
        if daily_cutoff <= created:
            daily_cutoff += timedelta(days=1)
        daily_cutoff += timedelta(minutes=buffer_minutes)
        min_cutoff = created + timedelta(hours=min_review_hours)
        effective_deadline = max(daily_cutoff, min_cutoff)
        if now >= effective_deadline:
            overdue.append(e)
    return overdue
