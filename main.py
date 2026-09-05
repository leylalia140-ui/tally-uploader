import asyncio
import hashlib
import hmac
import json
import logging
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

import httpx
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Header, Body
from typing import Optional

from config import (
    settings, SLOT_CREATORS, DEADLINE_BUFFER_MINUTES,
    ACTIVITY_STRIKE_TASKS, SHERRY_LIST_NOTION_TASK_TITLE, SHERRY_LIST_NOTION_ASSIGNED_TO,
    SHERRY_LIST_CHAT_ID, SHERRY_LIST_WINDOW_HOURS,
)
import notion_tasks
import approval_log
from drive import GoogleDriveClient
from dateutil_local import format_date
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
            json={"url": webhook_url, "allowed_updates": ["callback_query", "message"]},
        )
        logger.info(f"Webhook set: {r.json().get('description', r.text)}")
    asyncio.create_task(_periodic_retry_loop())
    asyncio.create_task(_daily_strike_check_loop())
    asyncio.create_task(_monthly_report_loop())


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

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
    niche = ""
    va_name = ""
    file_objects = []

    for f in fields_raw:
        label = f.get("label", "")
        ftype = f.get("type", "")

        if label in ("Was ist dein Creator Name", "For Which Model are you uploading Content?"):
            model_name = _resolve_dropdown(f) or (f.get("value") or "").strip()

        elif label in ("Was für eine Art von Content lädst du hoch?", "What kind of Content?"):
            content_type = _resolve_dropdown(f) or ""

        elif label in ("Margaret Niche", "Yuki Niche", "Abby Niche"):
            val = _resolve_dropdown(f) or ""
            if val:
                niche = val

        elif label in ("Wer lädst du hoch?", "Who is uploading this?"):
            va_name = _resolve_dropdown(f) or ""

        elif ftype == "FILE_UPLOAD":
            files = f.get("value")
            if files and isinstance(files, list):
                file_objects.extend([fo for fo in files if fo])

    logger.info(f"Parsed: model='{model_name}' content_type='{content_type}' niche='{niche}' files={len(file_objects)}")

    return [
        {
            "model": model_name,
            "content_type": content_type,
            "niche": niche,
            "va_name": va_name,
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


INSTAGRAM_FEED_PICTURES_CONTENT_TYPE = "Instagram FEED PICTURES"


async def _push_image_to_content_tracker(file_path: str, file_name: str, mime_type: str, creator: str) -> None:
    if not settings.FB_CONTENT_TRACKER_INTERNAL_TOKEN:
        logger.warning("FB_CONTENT_TRACKER_INTERNAL_TOKEN not set, skipping content tracker push")
        return
    try:
        with open(file_path, "rb") as f:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{settings.FB_CONTENT_TRACKER_URL}/api/internal/upload-image",
                    headers={"X-Internal-Token": settings.FB_CONTENT_TRACKER_INTERNAL_TOKEN},
                    files={"files": (file_name, f, mime_type)},
                    data={"creator": creator, "niche": INSTAGRAM_FEED_PICTURES_CONTENT_TYPE},
                )
                resp.raise_for_status()
        logger.info(f"Pushed to FB Content Tracker: {file_name} ({creator})")
    except Exception as e:
        logger.error(f"Failed to push {file_name} to FB Content Tracker: {e}")


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


FAILED_DIR = "/data/failed_uploads"


def _save_failed_uploads(uploads: list[dict]) -> str:
    os.makedirs(FAILED_DIR, exist_ok=True)
    upload_id = f"{datetime.now(BERLIN):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
    with open(os.path.join(FAILED_DIR, f"{upload_id}.json"), "w") as f:
        json.dump(uploads, f)
    return upload_id


def _list_failed_uploads() -> list[str]:
    if not os.path.isdir(FAILED_DIR):
        return []
    return sorted(f for f in os.listdir(FAILED_DIR) if f.endswith(".json"))


async def _retry_failed_upload(filename: str) -> Optional[str]:
    """Retries one saved failed upload. Returns an error string on failure, None on success."""
    path = os.path.join(FAILED_DIR, filename)
    with open(path) as f:
        uploads = json.load(f)
    try:
        await _process_uploads_core(uploads)
    except Exception as e:
        # Files that made it through this retry attempt (Drive + approval-send,
        # where applicable) before it failed again must not be re-saved for the
        # next retry — same reasoning as in _do_process_all_uploads.
        to_retry = getattr(e, "remaining_uploads", uploads)
        if not to_retry:
            os.remove(path)
        elif to_retry != uploads:
            with open(path, "w") as f:
                json.dump(to_retry, f)
        return f"{type(e).__name__}: {e}"
    os.remove(path)
    return None


async def _retry_all_failed_uploads() -> None:
    for filename in _list_failed_uploads():
        error = await _retry_failed_upload(filename)
        if error:
            logger.warning(f"Retry still failing for {filename}: {error}")
        else:
            logger.info(f"Retry succeeded for {filename}")


async def _periodic_retry_loop() -> None:
    while True:
        await asyncio.sleep(15 * 60)
        try:
            if _list_failed_uploads():
                await _retry_all_failed_uploads()
        except Exception as e:
            logger.error(f"Periodic retry loop error: {e}", exc_info=True)


_STRIKE_CHECKS = [
    {
        "key": f"activity_{t['chat_id']}_{t['deadline_hour']}{t['deadline_minute']}",
        "hour": t["deadline_hour"],
        "minute": t["deadline_minute"],
        "fn": (lambda t=t: telegram_bot.check_activity_deadline("bjarne", t["chat_id"], t["label"], t["window_hours"])),
    }
    for t in ACTIVITY_STRIKE_TASKS
]
_STRIKE_CHECKS.append({
    "key": "activity_sherry_list",
    # No static hour/minute — resolve_time asks Notion whether `anchor` is even a due
    # day at all (Sherry's list is every 3 days, not daily), and if so what time it's
    # due. The Notion record is assigned to Sherry (used only to find the schedule) —
    # the actual strike check still watches Bjarne's own activity in the chat, same
    # as every other check.
    "resolve_time": (lambda anchor: notion_tasks.get_deadline_for_date(
        SHERRY_LIST_NOTION_TASK_TITLE, SHERRY_LIST_NOTION_ASSIGNED_TO, anchor
    )),
    "fn": (lambda: telegram_bot.check_activity_deadline(
        "bjarne", SHERRY_LIST_CHAT_ID, "Sherry Reels Liste", SHERRY_LIST_WINDOW_HOURS
    )),
})

# Set of "{check_key}:{anchor_date}" strings already fired (or pre-launch, never to fire).
# Must be keyed per (check, anchor) pair, not just per check — two anchors (yesterday/today)
# can be simultaneously due in the same tick, and sharing one "last checked" slot between
# them made each overwrite the other's fired-marker, causing infinite re-firing every 5min.
_fired_checks: set[str] = set()


async def _daily_strike_check_loop() -> None:
    """Fires each strike check once per day, the first time the loop observes
    Berlin time at/after that check's deadline (hour:minute) + DEADLINE_BUFFER_MINUTES.

    Each tick re-evaluates both today's and yesterday's deadline for every check
    (not just today's) — necessary because a deadline like 23:59 + 15min buffer
    lands at 00:14 the *next* calendar day, so "today" at fire time is already
    one day past the deadline it's evaluating. Tracking each (check, anchor-day)
    pair independently in `_fired_checks` keeps each day's deadline checked
    exactly once regardless of which day it happens to fire on.

    A deadline occurrence is skipped only if it had *already* passed (deadline +
    buffer < launch moment) at the exact moment the feature first started —
    not the whole calendar day. So deploying at e.g. 19:51 skips today's 13:00
    check (already silently missed before the feature existed) but still lets
    today's still-upcoming 23:00/23:59 checks fire normally tonight."""
    launch_at = telegram_bot.strikes.get_or_create_launch_at()
    while True:
        await asyncio.sleep(5 * 60)
        try:
            # Not anchor/fixed-time based like the checks below — each unresolved
            # video has its own effective deadline (see check_daily_approval_deadline's
            # docstring), so this just gets re-evaluated every tick.
            await telegram_bot.check_daily_approval_deadline()

            now = datetime.now(BERLIN)
            for check in _STRIKE_CHECKS:
                for days_back in (1, 0):
                    anchor = (now - timedelta(days=days_back)).date()
                    anchor_str = anchor.strftime("%Y-%m-%d")
                    fire_key = f"{check['key']}:{anchor_str}"
                    if fire_key in _fired_checks:
                        continue
                    if "resolve_time" in check:
                        # Notion-gated check: ask whether `anchor` is even a due day at all,
                        # and if so what time it's due — re-asked every tick until resolved,
                        # since it can't be known ahead of time like a static hour/minute.
                        resolved = await check["resolve_time"](anchor)
                        if resolved is None:
                            _fired_checks.add(fire_key)  # not a due day — never check this anchor again
                            continue
                        hour, minute = resolved
                    else:
                        hour, minute = check["hour"], check["minute"]
                    deadline_dt = datetime(
                        anchor.year, anchor.month, anchor.day,
                        hour, minute, tzinfo=BERLIN,
                    ) + timedelta(minutes=DEADLINE_BUFFER_MINUTES)
                    if deadline_dt < launch_at:
                        _fired_checks.add(fire_key)  # already over before the feature existed — never fire
                        continue
                    if now >= deadline_dt:
                        _fired_checks.add(fire_key)
                        await check["fn"]()
        except Exception as e:
            logger.error(f"Daily strike check loop error: {e}", exc_info=True)


async def _monthly_report_loop() -> None:
    """Sends telegram_bot.send_monthly_report() (DM to Jeremi, no group) once on
    the 1st of each month, around 09:00 Berlin. Persists the last-reported month
    to disk so a restart on the 1st doesn't send it twice."""
    while True:
        await asyncio.sleep(5 * 60)
        try:
            now = datetime.now(BERLIN)
            if now.day != 1 or now.hour < 9:
                continue
            year_month = now.strftime("%Y-%m")
            if telegram_bot.strikes.get_last_report_month() == year_month:
                continue
            telegram_bot.strikes.set_last_report_month(year_month)
            await telegram_bot.send_monthly_report()
        except Exception as e:
            logger.error(f"Monthly report loop error: {e}", exc_info=True)


async def _do_process_all_uploads(uploads: list[dict]) -> None:
    model_name = uploads[0].get("model", "").strip()
    content_type = uploads[0].get("content_type", "").strip()
    try:
        await _process_uploads_core(uploads)
    except Exception as e:
        logger.error(f"FATAL error processing upload for {model_name}: {e}", exc_info=True)
        # Uploads that already reached Drive (and, if approval-eligible, were
        # already sent to the approval group) before the failure are excluded
        # from the retry set — retrying the full original list would re-upload
        # and re-send duplicate approval cards for content already out for
        # review. See the try/except around the per-upload loop below.
        to_retry = getattr(e, "remaining_uploads", uploads)
        if not to_retry:
            # Every file already reached Drive (and approval-send, where
            # applicable) — only the trailing folder-link/notification step
            # failed. Nothing left to retry; just alert.
            await telegram_bot.send_error_notification(
                f"Nacharbeiten fehlgeschlagen für {model_name} / {content_type} (alle Dateien bereits "
                f"hochgeladen/gesendet): {type(e).__name__}: {e}"
            )
            return
        upload_id = _save_failed_uploads(to_retry)
        await telegram_bot.send_error_notification(
            f"Upload fehlgeschlagen für {model_name} / {content_type}: {type(e).__name__}: {e}\n"
            f"{len(to_retry)}/{len(uploads)} Datei(en) noch offen — gespeichert als {upload_id}, "
            f"wird automatisch erneut versucht (alle 15 Min) oder per POST /admin/retry_failed."
        )


async def _process_uploads_core(uploads: list[dict]) -> None:
    model_name = uploads[0].get("model", "").strip()
    content_type = uploads[0].get("content_type", "").strip()
    niche = uploads[0].get("niche", "").strip()
    va_name = uploads[0].get("va_name", "").strip()
    date_str = format_date(datetime.now(BERLIN))
    form_id = uploads[0].get("form_id", "")

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

    # GoogleDriveClient is fully synchronous/blocking — every call below runs via
    # asyncio.to_thread so a slow/hung Drive API response can never freeze the
    # whole process (it already did once: an admin script blocked everything,
    # including Bjarne's approve/reject webhooks, for several minutes).
    drive = await asyncio.to_thread(GoogleDriveClient)
    folder_id = await asyncio.to_thread(drive.resolve_folder_path, folder_path)
    type_folder_ids: dict[str, str] = {}

    # Tracks which of the original uploads still need a retry on failure.
    # Videos are now sent for approval as soon as each one is done (see
    # below), so a failure partway through a multi-file submission must
    # NOT re-queue files that already went out for approval — that would
    # re-upload and re-send duplicate approval cards for content a human
    # may already be reviewing. Only files not yet fully processed at the
    # point of failure are kept for retry.
    remaining_uploads = list(uploads)

    for upload in uploads:
        try:
            file_url = upload.get("file_url")
            file_name = upload.get("file_name", "video.mp4")
            mime_type = upload.get("mime_type", "video/mp4")

            if not file_url:
                logger.error(f"Missing file_url: {upload}")
                remaining_uploads.remove(upload)
                continue

            type_folder_name = "Images" if is_image(file_name, mime_type) else "Videos"
            if type_folder_name not in type_folder_ids:
                type_folder_ids[type_folder_name] = await asyncio.to_thread(
                    drive.get_or_create_folder, type_folder_name, folder_id
                )
            upload_folder_id = type_folder_ids[type_folder_name]

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
                # convert_to_h264 deletes tmp_path itself, returns new out_path.
                # Runs in a thread — ffmpeg is CPU-bound and would otherwise freeze
                # the whole event loop (every other webhook/Telegram callback) for
                # the duration of the conversion.
                video_path, file_name = await asyncio.to_thread(convert_to_h264, tmp_path, file_name)
                mime_type = "video/mp4"
            else:
                video_path = tmp_path

            try:
                with open(video_path, "rb") as f:
                    await asyncio.to_thread(
                        drive.upload_file,
                        file_name=file_name,
                        file_stream=f,
                        folder_id=upload_folder_id,
                        mime_type=mime_type,
                    )
                logger.info(f"Uploaded to Drive: {file_name}")
            except Exception:
                os.unlink(video_path)
                raise

            if content_type == INSTAGRAM_FEED_PICTURES_CONTENT_TYPE and is_image(file_name, mime_type):
                await _push_image_to_content_tracker(video_path, file_name, mime_type, model_name)

            if not is_image(file_name, mime_type) and (
                (model_name in SLOT_CREATORS and content_type == "Full AI Content")
                or (
                    model_name in ("Margaret Asian", "Abby Parker", "Yuki Chen")
                    and content_type == "Instagram Reels"
                )
            ):
                # Sent immediately per video instead of batched after the whole
                # submission finishes — one slow/large video no longer delays
                # every other video in the same Tally submission from reaching
                # the approval group.
                await telegram_bot.send_for_approval(
                    [{"file_name": file_name, "path": video_path}], model_name, content_type, niche, va_name
                )
                # telegram_bot.py owns the file now and cleans it up after approve/reject
            else:
                os.unlink(video_path)

            remaining_uploads.remove(upload)
        except Exception as e:
            e.remaining_uploads = remaining_uploads
            raise

    # Every upload is fully done (Drive + approval-send, where applicable) at
    # this point — a failure below must not re-trigger the whole batch.
    try:
        folder_link = await asyncio.to_thread(drive.make_folder_public, folder_id)
        logger.info(f"Folder link: {folder_link}")

        await telegram_bot.send_notifications(
            model_name=model_name,
            content_type=content_type,
            date_str=date_str,
            drive_links=[folder_link],
        )
    except Exception as e:
        e.remaining_uploads = []
        raise


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
    elif "message" in data:
        message = data["message"]
        telegram_bot.maybe_record_activity(message)
        if "reply_to_message" in message:
            await telegram_bot.handle_reason_reply(message)
        elif (message.get("text") or "").startswith("/"):
            await telegram_bot.handle_command(message)
    return {"ok": True}


@app.post("/admin/push_unresolved")
async def admin_push_unresolved(
    since: Optional[str] = None,
    until: Optional[str] = None,
    x_admin_secret: Optional[str] = Header(None),
):
    """Approve every video still stuck in approval_log (self-healing from Drive
    as needed) — for clearing an orphaned backlog without Bjarne clicking each one.
    Optional ?since=YYYY-MM-DD&until=YYYY-MM-DD (Berlin-local, both inclusive)
    scopes this to only part of the backlog, e.g. an older batch, leaving newer
    uploads for a human to review as normal."""
    if not settings.ADMIN_SECRET or x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    return await telegram_bot.push_all_unresolved(since=since, until=until)


@app.post("/admin/skip_tokens")
async def admin_skip_tokens(
    tokens: list[str] = Body(..., embed=True),
    x_admin_secret: Optional[str] = Header(None),
):
    """One-off: mark specific approval_log tokens resolved without sending —
    for confirmed (MD5-verified) duplicate uploads that should not be distributed
    or reach the fb-content-tracker sync. Remove after use."""
    if not settings.ADMIN_SECRET or x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    return await telegram_bot.skip_tokens(tokens)


@app.post("/admin/bulk_approve")
async def admin_bulk_approve(request: Request, x_admin_secret: Optional[str] = Header(None)):
    if not settings.ADMIN_SECRET or x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    body = await request.json()
    models = body.get("models", [])
    if not models:
        raise HTTPException(status_code=400, detail="models required")
    return await telegram_bot.bulk_approve(models)


@app.get("/admin/debug_log_search")
async def admin_debug_log_search(q: str, x_admin_secret: Optional[str] = Header(None)):
    """Temporary diagnostic: search ALL approval_log entries (resolved or not)
    by filename substring. Remove after use."""
    if not settings.ADMIN_SECRET or x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    data = approval_log._load()
    matches = [
        {"token": token, **entry}
        for token, entry in data.items()
        if q.lower() in entry.get("file_name", "").lower()
    ]
    return {"matches": matches}




@app.get("/admin/failed_uploads")
async def admin_list_failed(x_admin_secret: Optional[str] = Header(None)):
    if not settings.ADMIN_SECRET or x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    items = []
    for filename in _list_failed_uploads():
        with open(os.path.join(FAILED_DIR, filename)) as f:
            uploads = json.load(f)
        items.append({
            "file": filename,
            "model": uploads[0].get("model", ""),
            "content_type": uploads[0].get("content_type", ""),
            "va_name": uploads[0].get("va_name", ""),
            "files": [u.get("file_name") for u in uploads],
        })
    return {"failed": items}


@app.post("/admin/retry_failed")
async def admin_retry_failed(x_admin_secret: Optional[str] = Header(None)):
    if not settings.ADMIN_SECRET or x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    results = {}
    for filename in _list_failed_uploads():
        error = await _retry_failed_upload(filename)
        results[filename] = "ok" if error is None else error
    return {"results": results}


@app.post("/admin/clear_backlog_tracking")
async def admin_clear_backlog_tracking(x_admin_secret: Optional[str] = Header(None)):
    """Mark every unresolved approval-log entry as resolved WITHOUT sending or
    approving anything — pure tracking reset so the strike/overdue count (and
    Bjarne's open-video count) starts at zero. Videos are untouched on Drive;
    their Telegram buttons stay visible but become inert."""
    if not settings.ADMIN_SECRET or x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    count = telegram_bot.approval_log.resolve_all_unresolved()
    return {"cleared": count}


@app.get("/admin/unresolved_approvals")
async def admin_unresolved_approvals(x_admin_secret: Optional[str] = Header(None)):
    if not settings.ADMIN_SECRET or x_admin_secret != settings.ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    data = telegram_bot.approval_log._load()
    unresolved = [
        {"token": t, **{k: v for k, v in e.items() if k != "resolved"}}
        for t, e in data.items() if not e["resolved"]
    ]
    return {"unresolved": unresolved, "count": len(unresolved)}


@app.get("/health")
async def health():
    return {"status": "ok"}
