from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.groupchat.features.opinion import GroupChatOpinionService
from app.groupchat.runtime.deps import GroupChatRuntimeDeps
from app.groupchat.runtime.handlers import GroupChatHandler
from app.groupchat.runtime.types import GroupChatEvent, GroupChatManagedContext

logger = logging.getLogger(__name__)


@dataclass
class OpinionV1Handler(GroupChatHandler):
    """
    Temporary adapter that reuses the existing opinion workflow service.

    This will be replaced by a native handler implementation once the workflow
    is re-homed under app/groupchat/handlers/.
    """

    name: str = "opinion_v1"

    async def handle(
        self,
        *,
        event: GroupChatEvent,
        managed: Optional[GroupChatManagedContext],
        deps: GroupChatRuntimeDeps,
    ) -> bool:
        logger.debug(
            "[GROUPCHAT][HANDLER:%s] start chat=%s event_id=%s",
            self.name,
            str(event.chat_guid)[:40],
            str(event.event_id)[:18],
        )
        svc = GroupChatOpinionService(
            db=deps.db,
            photon=deps.photon,
            openai=deps.openai,
            sender=deps.sender,
        )
        handled = await svc.handle_inbound_group_message(
            chat_guid=event.chat_guid,
            sender_user_id=str(event.sender_user_id or ""),
            sender_phone=str(event.sender_handle or ""),
            message_text=str(event.text or ""),
        )
        logger.debug(
            "[GROUPCHAT][HANDLER:%s] done chat=%s event_id=%s handled=%s",
            self.name,
            str(event.chat_guid)[:40],
            str(event.event_id)[:18],
            handled,
        )
        return handled
