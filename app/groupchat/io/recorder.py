from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from app.config import settings
from app.database.client import DatabaseClient
from app.groupchat.followup.utils import resolve_inactivity_minutes
from app.groupchat.runtime.types import GroupChatEvent

logger = logging.getLogger(__name__)

_CHAT_GUID_LOG_LEN = 40
_EVENT_ID_LOG_LEN = 18


def _clip(value: str, max_len: int) -> str:
    s = str(value or "")
    return s if len(s) <= max_len else s[:max_len]


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _best_effort_event_id(event: GroupChatEvent) -> str:
    """
    Prefer the router-provided event_id, which is the canonical identifier used
    for job anchors and idempotency across the groupchat pipeline.
    """
    if str(event.event_id or "").strip():
        return str(event.event_id)
    if str(event.message_id or "").strip():
        return str(event.message_id)

    blob = "|".join(
        [
            str(event.chat_guid or ""),
            str(event.sender_handle or ""),
            str(event.sender_user_id or ""),
            str(event.text or ""),
            str(event.media_url or ""),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class GroupChatRecorder:
    """
    Centralized raw-history recorder for group chats.

    Stores the raw transcript in Supabase (one row per chat tail) and schedules
    summary jobs for managed chats (atomic ingest).
    """

    db: DatabaseClient

    RAW_KEEP_LAST_N: int = 800

    async def record_inbound(
        self,
        *,
        event: GroupChatEvent,
        is_managed: bool,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not (event.chat_guid or "").strip():
            return False

        event_id = _best_effort_event_id(event)

        content = (event.text or "").strip()
        if not content:
            content = "[attachment]" if (event.media_url or "").strip() else "[empty]"

        sender_user_id = str(event.sender_user_id or "").strip() or None
        sender_handle = str(event.sender_handle or "").strip() or None
        sent_at = str(event.timestamp or "").strip() or _now_iso()

        msg_type = "user_message"
        if extra_metadata and isinstance(extra_metadata.get("type"), str):
            msg_type = str(extra_metadata.get("type") or "").strip() or msg_type

        try:
            # Always ingest inbound user messages through the atomic RPC:
            # it appends raw transcript AND debounces/schedules a summary job.
            out = await self.db.ingest_group_chat_user_message_and_schedule_summary_v1(
                chat_guid=event.chat_guid,
                event_id=event_id,
                message_id=event.message_id,
                sender_user_id=sender_user_id,
                sender_handle=sender_handle,
                sent_at=sent_at,
                content=content,
                media_url=event.media_url,
                inactivity_minutes=int(settings.groupchat_summary_inactivity_minutes),
                keep_last_n=self.RAW_KEEP_LAST_N,
            )
            ok = out is not None

            logger.debug(
                "[GROUPCHAT][RECORDER] inbound_write chat=%s event_id=%s ok=%s managed=%s",
                _clip(event.chat_guid, _CHAT_GUID_LOG_LEN),
                _clip(event_id, _EVENT_ID_LOG_LEN),
                "yes" if ok else "no",
                "yes" if is_managed else "no",
            )

            if ok and is_managed and getattr(settings, "groupchat_followup_enabled", False):
                try:
                    await self.db.schedule_group_chat_followup_job_v1(
                        chat_guid=event.chat_guid,
                        last_user_message_at=sent_at,
                        last_user_event_id=event_id,
                        inactivity_minutes=resolve_inactivity_minutes(),
                    )
                except Exception as e:
                    logger.warning(
                        "[GROUPCHAT][RECORDER] followup_schedule_failed chat=%s event_id=%s err=%s",
                        _clip(event.chat_guid, _CHAT_GUID_LOG_LEN),
                        _clip(event_id, _EVENT_ID_LOG_LEN),
                        e,
                    )
            return ok
        except Exception as e:
            logger.warning(
                "[GROUPCHAT][RECORDER] inbound_write_failed chat=%s event_id=%s err=%s",
                _clip(event.chat_guid, _CHAT_GUID_LOG_LEN),
                _clip(event_id, _EVENT_ID_LOG_LEN),
                e,
                exc_info=True,
            )
            return False

    async def record_outbound(
        self,
        *,
        chat_guid: str,
        content: str,
        message_id: Optional[str] = None,
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not (chat_guid or "").strip() or not (content or "").strip():
            return False

        sent_at = str(timestamp or "").strip() or _now_iso()
        event_id = message_id or hashlib.sha256(f"{chat_guid}|{content}|{sent_at}".encode("utf-8")).hexdigest()

        msg_type = "assistant_message"
        if metadata and isinstance(metadata.get("type"), str):
            msg_type = str(metadata.get("type") or "").strip() or msg_type

        try:
            out = await self.db.append_group_chat_raw_message_v1(
                chat_guid=chat_guid,
                event_id=event_id,
                message_id=message_id,
                role="assistant",
                sender_user_id=None,
                sender_handle=None,
                sent_at=sent_at,
                content=content,
                media_url=None,
                msg_type=msg_type,
                keep_last_n=self.RAW_KEEP_LAST_N,
            )
            ok = out is not None
            logger.debug(
                "[GROUPCHAT][RECORDER] outbound_write chat=%s event_id=%s ok=%s",
                _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                _clip(event_id, _EVENT_ID_LOG_LEN),
                "yes" if ok else "no",
            )
            return ok
        except Exception as e:
            logger.warning(
                "[GROUPCHAT][RECORDER] outbound_write_failed chat=%s event_id=%s err=%s",
                _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                _clip(event_id, _EVENT_ID_LOG_LEN),
                e,
                exc_info=True,
            )
            return False
