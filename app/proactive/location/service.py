"""Location update service.

Fetches all friend locations from Find My via Photon API,
matches each location to a user in the database, and upserts
the data into the user_locations table.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.database.client import DatabaseClient
from app.integrations.photon_client import PhotonClient
from app.utils.location_service import _normalize_handle

logger = logging.getLogger(__name__)


class LocationUpdateService:
    """Batch service that refreshes all user locations from Find My."""

    def __init__(self, db: DatabaseClient, photon: PhotonClient):
        self.db = db
        self.photon = photon

    async def run_once(self) -> int:
        """Fetch all locations, match to users, and upsert.

        Returns:
            Number of user locations updated.
        """
        # 1. Fetch all friend locations from Find My (single API call)
        locations = await self.photon.refresh_find_my_friends()
        if not locations:
            logger.info("[LOCATION_WORKER] No locations returned from Find My")
            return 0

        logger.info("[LOCATION_WORKER] Fetched %d locations from Find My", len(locations))

        # 2. Build handle -> location lookup for O(1) matching
        handle_map: Dict[str, Dict[str, Any]] = {}
        for loc in locations:
            handle = _normalize_handle(loc.get("handle", ""))
            if not handle:
                continue
            coords = loc.get("coordinates")
            if not coords or len(coords) < 2:
                continue
            # Skip null island ([0, 0])
            if coords[0] == 0 and coords[1] == 0:
                continue
            handle_map[handle] = loc

        if not handle_map:
            logger.info("[LOCATION_WORKER] No valid locations after filtering")
            return 0

        # 3. Get all onboarded users
        try:
            result = (
                self.db.client.table("users")
                .select("id, phone_number, email")
                .eq("is_onboarded", True)
                .execute()
            )
            users = result.data or []
        except Exception as e:
            logger.error("[LOCATION_WORKER] Failed to fetch users: %s", e, exc_info=True)
            return 0

        if not users:
            logger.info("[LOCATION_WORKER] No onboarded users found")
            return 0

        logger.info("[LOCATION_WORKER] Matching %d users against %d locations", len(users), len(handle_map))

        # 4. Match each user and upsert
        updated = 0
        for user in users:
            user_id = user.get("id")
            if not user_id:
                continue

            # Try phone first, then email fallback
            loc = None
            matched_handle = None

            phone = _normalize_handle(user.get("phone_number", ""))
            if phone and phone in handle_map:
                loc = handle_map[phone]
                matched_handle = phone

            if not loc:
                email = _normalize_handle(user.get("email", ""))
                if email and email in handle_map:
                    loc = handle_map[email]
                    matched_handle = email

            # Third fallback: check manually linked handles
            if not loc:
                try:
                    linked_handles = await self.db.get_linked_handles(user_id)
                    for linked in linked_handles:
                        normalized = _normalize_handle(linked)
                        if normalized and normalized in handle_map:
                            loc = handle_map[normalized]
                            matched_handle = normalized
                            break
                except Exception as e:
                    logger.warning("[LOCATION_WORKER] Failed to check linked handles for %s: %s", user_id[:8], e)

            if not loc or not matched_handle:
                continue

            coords = loc.get("coordinates", [])

            # Convert millisecond Unix timestamp to ISO datetime
            last_updated_iso = None
            raw_ts = loc.get("last_updated")
            if raw_ts:
                try:
                    ts_seconds = int(raw_ts) / 1000.0
                    last_updated_iso = datetime.fromtimestamp(ts_seconds, tz=timezone.utc).isoformat()
                except (ValueError, TypeError, OSError):
                    pass

            result = await self.db.upsert_user_location(
                user_id=user_id,
                latitude=coords[0],
                longitude=coords[1],
                findmy_handle=matched_handle,
                long_address=loc.get("long_address"),
                short_address=loc.get("short_address"),
                findmy_status=loc.get("status"),
                findmy_last_updated=last_updated_iso,
            )
            if result:
                updated += 1

        logger.info("[LOCATION_WORKER] Updated %d/%d user locations", updated, len(users))
        return updated
