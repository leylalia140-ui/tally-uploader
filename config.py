import os


class Settings:
    GOOGLE_DRIVE_ROOT_FOLDER_ID: str = os.environ.get(
        "GOOGLE_DRIVE_ROOT_FOLDER_ID", "1ofu3b6xK4Hdnk0n_vGD-y9cQNSg7lY2U"
    )
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TG_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    TALLY_SIGNING_SECRET: str | None = os.environ.get("TALLY_SIGNING_SECRET")
    ADMIN_SECRET: str = os.environ.get("ADMIN_SECRET", "")
    PORT: int = int(os.environ.get("PORT", "8000"))
    HOST: str = os.environ.get("HOST", "0.0.0.0")
    FB_CONTENT_TRACKER_URL: str = os.environ.get(
        "FB_CONTENT_TRACKER_URL", "https://fb-content-tracker-production.up.railway.app"
    )
    FB_CONTENT_TRACKER_INTERNAL_TOKEN: str = os.environ.get("FB_CONTENT_TRACKER_INTERNAL_TOKEN", "")


settings = Settings()

# ──────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────
MODELS = [
    "Sherry Hicks",
    "Emily Bryant",
    "Rose Kenzie",
    "Noura Amaar",
    "Sherry Hicks Shell",
    "Margaret Asian",
    "Abby Parker",
    "Yuki Chen",
]

# Content types available per model
# Sherry Hicks Shell only gets Full AI Content
MODEL_CONTENT_RESTRICTIONS = {
    "Sherry Hicks Shell": ["Full AI Content"],
}

# ──────────────────────────────────────────────
# Slot Distributor — AI Models Reels
# ──────────────────────────────────────────────
AI_MODELS_REELS_CHAT_ID = -1003965304219
SLOTS_PER_CREATOR = 6  # accounts per creator

# Creator → Topic-ID in "AI Models Reels" Forum-Supergroup (fallback / models ohne Nische)
SLOT_CREATORS = {
    "Margaret Asian": 4,
    "Abby Parker": 3,
    "Yuki Chen": 2,
    "Sherry Hicks": 236,
}

# (Model, Nische) → Topic-ID — überschreibt SLOT_CREATORS wenn Nische bekannt
NICHE_TOPICS = {
    ("Margaret Asian", "Snapchat Based Reels"): 270,
    ("Margaret Asian", "School Girl Reels"):    4,
    ("Margaret Asian", "Shell Reels"):          271,
    ("Margaret Asian", "Nurse Reels"):          272,
    ("Margaret Asian", "Sports (Football Reels)"): 1158,
    ("Margaret Asian", "Interview Reels"):      1159,
    ("Yuki Chen",      "Snapchat Based Reels"): 269,
    ("Yuki Chen",      "School Girl Reels"):    2,
    ("Yuki Chen",      "Sports (Football Reels)"): 1160,
    ("Yuki Chen",      "Interview Reels"):      1161,
}

# ──────────────────────────────────────────────
# Telegram Routing Rules
# Each VA receives a notification based on model + content type
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# Video distribution targets (separate from notifications)
# Videos are distributed round-robin across these topics
# ──────────────────────────────────────────────
VIDEO_DISTRIBUTION_TARGETS = [
    {"chat_id": "-1003604359153", "thread_id": 371},
    {"chat_id": "-1003604359153", "thread_id": 372},
]

# ──────────────────────────────────────────────
# VA Telegram user IDs — used to DM reject reasons directly
# IDs captured via getUpdates after each VA sent /start to @BehaveAgencyBot
# ──────────────────────────────────────────────
VA_TELEGRAM_IDS = {
    "Ken": 5900218841,
    "James": 5512507496,
}

# ──────────────────────────────────────────────
# Strike system — daily deadlines
# ──────────────────────────────────────────────
BJARNE_TELEGRAM_ID = 8013986821  # = VA 5 chat_id above, same account (@bjarnefuchs)
JEREMI_TELEGRAM_ID = 8371406259  # = VA 6 chat_id above, same account (@jeremi_snd) — only this ID may /removestrike

# Bjarne is currently the only strike subject — every check below (Ken/James/Trends groups,
# the approval flow) watches whether BJARNE did his part, not Ken/James/Sherry (they're just
# group members/owners whose groups Bjarne needs to act in; they get no strikes, no DMs).
PERSON_TELEGRAM_IDS = {"bjarne": BJARNE_TELEGRAM_ID}
PERSON_DISPLAY_NAMES = {"bjarne": "Bjarne"}

# ALL strikes post into this one group — no per-person groups.
STRIKE_GROUP_CHAT_ID = -5014530893  # "🚨 Bjarne Fuchs Strikes Tracking"

DEADLINE_BUFFER_MINUTES = 15  # grace period added to every deadline below before a strike fires
STRIKE_MONTHLY_DISPLAY_MAX = 3  # shown as "X/3" in messages — display only, no automatic consequence at 3

# Bjarne: Full-AI-Content-Videos in the APPROVAL BOT group must be approved/rejected by this hour
APPROVAL_DEADLINE_HOUR = 13

# Notion "Tasks Database Agency" — used to pull the real deadline for non-daily recurring
# tasks (e.g. Sherry's Reels list, every 3 days) instead of hardcoding a cadence in code.
NOTION_TOKEN: str = os.environ.get("NOTION_TOKEN", "")
NOTION_TASKS_DB_ID = "1ad54236-70f0-80dc-9e10-ca3339419e09"

# Activity-based deadlines: Bjarne must send >=1 message into chat_id by the deadline (+buffer),
# checked via activity_log.py (bot is admin in each monitored group so it sees every message
# there, and maybe_record_activity() only records messages actually sent by Bjarne — other
# members of these groups, e.g. Ken/James/Sherry, are ignored for this purpose).
# `window_hours` is how far back "did he send something" looks — 24h for daily tasks, wider
# for tasks that don't recur every day (Sherry's list is every 3 days).
ACTIVITY_STRIKE_TASKS = [
    {
        "chat_id": -1003746370573, "label": "AI Reels Gen (Ken)",
        "deadline_hour": 23, "deadline_minute": 59, "window_hours": 24,
    },
    {
        "chat_id": -1004439596787, "label": "AI Reels Gen (James)",
        "deadline_hour": 23, "deadline_minute": 59, "window_hours": 24,
    },
]

# Notion-gated activity task: only checked on days Notion actually lists a due task for
# this title+assignee — deadline time is read live from that Notion entry, not hardcoded.
SHERRY_LIST_NOTION_TASK_TITLE = "Instagram Reels Liste Sherry"
# The Notion task itself is assigned to "Sherry" (used only to find the due-day + deadline
# time) — but the actual strike check still watches BJARNE's activity in the chat, same as
# every other check. Notion's assignee here doesn't change who the strike system holds
# accountable, it's just which Notion record to read the schedule from.
SHERRY_LIST_NOTION_ASSIGNED_TO = "Sherry"
SHERRY_LIST_CHAT_ID = -1002303192503  # same "Instagram Reels Trends - Sherry" group
SHERRY_LIST_WINDOW_HOURS = 72  # 3-day cadence

TELEGRAM_ROUTING = [
    {
        "name": "VA 6",
        "chat_id": "8371406259",
        "rule": "always",
    },
    {
        "name": "VA 5",
        "chat_id": "8013986821",
        "rule": "custom",
        "content_types_all_models": [],
        "content_types_margaret_only": ["Full AI Content"],
    },
]
