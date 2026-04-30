import json
import logging
import os
import secrets
import tempfile
import httpx
from hydrogram import Client
from config import settings, TELEGRAM_ROUTING, VIDEO_DISTRIBUTION_TARGETS

APPROVAL_CHAT_ID = -5246342505
PENDING_APPROVALS: dict = {}  # token → {video_data, file_name, target, model_name}

logger = logging.getLogger(__name__)

SHERRY_HICKS_VARIANTS = {"Sherry Hicks", "Sherry Hicks Shell"}


def _telegram_api() -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"


def _should_notify(va: dict, model_name: str, content_type: str) -> bool:
    """Decide whether a VA should receive a notification for this upload."""
    if va["rule"] == "always":
        return True

    # content_types_all_models → any model triggers it
    if content_type in va.get("content_types_all_models", []):
        return True

    # content_types_sherry_only → only Sherry Hicks variants
    if (
        content_type in va.get("content_types_sherry_only", [])
        and model_name in SHERRY_HICKS_VARIANTS
    ):
        return True

    # content_types_margaret_only → only Margaret Asian
    if (
        content_type in va.get("content_types_margaret_only", [])
        and model_name == "Margaret Asian"
    ):
        return True

    return False


VA6_CHAT_ID = "8371406259"


def _build_message(
    model_name: str,
    content_type: str,
    date_str: str,
    drive_link: str,
) -> str:
    # Exact format from spec — no deviations
    return (
        f"New Content - {model_name}\n"
        f"This Model has uploaded some new content.\n"
        f"You can see it in this folder:\n"
        f"{model_name} ➔ {content_type} ➔ not edited ➔ {date_str}\n"
        f"{drive_link}\n"
        f"Please wait a couple of minutes, as the file is being converted "
        f"and this may take up to 10 minutes."
    )


async def send_error_notification(detail: str) -> None:
    """Send upload failure notice to VA 6 only."""
    message = f"⚠️ Upload fehlgeschlagen: Webhook-Daten unvollständig oder fehlend."
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            await client.post(
                f"{_telegram_api()}/sendMessage",
                json={"chat_id": VA6_CHAT_ID, "text": message},
            )
        except Exception as e:
            logger.error(f"Failed to send error notification: {e}")


async def send_for_approval(videos: list[dict], model_name: str, content_type: str) -> None:
    """Send each video to approval group with ✅/❌ buttons."""
    targets = VIDEO_DISTRIBUTION_TARGETS
    half = len(videos) // 2 or 1

    async with httpx.AsyncClient(timeout=180) as client:
        for i, video in enumerate(videos):
            token = secrets.token_urlsafe(16)
            target = targets[0] if i < half else targets[1]
            PENDING_APPROVALS[token] = {
                "video_data": video["data"],
                "file_name": video["file_name"],
                "target": target,
                "model_name": model_name,
                "content_type": content_type,
            }
            caption = (
                f"🎬 {model_name} — {content_type}\n"
                f"📁 {video['file_name']}\n"
                f"→ Thread {target['thread_id']}"
            )
            resp = await client.post(
                f"{_telegram_api()}/sendVideo",
                data={
                    "chat_id": APPROVAL_CHAT_ID,
                    "caption": caption,
                    "reply_markup": json.dumps({"inline_keyboard": [[
                        {"text": "✅ Approve", "callback_data": f"approve:{token}"},
                        {"text": "❌ Reject",  "callback_data": f"reject:{token}"},
                    ]]}),
                },
                files={"video": (video["file_name"], video["data"], "video/mp4")},
            )
            if resp.status_code == 200:
                logger.info(f"Sent for approval: {video['file_name']}")
            else:
                logger.warning(f"Approval send failed: {resp.text}")


async def handle_callback(cq: dict) -> None:
    """Handle approve/reject button presses."""
    data = cq.get("data", "")
    message = cq.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if ":" not in data:
        return
    action, token = data.split(":", 1)

    if token not in PENDING_APPROVALS:
        await _edit_caption(chat_id, message_id, "⚠️ Session abgelaufen — bitte neu hochladen")
        return

    pending = PENDING_APPROVALS.pop(token)

    if action == "approve":
        await _edit_caption(chat_id, message_id,
            f"⏳ Wird gesendet → Thread {pending['target']['thread_id']}…")
        await _send_approved_video(pending)
        await _edit_caption(chat_id, message_id,
            f"✅ Gesendet → Thread {pending['target']['thread_id']}\n📁 {pending['file_name']}")
    elif action == "reject":
        await _edit_caption(chat_id, message_id,
            f"❌ Abgelehnt — {pending['file_name']}")


async def _edit_caption(chat_id: int, message_id: int, text: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{_telegram_api()}/editMessageCaption",
            json={"chat_id": chat_id, "message_id": message_id, "caption": text},
        )


async def _send_approved_video(pending: dict) -> None:
    """Send a single approved video as .mp4 document via Hydrogram."""
    target = pending["target"]
    session_string = os.environ.get("TG_SESSION", "")
    api_id = int(os.environ.get("TG_API_ID", 0))
    api_hash = os.environ.get("TG_API_HASH", "")

    async with Client("uploader", api_id=api_id, api_hash=api_hash, session_string=session_string) as app:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(pending["video_data"])
            tmp_path = tmp.name
        try:
            await app.send_document(
                chat_id=int(target["chat_id"]),
                document=tmp_path,
                file_name=pending["file_name"],
                message_thread_id=target["thread_id"],
                force_document=True,
            )
            logger.info(f"Approved & sent: {pending['file_name']} → thread {target['thread_id']}")
        except Exception as e:
            logger.error(f"Error sending approved video: {e}")
        finally:
            os.unlink(tmp_path)


async def distribute_videos(videos: list[dict]) -> None:
    """
    Distribute converted videos across VIDEO_DISTRIBUTION_TARGETS via Pyrogram.
    First half → target[0], second half → target[1].
    Uses MTProto (force_document=True) so MP4 arrives as file, not video.
    """
    if not videos:
        return

    session_string = os.environ.get("TG_SESSION", "")
    if not session_string:
        logger.error("TG_SESSION not set — skipping video distribution")
        return

    api_id = int(os.environ.get("TG_API_ID", 0))
    api_hash = os.environ.get("TG_API_HASH", "")
    targets = VIDEO_DISTRIBUTION_TARGETS
    half = len(videos) // 2 or 1

    logger.info(f"Distributing {len(videos)} video(s) across {len(targets)} targets via Pyrogram")

    async with Client("uploader", api_id=api_id, api_hash=api_hash, session_string=session_string) as app:
        for i, video in enumerate(videos):
            target = targets[0] if i < half else targets[1]

            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                tmp.write(video["data"])
                tmp_path = tmp.name

            try:
                await app.send_document(
                    chat_id=int(target["chat_id"]),
                    document=tmp_path,
                    file_name=video["file_name"],
                    message_thread_id=target["thread_id"],
                    force_document=True,
                )
                logger.info(f"Sent {video['file_name']} → thread {target['thread_id']}")
            except Exception as e:
                logger.error(f"Error sending {video['file_name']}: {e}")
            finally:
                os.unlink(tmp_path)


async def send_notifications(
    model_name: str,
    content_type: str,
    date_str: str,
    drive_links: list[str],
) -> None:
    message = _build_message(model_name, content_type, date_str, drive_links[0])

    async with httpx.AsyncClient(timeout=15) as client:
        for va in TELEGRAM_ROUTING:
            if not _should_notify(va, model_name, content_type):
                continue

            try:
                resp = await client.post(
                    f"{_telegram_api()}/sendMessage",
                    json={
                        "chat_id": va["chat_id"],
                        "text": message,
                        "disable_web_page_preview": False,
                    },
                )
                if resp.status_code == 200:
                    logger.info(f"Notified {va['name']} (chat_id={va['chat_id']})")
                else:
                    logger.warning(
                        f"Failed to notify {va['name']}: {resp.status_code} {resp.text}"
                    )
            except Exception as e:
                logger.error(f"Error notifying {va['name']}: {e}")
