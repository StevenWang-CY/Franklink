"""Internal database client implementation (group chats)."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.database.models import GroupChatMode

logger = logging.getLogger(__name__)


class _GroupChatMethods:
    async def create_group_chat_record(
        self,
        chat_guid: str,
        user_a_id: str,
        user_b_id: str,
        connection_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        try:
            chat_data = {
                "chat_guid": chat_guid,
                "user_a_id": user_a_id,
                "user_b_id": user_b_id,
                "user_a_mode": GroupChatMode.ACTIVE.value,
                "user_b_mode": GroupChatMode.ACTIVE.value,
                "connection_request_id": connection_request_id,
                "created_at": datetime.utcnow().isoformat(),
            }

            result = self.client.table("group_chats").insert(chat_data).execute()
            logger.info(f"Created group chat record: {chat_guid}")
            return result.data[0]

        except Exception as e:
            logger.error(f"Error creating group chat record: {e}", exc_info=True)
            raise

    async def get_group_chat_by_guid(self, chat_guid: str) -> Optional[Dict[str, Any]]:
        try:
            result = self.client.table("group_chats").select("*").eq("chat_guid", chat_guid).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting group chat: {e}", exc_info=True)
            return None

    async def get_group_chat_for_users(self, user_a_id: str, user_b_id: str) -> Optional[Dict[str, Any]]:
        try:
            result = self.client.table("group_chats").select("*").or_(
                f"and(user_a_id.eq.{user_a_id},user_b_id.eq.{user_b_id}),"
                f"and(user_a_id.eq.{user_b_id},user_b_id.eq.{user_a_id})"
            ).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting group chat for users: {e}", exc_info=True)
            return None

    # ========================================================================
    # Multi-person group chat participant methods
    # ========================================================================

    async def add_group_chat_participant(
        self,
        chat_guid: str,
        user_id: str,
        role: str = "member",
        mode: str = "active",
        connection_request_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Add a participant to a group chat.

        Args:
            chat_guid: Chat GUID
            user_id: User ID to add
            role: Participant role (initiator, member)
            mode: Participant mode (active, quiet, muted)
            connection_request_id: Optional associated connection request

        Returns:
            Created participant record or None
        """
        try:
            participant_data = {
                "chat_guid": chat_guid,
                "user_id": user_id,
                "role": role,
                "mode": mode,
                "connection_request_id": connection_request_id,
                "joined_at": datetime.utcnow().isoformat(),
                "created_at": datetime.utcnow().isoformat(),
            }

            result = self.client.table("group_chat_participants").insert(
                participant_data
            ).execute()

            logger.info(f"Added participant {user_id} to chat {chat_guid}")
            return result.data[0] if result.data else None

        except Exception as e:
            logger.error(f"Error adding group chat participant: {e}", exc_info=True)
            return None

    async def get_group_chat_participants(
        self,
        chat_guid: str,
    ) -> List[Dict[str, Any]]:
        """
        Get all participants for a group chat.

        Args:
            chat_guid: Chat GUID

        Returns:
            List of participant records
        """
        try:
            result = self.client.table("group_chat_participants").select(
                "*"
            ).eq(
                "chat_guid", chat_guid
            ).execute()

            return result.data if result.data else []

        except Exception as e:
            logger.error(f"Error getting group chat participants: {e}", exc_info=True)
            return []

    async def get_user_group_chats(
        self,
        user_id: str,
    ) -> List[Dict[str, Any]]:
        """
        Get all group chats a user is participating in.

        Args:
            user_id: User ID

        Returns:
            List of group chat records with participant info
        """
        try:
            result = self.client.table("group_chat_participants").select(
                "*, group_chats(*)"
            ).eq(
                "user_id", user_id
            ).execute()

            return result.data if result.data else []

        except Exception as e:
            logger.error(f"Error getting user group chats: {e}", exc_info=True)
            return []

    async def update_participant_mode(
        self,
        chat_guid: str,
        user_id: str,
        mode: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Update a participant's mode in a group chat.

        Args:
            chat_guid: Chat GUID
            user_id: User ID
            mode: New mode (active, quiet, muted)

        Returns:
            Updated participant record or None
        """
        try:
            result = self.client.table("group_chat_participants").update({
                "mode": mode,
            }).eq(
                "chat_guid", chat_guid
            ).eq(
                "user_id", user_id
            ).execute()

            if result.data:
                logger.info(f"Updated participant mode: {user_id} in {chat_guid} -> {mode}")
                return result.data[0]

            return None

        except Exception as e:
            logger.error(f"Error updating participant mode: {e}", exc_info=True)
            return None

