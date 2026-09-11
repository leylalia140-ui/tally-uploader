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

from config import SLOT_CREATORS
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


# Mirrors main.py._process_uploads_core's approval-gate condition exactly —
# used for every form (VA and model-self-upload alike, the original code
# doesn't distinguish by form_id for this check, only for the folder rule
# below).
_NICHE_TOPIC_MODELS = ("Margaret Asian", "Abby Parker", "Yuki Chen", "Bertha Butts")
_NICHE_TOPIC_CONTENT_TYPES = ("Instagram Reels", "Instagram AI Reels")


def _should_send_for_approval(model: str, content_type: str, is_img: bool) -> bool:
    if is_img:
        return False
    return (model in SLOT_CREATORS and content_type == "Full AI Content") or (
        model in _NICHE_TOPIC_MODELS and content_type in _NICHE_TOPIC_CONTENT_TYPES
    )


def _folder_path(model: str, content_type: str, date_str: str, subfolder: str = "edited") -> list[str]:
    return ["Models", model, content_type, subfolder, date_str]


def _model_self_upload_subfolder(model: str, content_type: str) -> str:
    # Mirrors main.py._process_uploads_core's non-wAq9ql branch exactly.
    content_lower = content_type.lower()
    if model == "Sherry Hicks":
        return "not edited"
    if model == "Margaret Asian" and any(k in content_lower for k in ("ppv", "feed")):
        return "not edited"
    return "edited"


@router.get("/upload/va")
async def va_upload_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "va_upload.html"))


@router.get("/upload/model-de")
async def model_upload_de_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "model_upload_de.html"))


@router.get("/upload/model-es")
async def model_upload_es_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "model_upload_es.html"))


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
    drive_file_id: str | None = None
    date_str: str | None = None


async def _resolve_uploaded_file_id(body: "FileDoneRequest", drive: GoogleDriveClient) -> str | None:
    """
    The browser couldn't read Google's resumable-upload completion response
    (missing CORS headers for third-party origins on that specific response —
    verified: the upload itself succeeds regardless), so it doesn't know the
    file's ID. Find it the same way it was placed: by name, in the exact
    destination folder we resolved at /init time. Drive's own list index can
    lag by a moment right after upload, so retry briefly before giving up.
    """
    folder_id = await asyncio.to_thread(
        drive.resolve_folder_path, _folder_path(body.model, body.content_type, body.date_str)
    )
    type_folder_name = "Images" if is_image(body.file_name, body.mime_type) else "Videos"
    upload_folder_id = await asyncio.to_thread(drive.get_or_create_folder, type_folder_name, folder_id)
    for attempt in range(5):
        found = await asyncio.to_thread(drive.find_file, body.file_name, upload_folder_id)
        if found:
            return found
        await asyncio.sleep(2 * (attempt + 1))
    return None


async def _process_file_done(body: FileDoneRequest) -> None:
    drive = await asyncio.to_thread(GoogleDriveClient)
    tmp_path = None
    try:
        drive_file_id = body.drive_file_id or await _resolve_uploaded_file_id(body, drive)
        if not drive_file_id:
            logger.error(f"va-upload: could not find {body.file_name} in Drive after upload — giving up")
            return

        ext = os.path.splitext(body.file_name)[1] or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_f:
            tmp_path = tmp_f.name
        await asyncio.to_thread(drive.download_file, drive_file_id, tmp_path)

        is_img = is_image(body.file_name, body.mime_type)
        if _should_send_for_approval(body.model, body.content_type, is_img):
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


# ──────────────────────────────────────────────────────────────
# Model self-upload forms (German "Content Hochladen" / Spanish "Subir
# Contenido") — same mechanics as the VA form above, but: no niche field, no
# va_name field (models upload for themselves), and a different folder-
# subfolder rule (mirrors main.py's non-wAq9ql branch: Sherry Hicks and
# Margaret Asian PPV/Feed content go to "not edited").
# ──────────────────────────────────────────────────────────────

class ModelInitRequest(BaseModel):
    model: str
    content_type: str
    file_name: str
    mime_type: str
    date_str: str | None = None


@router.post("/api/model-upload/init")
async def model_upload_init(body: ModelInitRequest):
    date_str = body.date_str or format_date(datetime.now(BERLIN))
    subfolder = _model_self_upload_subfolder(body.model, body.content_type)

    drive = await asyncio.to_thread(GoogleDriveClient)
    folder_id = await asyncio.to_thread(
        drive.resolve_folder_path, _folder_path(body.model, body.content_type, date_str, subfolder)
    )
    type_folder_name = "Images" if is_image(body.file_name, body.mime_type) else "Videos"
    upload_folder_id = await asyncio.to_thread(drive.get_or_create_folder, type_folder_name, folder_id)

    upload_url = await asyncio.to_thread(
        drive.create_resumable_session, body.file_name, upload_folder_id, body.mime_type
    )
    return {"upload_url": upload_url, "date_str": date_str}


class ModelFileDoneRequest(BaseModel):
    model: str
    content_type: str
    file_name: str
    mime_type: str
    drive_file_id: str | None = None
    date_str: str | None = None


async def _resolve_model_uploaded_file_id(body: "ModelFileDoneRequest", drive: GoogleDriveClient) -> str | None:
    subfolder = _model_self_upload_subfolder(body.model, body.content_type)
    folder_id = await asyncio.to_thread(
        drive.resolve_folder_path, _folder_path(body.model, body.content_type, body.date_str, subfolder)
    )
    type_folder_name = "Images" if is_image(body.file_name, body.mime_type) else "Videos"
    upload_folder_id = await asyncio.to_thread(drive.get_or_create_folder, type_folder_name, folder_id)
    for attempt in range(5):
        found = await asyncio.to_thread(drive.find_file, body.file_name, upload_folder_id)
        if found:
            return found
        await asyncio.sleep(2 * (attempt + 1))
    return None


async def _process_model_file_done(body: ModelFileDoneRequest) -> None:
    drive = await asyncio.to_thread(GoogleDriveClient)
    tmp_path = None
    try:
        drive_file_id = body.drive_file_id or await _resolve_model_uploaded_file_id(body, drive)
        if not drive_file_id:
            logger.error(f"model-upload: could not find {body.file_name} in Drive after upload — giving up")
            return

        ext = os.path.splitext(body.file_name)[1] or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp_f:
            tmp_path = tmp_f.name
        await asyncio.to_thread(drive.download_file, drive_file_id, tmp_path)

        is_img = is_image(body.file_name, body.mime_type)
        if _should_send_for_approval(body.model, body.content_type, is_img):
            await telegram_bot.send_for_approval(
                [{"file_name": body.file_name, "path": tmp_path}],
                body.model, body.content_type, "", "",
            )
            tmp_path = None
        else:
            logger.info(f"model-upload: {body.file_name} stored on Drive, no approval flow needed")
    except Exception:
        logger.exception(f"model-upload file-done failed for {body.file_name}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@router.post("/api/model-upload/file-done")
async def model_upload_file_done(body: ModelFileDoneRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_process_model_file_done, body)
    return {"status": "accepted"}


class ModelFinalizeRequest(BaseModel):
    model: str
    content_type: str
    date_str: str


async def _process_model_finalize(body: ModelFinalizeRequest) -> None:
    try:
        subfolder = _model_self_upload_subfolder(body.model, body.content_type)
        drive = await asyncio.to_thread(GoogleDriveClient)
        folder_id = await asyncio.to_thread(
            drive.resolve_folder_path, _folder_path(body.model, body.content_type, body.date_str, subfolder)
        )
        folder_link = await asyncio.to_thread(drive.make_folder_public, folder_id)
        await telegram_bot.send_notifications(
            model_name=body.model,
            content_type=body.content_type,
            date_str=body.date_str,
            drive_links=[folder_link],
        )
    except Exception:
        logger.exception("model-upload finalize failed")


@router.post("/api/model-upload/finalize")
async def model_upload_finalize(body: ModelFinalizeRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_process_model_finalize, body)
    return {"status": "accepted"}
