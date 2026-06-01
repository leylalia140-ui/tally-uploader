import asyncio
import hashlib
import hmac
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Header
from typing import Optional

from config import settings, SLOT_CREATORS
from drive import GoogleDriveClient
import telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)

# Limit concurrent processing to 1 — prevents OOM when multiple webhooks arrive simultaneously
_process_semaphore = asyncio.Semaphore(1)

app = FastAPI(title="Tally → Drive → Telegram")


@app.on_event("startup")
async def startup_event():
    token = settings.TELEGRAM_BOT_TOKEN
    logger.info(f"TELEGRAM_BOT_TOKEN: {'SET (' + token[:10] + '...)' if token else 'EMPTY!'}")
    webhook_url = "https://tally-uploader-production.up.railway.app/bot"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{token}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["callback_query"]},
        )
        logger.info(f"Webhook set: {r.json().get('description', r.text)}")


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
    data = payload.get("data", {})
    fields_raw = data.get("fields", [])
    form_id = data.get("formId", "")

    model_name = ""
    content_type = ""
    file_objects = []

    for f in fields_raw:
        label = f.get("label", "")
        ftype = f.get("type", "")

        if label in ("Was ist dein Creator Name", "For Which Model are you uploading Content?"):
            model_name = _resolve_dropdown(f) or (f.get("value") or "").strip()

        elif label in ("Was für eine Art von Content lädst du hoch?", "What kind of Content?"):
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
            "form_id": form_id,
            "file_url": fo.get("url"),
            "file_name": fo.get("name", "upload.mp4"),
            "mime_type": fo.get("mimeType", "video/mp4"),
        }
        for fo in file_objects
    ]


# ──────────────────────────────────────────────────────────────
# Helpers: file type detection
# ──────────────────────────────────────────────────────────────

_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}

def is_image(file_name: str, mime_type: str) -> bool:
    ext = os.path.splitext(file_name)[1].lower()
    return mime_type.lower() in _IMAGE_MIME_TYPES or ext in _IMAGE_EXTENSIONS


# ──────────────────────────────────────────────────────────────
# Video conversion
# ──────────────────────────────────────────────────────────────

def convert_to_h264(in_path: str, original_name: str) -> tuple[str, str]:
    """
    Convert video at in_path to H.264/MP4 on disk. Deletes in_path when done.
    Returns (out_path, new_name). Caller must delete out_path when finished.
    """
    out_path = in_path + "_h264.mp4"
    new_name = os.path.splitext(original_name)[0] + ".mp4"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", in_path,
                "-vf", "scale=1074:1920,setsar=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-pix_fmt", "yuv420p",
                "-threads", "1",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                out_path,
            ],
            capture_output=True,
            timeout=600,
        )
        if result.returncode != 0:
            logger.error(f"ffmpeg FAILED (code {result.returncode}): {result.stderr.decode()[-1000:]}")
            os.rename(in_path, out_path)  # fallback: use original as-is
            return out_path, new_name

        size_mb = os.path.getsize(out_path) / 1024 / 1024
        logger.info(f"Converted to H.264: {new_name} ({size_mb:.1f} MB)")
        return out_path, new_name
    except Exception as e:
        logger.error(f"ffmpeg exception: {e}")
        os.rename(in_path, out_path)
        return out_path, new_name
    finally:
        if os.path.exists(in_path):
            os.unlink(in_path)


# ──────────────────────────────────────────────────────────────
# Background task
# ──────────────────────────────────────────────────────────────

async def process_all_uploads(uploads: list[dict]) -> None:
    if not uploads:
        return

    async with _process_semaphore:
        await _do_process_all_uploads(uploads)


async def _do_process_all_uploads(uploads: list[dict]) -> None:
    model_name = uploads[0].get("model", "").strip()
    content_type = uploads[0].get("content_type", "").strip()
    date_str = format_date(datetime.now(BERLIN))
    form_id = uploads[0].get("form_id", "")

    try:
        content_lower = content_type.lower()
        if form_id == "wAq9ql":
            folder_subfolder = "edited"
        elif model_name == "Sherry Hicks":
            folder_subfolder = "not edited"
        elif model_name == "Margaret Asian" and form_id == "mVMbpj" and any(k in content_lower for k in ("ppv", "feed")):
            folder_subfolder = "not edited"
        else:
            folder_subfolder = "edited"
        folder_path = ["Models", model_name, content_type, folder_subfolder, date_str]

        drive = GoogleDriveClient()
        folder_id = drive.resolve_folder_path(folder_path)

        # Videos for Margaret Asian approval (file paths, not bytes)
        approval_videos = []

        for upload in uploads:
            file_url = upload.get("file_url")
            file_name = upload.get("file_name", "video.mp4")
            mime_type = upload.get("mime_type", "video/mp4")

            if not file_url:
                logger.error(f"Missing file_url: {upload}")
                continue

            logger.info(f"Processing: {model_name} / {content_type} / {date_str} — {file_name}")
            logger.info(f"Downloading: {file_url}")

            # Stream directly to a temp file — never load the full video into RAM
            ext_in = os.path.splitext(file_name)[1] or ".mp4"
            with tempfile.NamedTemporaryFile(suffix=ext_in, delete=False) as tmp_f:
                tmp_path = tmp_f.name
                size = 0
                async with httpx.AsyncClient(timeout=900, follow_redirects=True) as client:
                    async with client.stream("GET", file_url) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes(chunk_size=10 * 1024 * 1024):
                            tmp_f.write(chunk)
                            size += len(chunk)
            logger.info(f"Downloaded {size / 1024 / 1024:.1f} MB → {tmp_path}")

            if content_type == "Full AI Content" and not is_image(file_name, mime_type):
                # convert_to_h264 deletes tmp_path itself, returns new out_path
                video_path, file_name = convert_to_h264(tmp_path, file_name)
                mime_type = "video/mp4"
            else:
                video_path = tmp_path

            try:
                with open(video_path, "rb") as f:
                    drive.upload_file(
                        file_name=file_name,
                        file_stream=f,
                        folder_id=folder_id,
                        mime_type=mime_type,
                    )
                logger.info(f"Uploaded to Drive: {file_name}")
            except Exception:
                os.unlink(video_path)
                raise

            if model_name in SLOT_CREATORS:
                approval_videos.append({"file_name": file_name, "path": video_path})
            else:
                os.unlink(video_path)

        folder_link = drive.make_folder_public(folder_id)
        logger.info(f"Folder link: {folder_link}")

        if model_name in SLOT_CREATORS and approval_videos:
            await telegram_bot.send_for_approval(approval_videos, model_name, content_type)
            # telegram_bot.py owns the files now and cleans them up after approve/reject

        await telegram_bot.send_notifications(
            model_name=model_name,
            content_type=content_type,
            date_str=date_str,
            drive_links=[folder_link],
        )

    except Exception as e:
        logger.error(f"FATAL error processing upload for {model_name}: {e}", exc_info=True)
        await telegram_bot.send_error_notification(
            f"Upload fehlgeschlagen für {model_name} / {content_type}: {type(e).__name__}: {e}"
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


@app.post("/bot")
async def telegram_callback(request: Request):
    data = await request.json()
    if "callback_query" in data:
        cq = data["callback_query"]
        await telegram_bot.handle_callback(cq)
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": cq["id"]},
            )
    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}
