"""Database client methods for user_email_signals table."""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _UserEmailSignalMethods:
    """Mixin for user email signal operations."""

    async def upsert_user_email_signals_v1(
        self,
        *,
        user_id: str,
        signals: List[Dict[str, Any]],
    ) -> int:
        """
        Insert or update signals for a user, replacing any existing active signals.

        Args:
            user_id: User ID
            signals: List of signal dicts with keys:
                - signal_text: str
                - signal_rank: int (1, 2, or 3)
                - urgency_score: float (optional, default 0.5)
                - relevance_score: float (optional, default 0.5)
                - source_intent_event_ids: List[str] (optional)
                - extraction_reasoning: str (optional)
                - match_type: str ('single' or 'multi', default 'single')
                - max_matches: int (default 1)

        Returns:
            Number of signals upserted
        """
        try:
            result = self.client.rpc(
                "upsert_user_email_signals_v1",
                {
                    "p_user_id": str(user_id),
                    "p_signals": json.dumps(signals),
                },
            ).execute()
            if isinstance(result.data, int):
                return result.data
            if isinstance(result.data, list) and result.data:
                return int(result.data[0]) if result.data[0] else 0
            return 0
        except Exception as e:
            logger.error(f"Error upserting user email signals: {e}", exc_info=True)
            return 0

    async def get_active_user_email_signals_v1(
        self,
        *,
        user_id: str,
    ) -> List[Dict[str, Any]]:
        """
        Get all active (non-expired) signals for a user, ordered by rank.

        Args:
            user_id: User ID

        Returns:
            List of active signal rows
        """
        try:
            result = self.client.rpc(
                "get_active_user_email_signals_v1",
                {"p_user_id": str(user_id)},
            ).execute()
            return result.data if isinstance(result.data, list) else []
        except Exception as e:
            logger.error(f"Error getting active user email signals: {e}", exc_info=True)
            return []

    async def update_signal_status_v1(
        self,
        *,
        signal_id: str,
        status: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Update the status of a specific signal.

        Args:
            signal_id: Signal ID
            status: New status (active, matched, expired, dismissed)

        Returns:
            Updated signal row or None
        """
        try:
            result = self.client.rpc(
                "update_signal_status_v1",
                {
                    "p_signal_id": str(signal_id),
                    "p_status": str(status),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error updating signal status: {e}", exc_info=True)
            return None

    # Backwards compatibility aliases
    async def upsert_user_email_demands_v1(self, *, user_id: str, demands: List[Dict[str, Any]]) -> int:
        """Backwards compatibility alias for upsert_user_email_signals_v1."""
        return await self.upsert_user_email_signals_v1(user_id=user_id, signals=demands)

    async def get_active_user_email_demands_v1(self, *, user_id: str) -> List[Dict[str, Any]]:
        """Backwards compatibility alias for get_active_user_email_signals_v1."""
        return await self.get_active_user_email_signals_v1(user_id=user_id)

    async def update_demand_status_v1(self, *, demand_id: str, status: str) -> Optional[Dict[str, Any]]:
        """Backwards compatibility alias for update_signal_status_v1."""
        return await self.update_signal_status_v1(signal_id=demand_id, status=status)
