"""Internal database client implementation (conversations)."""

import json
import logging
from typing import Any, Dict, List, Optional

from app.database.models import Conversation, MessageType

logger = logging.getLogger(__name__)


class _ConversationMethods:
    async def store_message(
        self,
        user_id: str,
        content: str,
        message_type: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Store a conversation message.
        """
        try:
            conversation = Conversation(
                user_id=user_id,
                content=content,
                message_type=MessageType(message_type),
                metadata=metadata or {},
            )

            conv_dict = json.loads(conversation.model_dump_json(exclude_none=True))
            result = self.client.table("conversations").insert(conv_dict).execute()

            logger.info(f"Stored {message_type} message for user {user_id}")
            return result.data[0]

        except Exception as e:
            logger.error(f"Error storing message: {str(e)}", exc_info=True)
            raise

    async def get_recent_messages(
        self,
        user_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Get recent conversation messages for a user.

        Args:
            user_id: User's UUID
            limit: Maximum number of messages to return (default 10)

        Returns:
            List of message dicts with 'role' and 'content' keys,
            ordered from oldest to newest
        """
        try:
            result = (
                self.client.table("conversations")
                .select("content, message_type, created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )

            # Convert to standard format and reverse to get chronological order
            messages = []
            for row in reversed(result.data):
                role = "user" if row["message_type"] == "user" else "assistant"
                messages.append({
                    "role": role,
                    "content": row["content"],
                })

            logger.debug(f"Fetched {len(messages)} recent messages for user {user_id}")
            return messages

        except Exception as e:
            logger.error(f"Error fetching recent messages: {str(e)}", exc_info=True)
            return []

