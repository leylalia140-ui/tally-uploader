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
        "rule": "always",
    },
    {
        "name": "VA 5",
        "chat_id": "8013986821",
        "rule": "always",
    },
]
