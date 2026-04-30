import logging
import httpx
from config import settings, TELEGRAM_ROUTING, VIDEO_DISTRIBUTION_TARGETS

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


async def distribute_videos(videos: list[dict]) -> None:
    """
    Distribute converted videos round-robin across VIDEO_DISTRIBUTION_TARGETS.
    videos = [{"file_name": "...", "data": b"..."}]
    """
    if not videos:
        return

    targets = VIDEO_DISTRIBUTION_TARGETS
    logger.info(f"Distributing {len(videos)} video(s) across {len(targets)} targets")

    half = len(videos) // 2 or 1

    async with httpx.AsyncClient(timeout=180) as client:
        for i, video in enumerate(videos):
            target = targets[0] if i < half else targets[1]
            size_mb = len(video["data"]) / 1024 / 1024

            if size_mb > 50:
                logger.warning(f"{video['file_name']} is {size_mb:.1f} MB > 50 MB — skipping")
                continue

            try:
                resp = await client.post(
                    f"{_telegram_api()}/sendDocument",
                    data={
                        "chat_id": target["chat_id"],
                        "message_thread_id": target["thread_id"],
                    },
                    files={"document": (video["file_name"], video["data"], "video/mp4")},
                )
                if resp.status_code == 200:
                    logger.info(f"Sent {video['file_name']} → thread {target['thread_id']}")
                else:
                    logger.warning(f"Failed ({resp.status_code}): {resp.text}")
            except Exception as e:
                logger.error(f"Error sending {video['file_name']}: {e}")


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
