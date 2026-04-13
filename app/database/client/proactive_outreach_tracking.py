"""Database client methods for proactive_outreach_tracking table."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _ProactiveOutreachTrackingMethods:
    """Mixin for proactive outreach tracking operations."""

    async def create_proactive_outreach_tracking_v1(
        self,
        *,
        user_id: str,
        signal_id: Optional[str] = None,
        signal_text: str,
        target_user_id: str,
        connection_request_id: str,
        outreach_type: str = "email_derived",
        message_sent: Optional[str] = None,
        signal_group_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Record a new proactive outreach.

        Args:
            user_id: User ID who received the outreach
            signal_id: Optional signal ID that triggered this outreach
            signal_text: The signal text (stored for semantic comparison)
            target_user_id: User ID being suggested as a connection
            connection_request_id: Connection request created for this outreach
            outreach_type: Type of outreach (email_derived, manual, scheduled)
            message_sent: The message sent to the user
            signal_group_id: Optional group ID for multi-match signals

        Returns:
            Created tracking row or None
        """
        try:
            result = self.client.rpc(
                "create_proactive_outreach_tracking_v2",
                {
                    "p_user_id": str(user_id),
                    "p_signal_id": str(signal_id) if signal_id else None,
                    "p_signal_text": str(signal_text),
                    "p_target_user_id": str(target_user_id),
                    "p_connection_request_id": str(connection_request_id),
                    "p_outreach_type": str(outreach_type or "email_derived"),
                    "p_message_sent": str(message_sent) if message_sent else None,
                    "p_signal_group_id": str(signal_group_id) if signal_group_id else None,
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error creating proactive outreach tracking: {e}", exc_info=True)
            return None

    async def get_recent_outreach_texts_v1(
        self,
        *,
        user_id: str,
        since: str,
    ) -> List[Dict[str, Any]]:
        """
        Get recent outreach signal texts for semantic duplicate checking.

        Args:
            user_id: User ID
            since: ISO timestamp - only get outreach after this time

        Returns:
            List of dicts with signal_text field
        """
        try:
            result = self.client.rpc(
                "get_recent_outreach_texts_v1",
                {
                    "p_user_id": str(user_id),
                    "p_since": str(since),
                },
            ).execute()
            if isinstance(result.data, list):
                return result.data
            return []
        except Exception as e:
            logger.error(f"Error getting recent outreach texts: {e}", exc_info=True)
            return []

    async def get_recent_outreach_by_demand_hash_v1(
        self,
        *,
        user_id: str,
        demand_text_hash: str,
        since: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Check if we've reached out about a similar demand recently.

        Args:
            user_id: User ID
            demand_text_hash: SHA256 hash of normalized demand text
            since: ISO timestamp - only check outreach after this time

        Returns:
            Most recent matching outreach or None
        """
        try:
            result = self.client.rpc(
                "get_recent_outreach_by_demand_hash_v1",
                {
                    "p_user_id": str(user_id),
                    "p_demand_text_hash": str(demand_text_hash),
                    "p_since": str(since),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error getting recent outreach by demand hash: {e}", exc_info=True)
            return None

    async def get_recent_outreach_by_target_v1(
        self,
        *,
        user_id: str,
        target_user_id: str,
        since: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Check if we've suggested this target user recently.

        Args:
            user_id: User ID
            target_user_id: Target user ID being suggested
            since: ISO timestamp - only check outreach after this time

        Returns:
            Most recent matching outreach or None
        """
        try:
            result = self.client.rpc(
                "get_recent_outreach_by_target_v1",
                {
                    "p_user_id": str(user_id),
                    "p_target_user_id": str(target_user_id),
                    "p_since": str(since),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error getting recent outreach by target: {e}", exc_info=True)
            return None

    async def update_outreach_outcome_v1(
        self,
        *,
        outreach_id: str,
        outcome: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Update the outcome of an outreach.

        Args:
            outreach_id: Outreach tracking ID
            outcome: New outcome (pending, confirmed, declined, no_response, expired)

        Returns:
            Updated tracking row or None
        """
        try:
            result = self.client.rpc(
                "update_outreach_outcome_v1",
                {
                    "p_outreach_id": str(outreach_id),
                    "p_outcome": str(outcome),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error updating outreach outcome: {e}", exc_info=True)
            return None

    async def update_outreach_outcome_by_connection_request_v1(
        self,
        *,
        connection_request_id: str,
        outcome: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Update outcome by connection request ID (for when user responds).

        Args:
            connection_request_id: Connection request ID
            outcome: New outcome (pending, confirmed, declined, no_response, expired)

        Returns:
            Updated tracking row or None
        """
        try:
            result = self.client.rpc(
                "update_outreach_outcome_by_connection_request_v1",
                {
                    "p_connection_request_id": str(connection_request_id),
                    "p_outcome": str(outcome),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error updating outreach outcome by connection request: {e}", exc_info=True)
            return None
