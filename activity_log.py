"""Tracks the most recent message timestamp per monitored Telegram chat
(Ken/James "AI Reels Gen" groups, Bjarne's Trends-research group).

Used by the strike system to check "did anyone send anything into this
chat within the last 24h" — persisted on the volume so it survives restarts.
"""
import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
LOG_PATH = "/data/activity_log.json"


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


def record_activity(chat_id: int) -> None:
    data = _load()
    data[str(chat_id)] = datetime.now(BERLIN).isoformat()
    _save(data)


def has_activity_in_last_24h(chat_id: int) -> bool:
    last = _load().get(str(chat_id))
    if not last:
        return False
    cutoff = (datetime.now(BERLIN) - timedelta(hours=24)).isoformat()
    return last >= cutoff
