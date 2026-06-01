import os


class Settings:
    GOOGLE_DRIVE_ROOT_FOLDER_ID: str = os.environ.get(
        "GOOGLE_DRIVE_ROOT_FOLDER_ID", "1ofu3b6xK4Hdnk0n_vGD-y9cQNSg7lY2U"
    )
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TG_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    TALLY_SIGNING_SECRET: str | None = os.environ.get("TALLY_SIGNING_SECRET")
    PORT: int = int(os.environ.get("PORT", "8000"))
    HOST: str = os.environ.get("HOST", "0.0.0.0")


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

# Creator → Topic-ID in "AI Models Reels" Forum-Supergroup
SLOT_CREATORS = {
    "Margaret Asian": 4,
    "Abby Parker": 3,
    "Yuki Chen": 2,
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
