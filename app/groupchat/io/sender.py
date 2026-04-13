from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.integrations.photon_client import PhotonClient
from app.groupchat.io.recorder import GroupChatRecorder

logger = logging.getLogger(__name__)

_CHAT_GUID_LOG_LEN = 40
_MESSAGE_ID_LOG_LEN = 18


def _clip(value: str, max_len: int) -> str:
    s = str(value or "")
    return s if len(s) <= max_len else s[:max_len]


@dataclass
class GroupChatSender:
    """
    Centralized group chat sender that always records outbound messages.
    """

    photon: PhotonClient
    recorder: GroupChatRecorder

    async def send_and_record(
        self,
        *,
        chat_guid: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        effect_id: Optional[str] = None,
        subject: Optional[str] = None,
    ) -> Dict[str, Any]:
        logger.debug(
            "[GROUPCHAT][SENDER] send_start chat=%s text_len=%d",
            _clip(chat_guid, _CHAT_GUID_LOG_LEN),
            len((content or "")),
        )

        try:
            result: Dict[str, Any] = await self.photon.send_message_to_chat(
                chat_guid,
                content,
                effect_id=effect_id,
                subject=subject,
            )
        except Exception as e:
            logger.error(
                "[GROUPCHAT][SENDER] send_failed chat=%s err=%s",
                _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                e,
                exc_info=True,
            )
            raise

        message_id: Optional[str] = None
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict):
            message_id = (
                data.get("guid")
                or data.get("messageGuid")
                or data.get("message_id")
                or data.get("id")
            )

        logger.debug(
            "[GROUPCHAT][SENDER] send_ok chat=%s msg_id=%s",
            _clip(chat_guid, _CHAT_GUID_LOG_LEN),
            _clip(message_id or "", _MESSAGE_ID_LOG_LEN),
        )

        try:
            ok = await self.recorder.record_outbound(
                chat_guid=chat_guid,
                content=content,
                message_id=str(message_id) if message_id else None,
                metadata=metadata,
            )
            logger.debug(
                "[GROUPCHAT][SENDER] outbound_recorded chat=%s msg_id=%s ok=%s",
                _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                _clip(message_id or "", _MESSAGE_ID_LOG_LEN),
                "yes" if ok else "no",
            )
        except Exception as e:
            logger.warning(
                "[GROUPCHAT][SENDER] outbound_record_failed chat=%s msg_id=%s err=%s",
                _clip(chat_guid, _CHAT_GUID_LOG_LEN),
                _clip(message_id or "", _MESSAGE_ID_LOG_LEN),
                e,
                exc_info=True,
            )

        return result
