import os


class Settings:
    GOOGLE_DRIVE_ROOT_FOLDER_ID: str = os.environ.get(
        "GOOGLE_DRIVE_ROOT_FOLDER_ID", "1ofu3b6xK4Hdnk0n_vGD-y9cQNSg7lY2U"
    )
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
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
]

# Content types available per model
# Sherry Hicks Shell only gets Full AI Content
MODEL_CONTENT_RESTRICTIONS = {
    "Sherry Hicks Shell": ["Full AI Content"],
}

# ──────────────────────────────────────────────
# Telegram Routing Rules
# Each VA receives a notification based on model + content type
# ──────────────────────────────────────────────
TELEGRAM_ROUTING = [
    {
        "name": "VA 6",
        "chat_id": "8371406259",
        "rule": "always",          # receives every upload
    },
    {
        "name": "VA 2",
        "chat_id": "1289565858",
        # Customs, Half AI Content → all models
        # Feed Content, PPV Content → only Sherry Hicks
        "rule": "custom",
        "content_types_all_models": ["Customs", "Half AI Content"],
        "content_types_sherry_only": ["Feed Content", "PPV Content"],
    },
    {
        "name": "VA 3",
        "chat_id": "7931507598",
        # Instagram Reels → only Sherry Hicks
        # Full AI Content → all models
        "rule": "custom",
        "content_types_all_models": ["Full AI Content"],
        "content_types_sherry_only": ["Instagram Reels"],
    },
    {
        "name": "VA 4",
        "chat_id": "5905688359",
        # Instagram Reels → only Sherry Hicks
        "rule": "custom",
        "content_types_all_models": [],
        "content_types_sherry_only": ["Instagram Reels"],
    },
    {
        "name": "VA 5",
        "chat_id": "8013986821",
        # Full AI Content + Half AI Content → all models
        "rule": "custom",
        "content_types_all_models": ["Full AI Content", "Half AI Content"],
        "content_types_sherry_only": [],
    },
]
