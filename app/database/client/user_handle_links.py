"""Database client methods for user_handle_links table."""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _UserHandleLinkMethods:
    """Mixin for user handle link database operations."""

    async def link_handle_to_user(
        self,
        user_id: str,
        handle: str,
        handle_type: str = "findmy",
    ) -> Optional[Dict[str, Any]]:
        """Link a Find My handle to a user account.

        Args:
            user_id: User UUID
            handle: The handle to link (phone, email, iCloud email, etc.)
            handle_type: Type of handle (default: 'findmy')

        Returns:
            Upserted record dict or None on error
        """
        try:
            data = {
                "user_id": user_id,
                "handle": handle,
                "handle_type": handle_type,
            }
            result = (
                self.client.table("user_handle_links")
                .upsert(data, on_conflict="user_id,handle")
                .execute()
            )
            if result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error linking handle to user: {e}", exc_info=True)
            return None

    async def get_linked_handles(self, user_id: str) -> List[str]:
        """Get all linked handles for a user.

        Args:
            user_id: User UUID

        Returns:
            List of handle strings
        """
        try:
            result = (
                self.client.table("user_handle_links")
                .select("handle")
                .eq("user_id", user_id)
                .execute()
            )
            return [row["handle"] for row in (result.data or [])]
        except Exception as e:
            logger.error(f"Error getting linked handles: {e}", exc_info=True)
            return []

    async def find_user_by_linked_handle(self, handle: str) -> Optional[str]:
        """Find a user ID by a linked handle.

        Args:
            handle: The handle to look up

        Returns:
            User ID string or None if not found
        """
        try:
            result = (
                self.client.table("user_handle_links")
                .select("user_id")
                .eq("handle", handle)
                .limit(1)
                .execute()
            )
            if result.data:
                return result.data[0]["user_id"]
            return None
        except Exception as e:
            logger.error(f"Error finding user by linked handle: {e}", exc_info=True)
            return None

    async def get_all_linked_handles(self) -> List[Dict[str, Any]]:
        """Get all handle links (for worker batch lookups).

        Returns:
            List of dicts with user_id and handle
        """
        try:
            result = (
                self.client.table("user_handle_links")
                .select("user_id, handle")
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error getting all linked handles: {e}", exc_info=True)
            return []
