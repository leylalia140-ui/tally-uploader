"""
Probe: a self-hosted 1:1 replica of the Tally "VA Content Upload" form
(https://tally.so/r/wAq9ql), replacing Tally as the intake step.

Key difference from the Tally-webhook flow in main.py: the browser uploads
files directly to Google Drive via a resumable session (bytes never pass
through this server, so Railway's 5-minute inbound-request-body limit never
applies, and multi-GB files survive flaky connections via chunked retries).
Everything downstream (approval flow, notifications) reuses the exact same
telegram_bot functions the Tally path already uses.
"""

import asyncio
import logging
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel

from dateutil_local import format_date
from drive import GoogleDriveClient
import telegram_bot

logger = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")

router = APIRouter()

_IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff"}
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}


def is_image(file_name: str, mime_type: str) -> bool:
    ext = os.path.splitext(file_name)[1].lower()
    return mime_type.lower() in _IMAGE_MIME_TYPES or ext in _IMAGE_EXTENSIONS


# Same literal string the live Tally dropdown uses for this content type —
# NOTE this differs in casing from main.py's INSTAGRAM_FEED_PICTURES_CONTENT_TYPE
# ("Instagram FEED PICTURES"), an existing mismatch kept as-is for 1:1 parity.
APPROVAL_MODELS = ("Margaret Asian", "Abby Parker", "Yuki Chen", "Bertha Butts")
APPROVAL_CONTENT_TYPES = ("Instagram Reels", "Instagram AI Reels")


def _folder_path(model: str, content_type: str, date_str: str) -> list[str]:
    # Mirrors main.py._process_uploads_core's form_id == "wAq9ql" branch: always "edited".
    return ["Models", model, content_type, "edited", date_str]


@router.get("/upload/va")
async def va_upload_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "va_upload.html"))


class InitRequest(BaseModel):
    model: str
    content_type: str
    file_name: str
    mime_type: str
    date_str: str | None = None


@router.post("/api/va-upload/init")
async def va_upload_init(body: InitRequest):
    date_str = body.date_str or format_date(datetime.now(BERLIN))

    # GoogleDriveClient/googleapiclient is fully synchronous/blocking — every call
    # must run via asyncio.to_thread or a slow/hung Drive API response freezes the
    # whole process for every other request (this happened once in production,
    # see main.py's comment on the same pattern).
    drive = await asyncio.to_thread(GoogleDriveClient)
    folder_id = await asyncio.to_thread(
        drive.resolve_folder_path, _folder_path(body.model, body.content_type, date_str)
    )
    type_folder_name = "Images" if is_image(body.file_name, body.mime_type) else "Videos"
    upload_folder_id = await asyncio.to_thread(drive.get_or_create_folder, type_folder_name, folder_id)

    upload_url = await asyncio.to_thread(
        drive.create_resumable_session, body.file_name, upload_folder_id, body.mime_type
    )
    return {"upload_url": upload_url, "date_str": date_str}


class FileDoneRequest(BaseModel):
    model: str
    content_type: str
    niche: str = ""
    va_name: str = ""
    file_name: str
    mime_type: str
    drive_file_id: str


async def _process_file_done(body: FileDoneRequest) -> None:
    drive = await asyncio.to_thread(GoogleDriveClient)
    tmp_path = None
    try:
        ext = os.path.splitext(body.file_name)[1] or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_f:
            tmp_path = tmp_f.name
        await asyncio.to_thread(drive.download_file, body.drive_file_id, tmp_path)

        if not is_image(body.file_name, body.mime_type) and (
            body.model in APPROVAL_MODELS and body.content_type in APPROVAL_CONTENT_TYPES
        ):
            await telegram_bot.send_for_approval(
                [{"file_name": body.file_name, "path": tmp_path}],
                body.model, body.content_type, body.niche, body.va_name,
            )
            # telegram_bot now owns the file and cleans it up after approve/reject
            tmp_path = None
        else:
            logger.info(f"va-upload: {body.file_name} stored on Drive, no approval flow needed")
    except Exception:
        logger.exception(f"va-upload file-done failed for {body.file_name}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.post("/api/va-upload/file-done")
async def va_upload_file_done(body: FileDoneRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_process_file_done, body)
    return {"status": "accepted"}


class FinalizeRequest(BaseModel):
    model: str
    content_type: str
    date_str: str


async def _process_finalize(body: FinalizeRequest) -> None:
    try:
        drive = await asyncio.to_thread(GoogleDriveClient)
        folder_id = await asyncio.to_thread(
            drive.resolve_folder_path, _folder_path(body.model, body.content_type, body.date_str)
        )
        folder_link = await asyncio.to_thread(drive.make_folder_public, folder_id)
        await telegram_bot.send_notifications(
            model_name=body.model,
            content_type=body.content_type,
            date_str=body.date_str,
            drive_links=[folder_link],
        )
    except Exception:
        logger.exception("va-upload finalize failed")


@router.post("/api/va-upload/finalize")
async def va_upload_finalize(body: FinalizeRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_process_finalize, body)
    return {"status": "accepted"}
