import hashlib
import logging
import os
import secrets
import httpx
from datetime import date
from zoneinfo import ZoneInfo
from hydrogram import Client
from config import (
    settings, TELEGRAM_ROUTING, VIDEO_DISTRIBUTION_TARGETS,
    SLOT_CREATORS, NICHE_TOPICS, AI_MODELS_REELS_CHAT_ID, SLOTS_PER_CREATOR,
    VA_TELEGRAM_IDS, JEREMI_TELEGRAM_ID, STRIKE_GROUP_CHAT_ID,
    APPROVAL_DEADLINE_HOUR, PERSON_TELEGRAM_IDS, PERSON_DISPLAY_NAMES,
    STRIKE_MONTHLY_DISPLAY_MAX, ACTIVITY_STRIKE_TASKS,
)
import approval_log
import activity_log
import strikes

MONITORED_ACTIVITY_CHAT_IDS = {t["chat_id"] for t in ACTIVITY_STRIKE_TASKS}

BERLIN = ZoneInfo("Europe/Berlin")
APPROVAL_CHAT_ID = -1004259848545
PENDING_APPROVALS: dict = {}   # token → {file_path, file_name, model_name, content_type, slots, topic_id, behave_target, va_name}
REJECTED_APPROVALS: dict = {}  # token → pending-dict + "rejected_at" + "chat_id" + "message_id"
AWAITING_REASON: dict = {}    # force-reply message_id → {token, va_name, file_name}

# ── Daily slot counters (reset each Berlin midnight) ──
_daily_counters: dict[str, dict] = {}  # creator → {"date": date, "count": int}
_daily_hashes: dict[str, dict] = {}    # creator → {"date": date, "hashes": set}


def _today() -> date:
    from datetime import datetime
    return datetime.now(BERLIN).date()


def _get_and_increment_counter(creator: str) -> int:
    today = _today()
    if creator not in _daily_counters or _daily_counters[creator]["date"] != today:
        _daily_counters[creator] = {"date": today, "count": 0}
    idx = _daily_counters[creator]["count"]
    _daily_counters[creator]["count"] += 1
    return idx


def _compute_hash(file_path: str) -> str:
    h = hashlib.md5()
    h.update(str(os.path.getsize(file_path)).encode())
    with open(file_path, "rb") as f:
        h.update(f.read(10 * 1024 * 1024))
    return h.hexdigest()


def _is_duplicate(creator: str, file_path: str) -> bool:
    today = _today()
    if creator not in _daily_hashes or _daily_hashes[creator]["date"] != today:
        _daily_hashes[creator] = {"date": today, "hashes": set()}
    file_hash = _compute_hash(file_path)
    if file_hash in _daily_hashes[creator]["hashes"]:
        return True
    _daily_hashes[creator]["hashes"].add(file_hash)
    return False


def _remove_hash(creator: str, file_path: str) -> None:
    """Remove a file's hash from the duplicate-detection set (used on reject/undo)."""
    if creator not in _daily_hashes:
        return
    today = _today()
    if _daily_hashes[creator]["date"] != today:
        return
    try:
        file_hash = _compute_hash(file_path)
        _daily_hashes[creator]["hashes"].discard(file_hash)
    except Exception:
        pass


def _get_slots(video_index: int) -> list[int]:
    """Return 3 slot numbers for this video (1-indexed, sequential rotation)."""
    start = (video_index * 3) % SLOTS_PER_CREATOR
    return [(start + i) % SLOTS_PER_CREATOR + 1 for i in range(3)]

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
    message = f"⚠️ Upload fehlgeschlagen: {detail}"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            await client.post(
                f"{_telegram_api()}/sendMessage",
                json={"chat_id": VA6_CHAT_ID, "text": message},
            )
        except Exception as e:
            logger.error(f"Failed to send error notification: {e}")


async def send_for_approval(videos: list[dict], model_name: str, content_type: str, niche: str = "", va_name: str = "") -> None:
    """Send each video to approval group as .mp4 file + separate buttons message.
    videos: list of {"file_name": str, "path": str} — paths to temp files on disk.
    Files are kept alive in PENDING_APPROVALS and deleted after approve/reject."""
    # approval_topic: always the model's main topic in the approval group
    approval_topic_id = SLOT_CREATORS[model_name]
    # dest_topic: niche-specific topic in AI Models Reels (used after approval)
    dest_topic_id = NICHE_TOPICS.get((model_name, niche)) or SLOT_CREATORS[model_name]

    session_string = os.environ.get("TG_SESSION", "")
    api_id = int(os.environ.get("TG_API_ID", 0))
    api_hash = os.environ.get("TG_API_HASH", "")

    async with Client("uploader", api_id=api_id, api_hash=api_hash, session_string=session_string) as app:
        try:
            await app.get_chat(APPROVAL_CHAT_ID)
        except Exception as e:
            logger.warning(f"get_chat pre-fetch: {e}")
        async with httpx.AsyncClient(timeout=180) as client:
            for i, video in enumerate(videos):
                # Duplicate check
                is_dup = _is_duplicate(model_name, video["path"])

                # Calculate slots
                video_index = _get_and_increment_counter(model_name)
                slots = _get_slots(video_index)
                slots_text = " | ".join(f"Account {s}" for s in slots)

                # For Margaret: also route to Behave x Amin
                behave_target = None
                if model_name == "Margaret Asian":
                    behave_target = VIDEO_DISTRIBUTION_TARGETS[video_index % 2]

                token = secrets.token_urlsafe(16)
                PENDING_APPROVALS[token] = {
                    "file_path": video["path"],
                    "file_name": video["file_name"],
                    "model_name": model_name,
                    "content_type": content_type,
                    "niche": niche,
                    "topic_id": dest_topic_id,
                    "slots": slots,
                    "behave_target": behave_target,
                    "va_name": va_name,
                }
                approval_log.log_created(token, model_name, video["file_name"], va_name)

                # 1. Send file as .mp4 document via Hydrogram (directly from disk)
                try:
                    await app.send_document(
                        chat_id=APPROVAL_CHAT_ID,
                        document=video["path"],
                        file_name=video["file_name"],
                        message_thread_id=approval_topic_id,
                        force_document=True,
                    )
                    logger.info(f"Sent file for approval: {video['file_name']}")
                except Exception as e:
                    logger.error(f"Error sending approval file: {e}")

                # 2. Send buttons message via Bot API
                dup_prefix = "⚠️ Duplikat!\n" if is_dup else ""
                niche_line = f"🏷 {niche}\n" if niche else ""
                va_line = f"👤 {va_name}\n" if va_name else ""
                caption = (
                    f"{dup_prefix}🎬 {model_name} — {content_type}\n"
                    f"{niche_line}"
                    f"{va_line}"
                    f"📁 {video['file_name']}\n"
                    f"📌 {slots_text}"
                )
                resp = await client.post(
                    f"{_telegram_api()}/sendMessage",
                    json={
                        "chat_id": APPROVAL_CHAT_ID,
                        "message_thread_id": approval_topic_id,
                        "text": caption,
                        "reply_markup": {"inline_keyboard": [[
                            {"text": "✅ Approve", "callback_data": f"approve:{token}"},
                            {"text": "❌ Reject",  "callback_data": f"reject:{token}"},
                        ]]},
                    },
                )
                if resp.status_code != 200:
                    logger.warning(f"Buttons send failed: {resp.text}")


async def _edit_caption_with_buttons(chat_id: int, message_id: int, text: str, keyboard: list) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{_telegram_api()}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )


async def handle_callback(cq: dict) -> None:
    """Handle approve/reject/undo button presses."""
    from datetime import datetime, timedelta
    data = cq.get("data", "")
    message = cq.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if ":" not in data:
        return
    action, token = data.split(":", 1)

    # ── Undo rejected ──
    if action == "undo":
        if token not in REJECTED_APPROVALS:
            await _edit_caption(chat_id, message_id, "⚠️ Rückgängig nicht mehr möglich — Datei bereits gelöscht")
            return
        rejected = REJECTED_APPROVALS.pop(token)
        file_path = rejected.get("file_path", "")
        if not file_path or not os.path.exists(file_path):
            await _edit_caption(chat_id, message_id, "⚠️ Datei nicht mehr verfügbar — bitte neu hochladen")
            return
        # Restore to pending (hash already removed on reject, re-add it)
        _daily_hashes.setdefault(rejected["model_name"], {"date": _today(), "hashes": set()})
        try:
            _daily_hashes[rejected["model_name"]]["hashes"].add(_compute_hash(file_path))
        except Exception:
            pass
        PENDING_APPROVALS[token] = {k: v for k, v in rejected.items()
                                     if k not in ("rejected_at", "chat_id", "message_id")}
        approval_log.set_resolved(token, False)
        slots_text = " | ".join(f"Account {s}" for s in rejected.get("slots", []))
        va_line = f"👤 {rejected['va_name']}\n" if rejected.get("va_name") else ""
        caption = (
            f"🎬 {rejected['model_name']} — {rejected['content_type']}\n"
            f"{va_line}"
            f"📁 {rejected['file_name']}\n"
            f"📌 {slots_text}"
        )
        await _edit_caption_with_buttons(chat_id, message_id, caption, [[
            {"text": "✅ Approve", "callback_data": f"approve:{token}"},
            {"text": "❌ Reject",  "callback_data": f"reject:{token}"},
        ]])
        return

    if token not in PENDING_APPROVALS:
        await _edit_caption(chat_id, message_id, "⚠️ Session abgelaufen — bitte neu hochladen")
        return

    pending = PENDING_APPROVALS.pop(token)

    if action == "approve":
        approval_log.set_resolved(token, True)
        slots_text = " | ".join(f"Account {s}" for s in pending.get("slots", []))
        await _edit_caption(chat_id, message_id,
            f"⏳ Wird gesendet → AI Models Reels ({pending['model_name']})…")
        await _send_approved_video(pending)
        await _edit_caption(chat_id, message_id,
            f"✅ Gesendet → {pending['model_name']}\n📌 {slots_text}\n📁 {pending['file_name']}")
    elif action == "reject":
        approval_log.set_resolved(token, True)
        file_path = pending.get("file_path", "")
        # Remove hash so the video can be re-uploaded via Tally without duplicate warning
        if file_path and os.path.exists(file_path):
            _remove_hash(pending["model_name"], file_path)
        # Keep file alive for undo; store in REJECTED_APPROVALS
        REJECTED_APPROVALS[token] = {
            **pending,
            "rejected_at": datetime.now(),
            "chat_id": chat_id,
            "message_id": message_id,
        }
        await _edit_caption_with_buttons(chat_id, message_id,
            f"❌ Abgelehnt — {pending['file_name']}",
            [[{"text": "↩️ Rückgängig", "callback_data": f"undo:{token}"}]]
        )

        # Ask for a reject reason to forward to the VA via DM
        va_name = pending.get("va_name", "")
        if va_name and va_name in VA_TELEGRAM_IDS:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{_telegram_api()}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "message_thread_id": message.get("message_thread_id"),
                        "text": f"✏️ Was muss {va_name} besser machen? (Antworte auf diese Nachricht — {va_name} bekommt sie per DM)",
                        "reply_markup": {"force_reply": True, "selective": True},
                    },
                )
                data_resp = resp.json()
                if data_resp.get("ok"):
                    prompt_message_id = data_resp["result"]["message_id"]
                    AWAITING_REASON[prompt_message_id] = {
                        "va_name": va_name,
                        "file_name": pending["file_name"],
                        "file_path": pending.get("file_path", ""),
                    }
                else:
                    logger.warning(f"Reject-reason prompt failed: {data_resp}")


async def handle_reason_reply(message: dict) -> None:
    """Handle a reply to the reject-reason ForceReply prompt — forwards the reason to the VA via DM."""
    reply_to = message.get("reply_to_message")
    if not reply_to:
        return
    prompt_id = reply_to.get("message_id")
    if prompt_id not in AWAITING_REASON:
        return
    info = AWAITING_REASON.pop(prompt_id)
    reason_text = message.get("text", "").strip()
    va_name = info["va_name"]
    va_user_id = VA_TELEGRAM_IDS.get(va_name)
    chat_id = message.get("chat", {}).get("id")
    thread_id = message.get("message_thread_id")

    if not reason_text or not va_user_id:
        return

    caption = f"🔁 This video needs to be reworked:\n\n{reason_text}"
    file_path = info.get("file_path", "")

    async with httpx.AsyncClient(timeout=60) as client:
        if file_path and os.path.exists(file_path):
            with open(file_path, "rb") as f:
                dm_resp = await client.post(
                    f"{_telegram_api()}/sendDocument",
                    data={"chat_id": va_user_id, "caption": caption},
                    files={"document": (info["file_name"], f)},
                )
        else:
            dm_resp = await client.post(
                f"{_telegram_api()}/sendMessage",
                json={"chat_id": va_user_id, "text": caption},
            )

        if dm_resp.json().get("ok"):
            await client.post(
                f"{_telegram_api()}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "message_thread_id": thread_id,
                    "text": f"✅ An {va_name} geschickt (mit Video): \"{reason_text}\"",
                },
            )
        else:
            logger.warning(f"Reject-reason DM to {va_name} failed: {dm_resp.text}")
            await client.post(
                f"{_telegram_api()}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "message_thread_id": thread_id,
                    "text": f"⚠️ Konnte {va_name} nicht per DM erreichen — hat er/sie den Bot gestartet?",
                },
            )


async def _edit_caption(chat_id: int, message_id: int, text: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"{_telegram_api()}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id, "text": text},
        )


async def _send_approved_video(pending: dict) -> None:
    """Send approved video to AI Models Reels topic (+ Behave x Amin for Margaret). Deletes temp file after."""
    file_path = pending["file_path"]
    slots_caption = (
        "📌 Post on: " + " | ".join(f"Account {s}" for s in pending.get("slots", [])) + "\n"
        "⚠️ This video may only be posted on these 3 accounts — do not post it anywhere else!"
    )
    session_string = os.environ.get("TG_SESSION", "")
    api_id = int(os.environ.get("TG_API_ID", 0))
    api_hash = os.environ.get("TG_API_HASH", "")

    async with Client("uploader", api_id=api_id, api_hash=api_hash, session_string=session_string) as app:
        try:
            # 1. Send to AI Models Reels (creator-specific topic)
            await app.send_document(
                chat_id=AI_MODELS_REELS_CHAT_ID,
                document=file_path,
                file_name=pending["file_name"],
                message_thread_id=pending["topic_id"],
                caption=slots_caption,
                force_document=True,
            )
            logger.info(f"Approved & sent: {pending['file_name']} → AI Models Reels topic {pending['topic_id']}")

            # 2. Margaret Asian → also send to Behave x Amin
            if pending.get("behave_target"):
                bt = pending["behave_target"]
                await app.send_document(
                    chat_id=int(bt["chat_id"]),
                    document=file_path,
                    file_name=pending["file_name"],
                    message_thread_id=bt["thread_id"],
                    force_document=True,
                )
                logger.info(f"Also sent to Behave x Amin thread {bt['thread_id']}")
        except Exception as e:
            logger.error(f"Error sending approved video: {e}")
        finally:
            if os.path.exists(file_path):
                os.unlink(file_path)


async def bulk_approve(model_names: list[str]) -> dict:
    """Approve every currently pending video for the given creators at once
    (same effect as clicking ✅ Approve on each), without editing the original
    approval-group messages (those buttons will just show 'session expired' if pressed later)."""
    tokens = [t for t, p in list(PENDING_APPROVALS.items()) if p["model_name"] in model_names]
    approved = []
    for token in tokens:
        pending = PENDING_APPROVALS.pop(token, None)
        if not pending:
            continue
        approval_log.set_resolved(token, True)
        await _send_approved_video(pending)
        approved.append({"model_name": pending["model_name"], "file_name": pending["file_name"]})
    return {"approved_count": len(approved), "approved": approved}


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
            file_path = video["path"]
            try:
                await app.send_document(
                    chat_id=int(target["chat_id"]),
                    document=file_path,
                    file_name=video["file_name"],
                    message_thread_id=target["thread_id"],
                    force_document=True,
                )
                logger.info(f"Sent {video['file_name']} → thread {target['thread_id']}")
            except Exception as e:
                logger.error(f"Error sending {video['file_name']}: {e}")
            finally:
                if os.path.exists(file_path):
                    os.unlink(file_path)


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


# ──────────────────────────────────────────────
# Strike system — daily deadlines (Approval-Bjarne, Activity-Bjarne/Ken/James)
# All strikes, regardless of person/reason, post into the one STRIKE_GROUP_CHAT_ID.
# ──────────────────────────────────────────────

def maybe_record_activity(message: dict) -> None:
    """If this message landed in one of the monitored groups AND was sent by
    Bjarne specifically, record it as his activity there. Other members of
    these groups (e.g. Ken/James/Sherry, who own or also post in some of them)
    are deliberately ignored — this tracks only whether Bjarne did his part."""
    chat_id = message.get("chat", {}).get("id")
    sender_id = message.get("from", {}).get("id")
    if chat_id in MONITORED_ACTIVITY_CHAT_IDS and sender_id == PERSON_TELEGRAM_IDS["bjarne"]:
        activity_log.record_activity(chat_id)


async def _notify_strike(person: str, entry: dict, extra_dm_detail: str = "") -> None:
    count = len(strikes.current_month_strikes(person))
    date_eu = strikes.to_eu_date(entry["date"])
    display_name = PERSON_DISPLAY_NAMES.get(person, person.capitalize())
    person_id = PERSON_TELEGRAM_IDS.get(person)
    count_line = f"Strikes diesen Monat: {count}/{STRIKE_MONTHLY_DISPLAY_MAX}"

    dm_text = f"🚨 Strike ({date_eu})\n{entry['reason']}"
    if extra_dm_detail:
        dm_text += f"\n{extra_dm_detail}"
    dm_text += f"\n\n{count_line}"

    group_text = f"🔴 Strike — {display_name} — {date_eu}\nGrund: {entry['reason']}\n{count_line}"

    async with httpx.AsyncClient(timeout=15) as client:
        if person_id:
            await client.post(
                f"{_telegram_api()}/sendMessage",
                json={"chat_id": person_id, "text": dm_text},
            )
        await client.post(
            f"{_telegram_api()}/sendMessage",
            json={"chat_id": STRIKE_GROUP_CHAT_ID, "text": group_text},
        )
    logger.info(f"Strike given to {display_name} ({entry['date']}): {entry['reason']} — month total {count}")


async def check_daily_approval_deadline() -> None:
    """Called once daily (~13:00 + buffer Berlin). If any video uploaded in the
    last 24h is still neither approved nor rejected, Bjarne gets one strike."""
    unresolved = approval_log.unresolved_created_in_last_24h()
    if not unresolved:
        return
    if strikes.has_strike_today("bjarne"):
        return  # already struck today (e.g. loop re-fired after a restart)

    reason = f"{len(unresolved)} Video(s) nicht bis 13:00 Uhr approved/rejected"
    entry = strikes.add_strike("bjarne", reason)
    files_list = "\n".join(f"• {e['model_name']} — {e['file_name']}" for e in unresolved)
    await _notify_strike("bjarne", entry, extra_dm_detail=files_list)


async def check_activity_deadline(person: str, chat_id: int, label: str, window_hours: int = 24) -> None:
    """Called once per due-day (~deadline + buffer Berlin) for an activity-based
    task (Bjarne posting into the Ken/James AI Reels Gen groups, the Trends
    research group, Sherry's list group). Strikes if Bjarne sent nothing into
    chat_id within the last `window_hours`."""
    if activity_log.has_activity_in_last_hours(chat_id, window_hours):
        return
    if strikes.has_strike_today(person):
        return

    entry = strikes.add_strike(person, f"{label} — nichts geschickt")
    await _notify_strike(person, entry)


async def handle_command(message: dict) -> None:
    """Handle plain-text bot commands (e.g. /removestrike), sent as DM or in the strike group."""
    text = (message.get("text") or "").strip()
    if not text.startswith("/"):
        return
    from_id = message.get("from", {}).get("id")
    chat_id = message.get("chat", {}).get("id")
    thread_id = message.get("message_thread_id")

    parts = text.split()
    command = parts[0].split("@")[0]  # strip @BotUsername suffix if present

    if command != "/removestrike":
        return

    async def reply(msg: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{_telegram_api()}/sendMessage",
                json={"chat_id": chat_id, "message_thread_id": thread_id, "text": msg},
            )

    if from_id != JEREMI_TELEGRAM_ID:
        await reply("❌ Nicht berechtigt.")
        return

    if len(parts) < 2 or parts[1].lower() not in PERSON_TELEGRAM_IDS:
        await reply("Nutzung: /removestrike <bjarne|ken|james> [TT.MM.JJ]")
        return

    person = parts[1].lower()
    date_arg_eu = parts[2] if len(parts) > 2 else None
    date_arg_iso = None
    if date_arg_eu:
        try:
            date_arg_iso = strikes.parse_eu_date(date_arg_eu)
        except ValueError:
            await reply(f"⚠️ Datum \"{date_arg_eu}\" nicht erkannt — bitte im Format TT.MM.JJ (z.B. 08.08.26).")
            return

    revoked = strikes.revoke_strike(person, date_arg_iso, revoked_by="Jeremi")
    if not revoked:
        await reply(
            f"⚠️ Kein offener Strike für {PERSON_DISPLAY_NAMES.get(person, person)} gefunden"
            + (f" am {date_arg_eu}" if date_arg_eu else " diesen Monat") + "."
        )
        return

    count = len(strikes.current_month_strikes(person))
    await reply(
        f"↩️ Strike vom {strikes.to_eu_date(revoked['date'])} für {PERSON_DISPLAY_NAMES.get(person, person)} zurückgezogen. "
        f"Strikes diesen Monat: {count}/{STRIKE_MONTHLY_DISPLAY_MAX}"
    )
