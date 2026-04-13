from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

from app.groupchat.runtime.deps import GroupChatRuntimeDeps
from app.groupchat.runtime.handlers import GroupChatHandler
from app.groupchat.runtime.types import GroupChatEvent, GroupChatManagedContext
from app.agents.interaction.agent import InteractionAgent

logger = logging.getLogger(__name__)


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


def _strip_invocation(text: str) -> str:
    msg = (text or "").strip()
    if not msg:
        return msg
    patterns = [
        r"^@frank\b[:,]?\s*",
        r"^frank\b[:,]?\s*",
        r"^(hey|hi|yo)\s+frank\b[:,]?\s*",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", msg, flags=re.IGNORECASE)
        if cleaned != msg:
            return cleaned.strip()
    return msg


@dataclass
class InteractionAgentHandler(GroupChatHandler):
    name: str = "interaction_agent"

    async def handle(
        self,
        *,
        event: GroupChatEvent,
        managed: Optional[GroupChatManagedContext],
        deps: GroupChatRuntimeDeps,
    ) -> bool:
        if not _looks_like_frank_invocation(event.text):
            return False

        message_text = _strip_invocation(event.text)
        if not message_text:
            message_text = "hey"

        user = None
        if event.sender_user_id:
            user = await deps.db.get_user_by_id(str(event.sender_user_id))
        if not user:
            user = await deps.db.get_or_create_user(str(event.sender_handle or ""))

        agent = InteractionAgent(
            db=deps.db,
            photon=deps.photon,
            openai=deps.openai,
        )

        webhook = SimpleNamespace(
            content=message_text,
            from_number=event.sender_handle,
            message_id=event.message_id or event.event_id,
            timestamp=event.timestamp,
            media_url=event.media_url,
            chat_guid=event.chat_guid,
        )

        result = await agent.process_message(
            phone_number=webhook.from_number,
            message_content=webhook.content,
            user=user,
            webhook_data={
                "message_id": webhook.message_id,
                "timestamp": webhook.timestamp,
                "media_url": webhook.media_url,
                "chat_guid": webhook.chat_guid,
            },
        )
        if not result.get("success"):
            logger.debug("[GROUPCHAT][INTERACTION] no response generated")
            return False

        responses = result.get("responses") if isinstance(result.get("responses"), list) else []
        if not responses and result.get("response_text"):
            responses = [
                {
                    "response_text": result.get("response_text"),
                    "intent": result.get("intent"),
                    "task": result.get("intent"),
                }
            ]

        sent_any = False
        for response in responses:
            text = str(response.get("response_text") or "").strip()
            if not text:
                continue
            await deps.sender.send_and_record(
                chat_guid=event.chat_guid,
                content=text,
                metadata={
                    "task": response.get("task"),
                    "intent": response.get("intent"),
                    "handler": self.name,
                },
            )
            sent_any = True

        return sent_any
