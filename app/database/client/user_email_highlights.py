"""Internal database client implementation (user_email_highlights)."""

import logging
from datetime import datetime
from typing import Any, Dict, List

from postgrest.exceptions import APIError

logger = logging.getLogger(__name__)


class _UserEmailHighlightMethods:
    async def store_user_email_highlights(self, user_id: str, highlights: List[Dict[str, Any]]) -> int:
        """
        Store processed email highlights, skipping duplicates by message_id.
        Returns count of highlights stored.
        """
        if not highlights:
            return 0

        try:
            existing = (
                self.client.table("user_email_highlights")
                .select("message_id")
                .eq("user_id", user_id)
                .execute()
            )
            existing_ids = {r.get("message_id") for r in (existing.data or []) if r.get("message_id")}

            rows = []
            for highlight in highlights:
                message_id = highlight.get("message_id")
                if message_id and message_id in existing_ids:
                    continue

                rows.append(
                    {
                        "user_id": user_id,
                        "message_id": message_id,
                        "direction": highlight.get("direction"),
                        "is_from_me": bool(highlight.get("is_from_me")),
                        "sender": highlight.get("sender"),
                        "sender_domain": highlight.get("sender_domain"),
                        "subject": highlight.get("subject"),
                        "body_excerpt": highlight.get("body_excerpt"),
                        "received_at": highlight.get("received_at"),
                        "fetched_at": highlight.get("fetched_at"),
                        "created_at": datetime.utcnow().isoformat(),
                    }
                )

            if not rows:
                logger.info("No new email highlights to store for user %s", user_id)
                return 0

            result = (
                self.client.table("user_email_highlights")
                .insert(rows)
                .execute()
            )
            stored_count = len(result.data) if result.data else 0
            logger.info("Stored %s email highlights for user %s", stored_count, user_id)
            return stored_count

        except APIError as e:
            logger.error("API error storing email highlights: %s", str(e), exc_info=True)
            raise
        except Exception as e:
            logger.error("Error storing email highlights: %s", str(e), exc_info=True)
            raise

    async def get_user_email_highlights(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get email highlights for a user, ordered by created_at desc.
        """
        try:
            result = (
                self.client.table("user_email_highlights")
                .select(
                    "message_id,direction,is_from_me,sender,sender_domain,subject,body_excerpt,received_at,fetched_at,created_at"
                )
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return list(result.data or [])
        except APIError as e:
            logger.error("API error fetching email highlights: %s", str(e), exc_info=True)
            return []
        except Exception as e:
            logger.error("Error fetching email highlights: %s", str(e), exc_info=True)
            return []

    async def get_unsynced_highlights_for_zep(
        self,
        user_id: str,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """
        Get email highlights that haven't been synced to Zep yet.

        Args:
            user_id: User ID
            limit: Max highlights to return

        Returns:
            List of highlights where zep_synced_at IS NULL
        """
        try:
            result = (
                self.client.table("user_email_highlights")
                .select("id,message_id,direction,is_from_me,sender,sender_domain,subject,body_excerpt,received_at,fetched_at")
                .eq("user_id", user_id)
                .is_("zep_synced_at", "null")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return list(result.data or [])
        except APIError as e:
            logger.error("API error fetching unsynced highlights: %s", str(e), exc_info=True)
            return []
        except Exception as e:
            logger.error("Error fetching unsynced highlights: %s", str(e), exc_info=True)
            return []

    async def mark_highlights_zep_synced(
        self,
        highlight_ids: List[str],
    ) -> int:
        """
        Mark highlights as synced to Zep by setting zep_synced_at timestamp.

        Args:
            highlight_ids: List of highlight UUIDs to mark as synced

        Returns:
            Number of highlights updated
        """
        if not highlight_ids:
            return 0

        try:
            now = datetime.utcnow().isoformat()
            result = (
                self.client.table("user_email_highlights")
                .update({"zep_synced_at": now})
                .in_("id", highlight_ids)
                .execute()
            )
            updated = len(result.data) if result.data else 0
            logger.info("Marked %d highlights as synced to Zep", updated)
            return updated
        except APIError as e:
            logger.error("API error marking highlights as synced: %s", str(e), exc_info=True)
            return 0
        except Exception as e:
            logger.error("Error marking highlights as synced: %s", str(e), exc_info=True)
            return 0
