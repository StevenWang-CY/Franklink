"""Database client methods for user_locations table."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _UserLocationMethods:
    """Mixin for user location database operations."""

    async def upsert_user_location(
        self,
        user_id: str,
        latitude: float,
        longitude: float,
        findmy_handle: str,
        long_address: Optional[str] = None,
        short_address: Optional[str] = None,
        findmy_status: Optional[str] = None,
        findmy_last_updated: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create or update a user's location.

        Args:
            user_id: User UUID
            latitude: Latitude coordinate
            longitude: Longitude coordinate
            findmy_handle: The Find My handle (phone or email) that matched
            long_address: Full street address
            short_address: Abbreviated address
            findmy_status: Find My status (legacy, live, shallow)
            findmy_last_updated: When Find My last updated this location

        Returns:
            Upserted record dict or None on error
        """
        try:
            data = {
                "user_id": user_id,
                "latitude": latitude,
                "longitude": longitude,
                "findmy_handle": findmy_handle,
                "long_address": long_address,
                "short_address": short_address,
                "findmy_status": findmy_status,
                "findmy_last_updated": findmy_last_updated,
                "updated_at": datetime.utcnow().isoformat(),
            }

            result = (
                self.client.table("user_locations")
                .upsert(data, on_conflict="user_id")
                .execute()
            )

            if result.data:
                return result.data[0]
            return None

        except Exception as e:
            logger.error(f"Error upserting user location: {e}", exc_info=True)
            return None

    async def get_user_location(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get a user's stored location.

        Args:
            user_id: User UUID

        Returns:
            Location dict or None if not found
        """
        try:
            result = (
                self.client.table("user_locations")
                .select("*")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )

            if result.data:
                return result.data[0]
            return None

        except Exception as e:
            logger.error(f"Error getting user location: {e}", exc_info=True)
            return None

    async def get_all_user_locations(self) -> List[Dict[str, Any]]:
        """Get all stored user locations.

        Returns:
            List of location dicts
        """
        try:
            result = (
                self.client.table("user_locations")
                .select("*")
                .execute()
            )
            return result.data or []

        except Exception as e:
            logger.error(f"Error getting all user locations: {e}", exc_info=True)
            return []
