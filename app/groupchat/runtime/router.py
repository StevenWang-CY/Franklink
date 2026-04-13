from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.integrations.photon_client import PhotonClient
from app.groupchat.io.recorder import GroupChatRecorder
from app.groupchat.io.sender import GroupChatSender
from app.groupchat.runtime.deps import GroupChatRuntimeDeps
from app.groupchat.runtime.handlers import GroupChatHandler
from app.groupchat.runtime.types import GroupChatEvent, GroupChatManagedContext

logger = logging.getLogger(__name__)

_CHAT_GUID_LOG_LEN = 40
_MESSAGE_ID_LOG_LEN = 18
_EVENT_ID_LOG_LEN = 18


def _is_group_chat(chat_guid: str) -> bool:
    guid = str(chat_guid or "")
    return bool(guid) and (";+;" in guid or guid.startswith("chat"))


def _clip(value: str, max_len: int) -> str:
    s = str(value or "")
    return s if len(s) <= max_len else s[:max_len]


def _looks_like_frank_invocation(text: str) -> bool:
    msg = (text or "").strip().lower()
    if not msg:
        return False
    if msg.startswith("frank"):
        return True
    if msg.startswith("@frank"):
        return True
    if msg.startswith("hey frank") or msg.startswith("hi frank") or msg.startswith("yo frank"):
        return True
    return False

def _digits_last10(value: str) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def _normalize_handle(handle: str | None) -> tuple[str, str]:
    raw = str(handle or "").strip()
    if not raw:
        return ("", "")
    if "@" in raw:
        return (raw.lower(), "")
    return ("", _digits_last10(raw))


def _fallback_sender_name(sender_handle: str | None) -> str:
    handle = str(sender_handle or "").strip()
    if handle and "@" in handle:
        local = handle.split("@", 1)[0].strip().lower()
        local = re.sub(r"[^a-z0-9]+", " ", local).strip()
        token = (local.split(" ", 1)[0] if local else "").strip()
        return (token[:18] if token else "there")
    if handle:
        d = _digits_last10(handle)
        if d:
            return d[-4:]
    return "there"

def _mask_handle(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "@" in raw:
        local, domain = raw.split("@", 1)
        local = (local[:2] + "***") if local else "***"
        domain = domain[:32]
        return f"{local}@{domain}"
    digits = _digits_last10(raw)
    if digits:
        return f"***{digits[-4:]}"
    return "***"


def _compute_fallback_event_id(payload: Dict[str, Any]) -> str:
    blob = "|".join(
        [
            str(payload.get("chat_guid") or ""),
            str(payload.get("message_id") or ""),
            str(payload.get("from_number") or ""),
            str(payload.get("content") or ""),
            str(payload.get("media_url") or ""),
            str(payload.get("timestamp") or ""),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class GroupChatRouter:
    """
    Single entry point for all group chat inbound messages.

    Pipeline:
    - Determine if group chat
    - Load managed context
    - Record inbound raw message (idempotent)
    - Dispatch to handlers
    """

    def __init__(
        self,
        *,
        db: Optional[DatabaseClient] = None,
        photon: Optional[PhotonClient] = None,
        openai: Optional[AzureOpenAIClient] = None,
        handlers: Optional[List[GroupChatHandler]] = None,
    ):
        self.db = db or DatabaseClient()
        self.photon = photon or PhotonClient()
        self.openai = openai or AzureOpenAIClient()
        self.recorder = GroupChatRecorder(db=self.db)
        self.sender = GroupChatSender(photon=self.photon, recorder=self.recorder)
        if handlers is None:
            from app.groupchat.runtime.handlers.interaction_agent import InteractionAgentHandler
            from app.groupchat.runtime.handlers.opinion_v1 import OpinionV1Handler

            handlers = [InteractionAgentHandler(), OpinionV1Handler()]
        self.handlers = handlers

    @property
    def deps(self) -> GroupChatRuntimeDeps:
        return GroupChatRuntimeDeps(
            db=self.db,
            photon=self.photon,
            openai=self.openai,
            recorder=self.recorder,
            sender=self.sender,
        )

    async def handle_inbound(self, webhook: Any, *, sender_user_id: str) -> bool:
        chat_guid = str(getattr(webhook, "chat_guid", "") or "")
        if not chat_guid or not _is_group_chat(chat_guid):
            return False

        payload: Dict[str, Any] = {
            "chat_guid": chat_guid,
            "message_id": getattr(webhook, "message_id", None),
            "timestamp": getattr(webhook, "timestamp", None),
            "from_number": getattr(webhook, "from_number", None),
            "content": getattr(webhook, "content", None),
            "media_url": getattr(webhook, "media_url", None),
        }

        message_id = str(payload.get("message_id") or "") or None
        event_id = message_id or _compute_fallback_event_id(payload)

        logger.debug(
            "[GROUPCHAT][ROUTER] inbound_start chat=%s msg_id=%s event_id=%s sender=%s has_media=%s text_len=%d",
            _clip(chat_guid, _CHAT_GUID_LOG_LEN),
            _clip(message_id or "", _MESSAGE_ID_LOG_LEN),
            _clip(event_id, _EVENT_ID_LOG_LEN),
            _mask_handle(payload.get("from_number")),
            "yes" if (payload.get("media_url") or "") else "no",
            len(str(payload.get("content") or "") or ""),
        )

        event = GroupChatEvent(
            chat_guid=chat_guid,
            event_id=event_id,
            message_id=message_id,
            timestamp=str(payload.get("timestamp") or "") or None,
            sender_handle=str(payload.get("from_number") or "") or None,
            sender_user_id=str(sender_user_id or "") or None,
            sender_name=None,
            resolved_participant="unknown",
            text=str(payload.get("content") or "") or "",
            media_url=str(payload.get("media_url") or "") or None,
            raw_payload=payload,
        )

        managed = await self._load_managed_context(chat_guid=chat_guid)
        logger.debug(
            "[GROUPCHAT][ROUTER] managed_lookup chat=%s managed=%s user_a=%s user_b=%s",
            _clip(chat_guid, _CHAT_GUID_LOG_LEN),
            "yes" if managed else "no",
            _clip(getattr(managed, "user_a_id", "") or "", 8),
            _clip(getattr(managed, "user_b_id", "") or "", 8),
        )
        event = await self._enrich_event(event=event, managed=managed)
        logger.debug(
            "[GROUPCHAT][ROUTER] event_enriched chat=%s event_id=%s sender_user=%s participant=%s sender_name=%s",
            _clip(chat_guid, _CHAT_GUID_LOG_LEN),
            _clip(event.event_id, _EVENT_ID_LOG_LEN),
            _clip(str(event.sender_user_id or ""), 8),
            str(event.resolved_participant or "unknown"),
            _clip(str(event.sender_name or ""), 24),
        )

        # Record inbound raw message before any handler logic.
        should_record = bool(managed) or _looks_like_frank_invocation(event.text)
        if should_record:
            try:
                recorded = await self.recorder.record_inbound(event=event, is_managed=bool(managed))
                logger.debug(
                    "[GROUPCHAT][ROUTER] inbound_recorded chat=%s event_id=%s ok=%s",
                    _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                    _clip(event.event_id, _EVENT_ID_LOG_LEN),
                    "yes" if recorded else "no",
                )
            except Exception:
                pass
        else:
            logger.debug(
                "[GROUPCHAT][ROUTER] inbound_record_skipped chat=%s event_id=%s reason=%s",
                _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                _clip(event.event_id, _EVENT_ID_LOG_LEN),
                "unmanaged_and_not_invocation",
            )

        handled = False
        for handler in self.handlers:
            try:
                logger.debug(
                    "[GROUPCHAT][ROUTER] handler_try chat=%s event_id=%s handler=%s",
                    _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                    _clip(event.event_id, _EVENT_ID_LOG_LEN),
                    getattr(handler, "name", "?"),
                )
                if await handler.handle(event=event, managed=managed, deps=self.deps):
                    handled = True
                    logger.info(
                        "[GROUPCHAT][ROUTER] handler_claimed chat=%s event_id=%s handler=%s",
                        _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                        _clip(event.event_id, _EVENT_ID_LOG_LEN),
                        getattr(handler, "name", "?"),
                    )
                    break
            except Exception as e:
                logger.error("[GROUPCHAT][ROUTER] handler failed name=%s err=%s", getattr(handler, "name", "?"), e, exc_info=True)

        if not handled:
            logger.debug(
                "[GROUPCHAT][ROUTER] no_handler_claimed chat=%s event_id=%s managed=%s",
                _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                _clip(event.event_id, _EVENT_ID_LOG_LEN),
                "yes" if managed else "no",
            )

        # Even if no handler claims the event, it's still "handled" in the sense that we never DM-reply.
        return True if managed else handled

    async def _load_managed_context(self, *, chat_guid: str) -> Optional[GroupChatManagedContext]:
        try:
            chat = await self.db.get_group_chat_by_guid(chat_guid)
        except Exception:
            chat = None

        if not isinstance(chat, dict):
            return None

        return GroupChatManagedContext(
            chat_guid=str(chat_guid),
            user_a_id=str(chat.get("user_a_id") or ""),
            user_b_id=str(chat.get("user_b_id") or ""),
            user_a_mode=str(chat.get("user_a_mode") or "") or None,
            user_b_mode=str(chat.get("user_b_mode") or "") or None,
            connection_request_id=str(chat.get("connection_request_id") or "") or None,
        )

    async def _enrich_event(
        self,
        *,
        event: GroupChatEvent,
        managed: Optional[GroupChatManagedContext],
    ) -> GroupChatEvent:
        sender_name = event.sender_name
        resolved_participant = event.resolved_participant or "unknown"
        resolved_user_id = event.sender_user_id

        user_a_row = None
        user_b_row = None

        if managed and resolved_user_id not in {managed.user_a_id, managed.user_b_id}:
            # Best-effort mapping for "duplicate user rows" caused by handle/phone formatting mismatches.
            sender_email, sender_digits = _normalize_handle(event.sender_handle)
            if sender_email or sender_digits:
                try:
                    user_a_row = await self.db.get_user_by_id(managed.user_a_id)
                    user_b_row = await self.db.get_user_by_id(managed.user_b_id)
                except Exception:
                    user_a_row = None
                    user_b_row = None

                a_handle = str((user_a_row or {}).get("phone_number") or "")
                b_handle = str((user_b_row or {}).get("phone_number") or "")
                a_email, a_digits = _normalize_handle(a_handle)
                b_email, b_digits = _normalize_handle(b_handle)

                if sender_email and a_email and sender_email == a_email:
                    resolved_user_id = managed.user_a_id
                elif sender_email and b_email and sender_email == b_email:
                    resolved_user_id = managed.user_b_id
                elif sender_digits and a_digits and sender_digits == a_digits:
                    resolved_user_id = managed.user_a_id
                elif sender_digits and b_digits and sender_digits == b_digits:
                    resolved_user_id = managed.user_b_id

        if managed:
            if resolved_user_id == managed.user_a_id:
                resolved_participant = "user_a"
            elif resolved_user_id == managed.user_b_id:
                resolved_participant = "user_b"
            else:
                resolved_participant = "unknown"

        if not (sender_name or "").strip():
            # Prefer participant names for managed chats.
            if managed and resolved_participant == "user_a":
                if user_a_row is None:
                    try:
                        user_a_row = await self.db.get_user_by_id(managed.user_a_id)
                    except Exception:
                        user_a_row = None
                sender_name = str((user_a_row or {}).get("name") or "").strip() or "user a"
            elif managed and resolved_participant == "user_b":
                if user_b_row is None:
                    try:
                        user_b_row = await self.db.get_user_by_id(managed.user_b_id)
                    except Exception:
                        user_b_row = None
                sender_name = str((user_b_row or {}).get("name") or "").strip() or "user b"
            else:
                try:
                    user_row = await self.db.get_user_by_id(str(resolved_user_id or ""))
                    sender_name = str((user_row or {}).get("name") or "").strip() or None
                except Exception:
                    sender_name = None

        sender_name = (sender_name or "").strip() or _fallback_sender_name(event.sender_handle)

        return GroupChatEvent(
            chat_guid=event.chat_guid,
            event_id=event.event_id,
            message_id=event.message_id,
            timestamp=event.timestamp,
            sender_handle=event.sender_handle,
            sender_user_id=resolved_user_id,
            sender_name=sender_name,
            resolved_participant=resolved_participant,
            text=event.text,
            media_url=event.media_url,
            raw_payload=event.raw_payload,
        )
