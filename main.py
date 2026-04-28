import io
import hashlib
import hmac
import logging
from datetime import datetime

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Header
from typing import Optional

from config import settings
from drive import GoogleDriveClient
import telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Tally → Drive → Telegram")


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def format_date(dt: datetime) -> str:
    """Return e.g. '26th March 2026' (never '26-03-2026')."""
    day = dt.day
    suffix = (
        "th"
        if 11 <= day <= 13
        else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    )
    return f"{day}{suffix} {dt.strftime('%B %Y')}"


def verify_tally_signature(body: bytes, signature: str) -> bool:
    """Optional: verify the webhook came from Tally."""
    if not settings.TALLY_SIGNING_SECRET:
        return True  # skip verification if no secret configured
    expected = hmac.new(
        settings.TALLY_SIGNING_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _resolve_dropdown(field: dict) -> str | None:
    """
    Tally DROPDOWN values are option IDs (UUIDs).
    The actual label is in field['options'][id]['text'].
    """
    value = field.get("value")
    options = {o["id"]: o["text"] for o in field.get("options", [])}
    if isinstance(value, list):
        labels = [options.get(v, v) for v in value if v]
        return labels[0] if labels else None
    return options.get(value, value)


def extract_uploads(payload: dict) -> list[dict]:
    """
    Parse the Tally webhook and return a list of upload dicts, one per file:
      [{ "model": "Sherry Hicks", "content_type": "Instagram Reels",
         "file_url": "...", "file_name": "...", "mime_type": "..." }, ...]
    """
    fields_raw = payload.get("data", {}).get("fields", [])

    model_name = ""
    content_type = ""
    file_objects = []

    for f in fields_raw:
        label = f.get("label", "")
        ftype = f.get("type", "")

        if label == "Was ist dein Creator Name":
            model_name = (f.get("value") or "").strip()

        elif label == "Was für eine Art von Content lädst du hoch?":
            # This is the Drive folder content type (Instagram Reels, PPV etc.)
            content_type = _resolve_dropdown(f) or ""

        elif ftype == "FILE_UPLOAD":
            files = f.get("value")
            if files and isinstance(files, list):
                file_objects.extend([fo for fo in files if fo])

    logger.info(f"Parsed: model='{model_name}' content_type='{content_type}' files={len(file_objects)}")

    return [
        {
            "model": model_name,
            "content_type": content_type,
            "file_url": fo.get("url"),
            "file_name": fo.get("name", "upload.mp4"),
            "mime_type": fo.get("mimeType", "video/mp4"),
        }
        for fo in file_objects
    ]


# ──────────────────────────────────────────────────────────────
# Background task
# ──────────────────────────────────────────────────────────────

async def process_all_uploads(uploads: list[dict]) -> None:
    if not uploads:
        return

    model_name = uploads[0].get("model", "").strip()
    content_type = uploads[0].get("content_type", "").strip()
    date_str = format_date(datetime.now())
    folder_path = ["Models", model_name, content_type, "not edited", date_str]

    drive = GoogleDriveClient()
    folder_id = drive.resolve_folder_path(folder_path)

    for upload in uploads:
        file_url = upload.get("file_url")
        file_name = upload.get("file_name", "video.mp4")
        mime_type = upload.get("mime_type", "video/mp4")

        if not file_url:
            logger.error(f"Missing file_url: {upload}")
            continue

        logger.info(f"Processing: {model_name} / {content_type} / {date_str} — {file_name}")
        logger.info(f"Downloading: {file_url}")

        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            async with client.stream("GET", file_url) as response:
                response.raise_for_status()
                buffer = io.BytesIO()
                async for chunk in response.aiter_bytes(chunk_size=10 * 1024 * 1024):
                    buffer.write(chunk)
        buffer.seek(0)
        logger.info(f"Downloaded {buffer.getbuffer().nbytes / 1024 / 1024:.1f} MB")

        drive.upload_file(
            file_name=file_name,
            file_stream=buffer,
            folder_id=folder_id,
            mime_type=mime_type,
        )
        logger.info(f"Done: {file_name}")

    folder_link = drive.make_folder_public(folder_id)
    logger.info(f"Folder link: {folder_link}")
    await telegram_bot.send_notifications(
        model_name=model_name,
        content_type=content_type,
        date_str=date_str,
        drive_links=[folder_link],
    )


# ──────────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────────

@app.get("/webhook/tally")
async def tally_webhook_verify():
    return {"status": "ok"}


@app.post("/webhook/tally")
async def tally_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    tally_signature: Optional[str] = Header(None, alias="tally-signature"),
):
    body = await request.body()

    if tally_signature and not verify_tally_signature(body, tally_signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()

    # Only handle form responses
    if payload.get("eventType") != "FORM_RESPONSE":
        return {"status": "ignored", "reason": "not a form response"}

    uploads = extract_uploads(payload)
    if not uploads:
        await telegram_bot.send_error_notification("no files found")
        return {"status": "error", "reason": "no files"}

    background_tasks.add_task(process_all_uploads, uploads)

    # Return 200 immediately so Tally doesn't retry
    return {"status": "accepted"}


@app.get("/health")
async def health():
    return {"status": "ok"}
