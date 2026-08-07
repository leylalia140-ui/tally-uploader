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

PERSON_TELEGRAM_IDS = {
    "bjarne": BJARNE_TELEGRAM_ID,
    "ken": VA_TELEGRAM_IDS["Ken"],
    "james": VA_TELEGRAM_IDS["James"],
}
PERSON_DISPLAY_NAMES = {"bjarne": "Bjarne", "ken": "Ken", "james": "James"}

# ALL strikes (Bjarne/Ken/James, any reason) post into this one group — no per-person groups.
STRIKE_GROUP_CHAT_ID = -5014530893  # "🚨 Bjarne Fuchs Strikes Tracking"

DEADLINE_BUFFER_MINUTES = 15  # grace period added to every deadline below before a strike fires
STRIKE_MONTHLY_DISPLAY_MAX = 3  # shown as "X/3" in messages — display only, no automatic consequence at 3

# Bjarne: Full-AI-Content-Videos in the APPROVAL BOT group must be approved/rejected by this hour
APPROVAL_DEADLINE_HOUR = 13

# Activity-based deadlines: the person must send >=1 message into chat_id by deadline_hour (+buffer),
# checked via activity_log.py (bot is admin in each of these 3 groups so it sees every message there).
ACTIVITY_STRIKE_TASKS = [
    {"person": "bjarne", "chat_id": -1002303192503, "deadline_hour": 23, "deadline_minute": 0,  "label": "Full AI Trends Research"},
    {"person": "ken",    "chat_id": -1003746370573, "deadline_hour": 23, "deadline_minute": 59, "label": "AI Reels Gen (Ken)"},
    {"person": "james",  "chat_id": -1004439596787, "deadline_hour": 23, "deadline_minute": 59, "label": "AI Reels Gen (James)"},
]

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
