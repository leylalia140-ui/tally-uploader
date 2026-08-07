"""Reads deadlines live from the Notion "Tasks Database Agency" for recurring
tasks that don't happen every day (e.g. Sherry's Reels list, every 3 days) —
so the strike system follows whatever's actually entered in Notion instead of
a hardcoded cadence that would drift out of sync if the schedule changes there.
"""
import logging
from datetime import date, datetime

import httpx

from config import NOTION_TOKEN, NOTION_TASKS_DB_ID

logger = logging.getLogger(__name__)


async def get_deadline_for_date(task_title: str, assigned_to: str, target_date: date) -> tuple[int, int] | None:
    """Looks for a task in the Tasks Database Agency whose title contains
    `task_title`, is assigned to `assigned_to`, and has its Deadline on
    `target_date`. Returns (hour, minute) of that deadline, or None if no
    such task exists for that date (i.e. it's not a due day)."""
    if not NOTION_TOKEN:
        logger.warning("NOTION_TOKEN not set, skipping Notion-gated deadline check")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.notion.com/v1/databases/{NOTION_TASKS_DB_ID}/query",
                headers={
                    "Authorization": f"Bearer {NOTION_TOKEN}",
                    "Notion-Version": "2022-06-28",
                },
                json={
                    "filter": {
                        "and": [
                            {"property": "Tasks", "title": {"contains": task_title}},
                            {"property": "Deadline", "date": {"equals": target_date.isoformat()}},
                        ]
                    }
                },
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
    except Exception as e:
        logger.error(f"Notion deadline lookup failed for '{task_title}' on {target_date}: {e}")
        return None

    for r in results:
        props = r["properties"]
        assigned = [x.get("name") for x in props.get("Assigned To", {}).get("multi_select", [])]
        if assigned_to not in assigned:
            continue
        deadline = props.get("Deadline", {}).get("date")
        if not deadline or not deadline.get("start"):
            continue
        dt = datetime.fromisoformat(deadline["start"])
        return dt.hour, dt.minute

    return None
