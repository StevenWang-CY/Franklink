"""Location service for calculating distances between users via Find My Friends.

Uses Photon's iCloud Find My Friends API to get user locations and
calculates distances using the Haversine formula.

Results are cached in Redis to avoid excessive API calls.
"""

import logging
import math
from typing import Any, Dict, List, Optional, Tuple

from app.utils.redis_client import redis_client

logger = logging.getLogger(__name__)

CACHE_KEY = "findmy:friend_locations"
CACHE_TTL = 600  # 10 minutes


async def get_friend_locations(photon: Any) -> List[Dict]:
    """Fetch friend locations from Find My. Caches in Redis for 10 min.

    Args:
        photon: PhotonClient instance

    Returns:
        List of location dicts. Empty list on any error (never blocks matching).
    """
    cached = redis_client.get_cached(CACHE_KEY)
    if cached is not None:
        return cached
    try:
        locations = await photon.refresh_find_my_friends()
        if locations:
            redis_client.set_cached(CACHE_KEY, locations, ttl=CACHE_TTL)
        return locations
    except Exception as e:
        logger.warning(f"[LOCATION] Failed to fetch friend locations: {e}")
        return []


def calculate_distance_miles(
    coord1: Tuple[float, float],
    coord2: Tuple[float, float],
) -> float:
    """Calculate distance between two coordinates using Haversine formula.

    Args:
        coord1: (latitude, longitude) of first point
        coord2: (latitude, longitude) of second point

    Returns:
        Distance in miles
    """
    R = 3958.8  # Earth radius in miles
    lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
    lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


def _normalize_handle(handle: str) -> str:
    """Normalize a phone number or email for matching against Find My handles.

    Phone numbers: strips non-digit chars except leading +
    Emails: lowercased and stripped

    Args:
        handle: Phone number or email address

    Returns:
        Normalized handle string, or empty string if invalid
    """
    if not handle:
        return ""
    handle = handle.strip()
    if "@" in handle:
        return handle.lower()
    # Phone number: keep leading + and digits only
    if handle.startswith("+"):
        return "+" + "".join(c for c in handle[1:] if c.isdigit())
    return "".join(c for c in handle if c.isdigit())


def find_location_by_handle(
    locations: List[Dict],
    phone_or_email: str,
) -> Optional[Dict]:
    """Find a specific user's location from the locations list.

    Matches by normalized handle (phone number or email).

    Args:
        locations: List of location dicts from Find My API
        phone_or_email: User's phone number or email

    Returns:
        Location dict if found, None otherwise
    """
    target = _normalize_handle(phone_or_email)
    if not target:
        return None
    for loc in locations:
        loc_handle = _normalize_handle(loc.get("handle", ""))
        if loc_handle == target:
            return loc
    return None


def format_distance(miles: float) -> str:
    """Format distance for human-readable display.

    Args:
        miles: Distance in miles

    Returns:
        Formatted string like "5.2 miles away" or "0.1 miles away"
    """
    if miles < 0.1:
        return "0.1 miles away"
    return f"{miles:.1f} miles away"


async def get_distance_between_users(
    photon: Any,
    initiator_phone: str,
    target_phone: str,
    cached_locations: Optional[List[Dict]] = None,
    initiator_email: Optional[str] = None,
    target_email: Optional[str] = None,
    initiator_user_id: Optional[str] = None,
    target_user_id: Optional[str] = None,
) -> Optional[str]:
    """Get formatted distance string between two users.

    Looks up both users in the Find My friends list, calculates distance,
    and returns a formatted string. Falls back to stored locations from the
    user_locations table if Find My lookup fails.

    Tries phone number first, then falls back to email if the Find My handle
    is email-based (e.g. iCloud email instead of phone number).

    Args:
        photon: PhotonClient instance
        initiator_phone: Initiator's phone number
        target_phone: Target's phone number
        cached_locations: Pre-fetched locations list (avoids re-fetching in loops)
        initiator_email: Initiator's email (fallback handle for Find My lookup)
        target_email: Target's email (fallback handle for Find My lookup)
        initiator_user_id: Initiator's user ID (for stored location fallback)
        target_user_id: Target's user ID (for stored location fallback)

    Returns:
        Formatted distance string, or None if location unavailable
    """
    locations = cached_locations if cached_locations is not None else await get_friend_locations(photon)
    if not locations:
        logger.info("[LOCATION] No locations available, skipping distance")
        return None

    # Log available handles for debugging
    available_handles = [loc.get("handle", "?") for loc in locations]
    logger.info(f"[LOCATION] Available handles in Find My: {available_handles}")

    initiator_loc = find_location_by_handle(locations, initiator_phone)
    if not initiator_loc and initiator_email:
        initiator_loc = find_location_by_handle(locations, initiator_email)

    target_loc = find_location_by_handle(locations, target_phone)
    if not target_loc and target_email:
        target_loc = find_location_by_handle(locations, target_email)

    if not initiator_loc or not target_loc:
        logger.info(
            f"[LOCATION] Missing location - initiator({'found' if initiator_loc else 'NOT found'}, "
            f"phone={initiator_phone}, email={initiator_email}) | "
            f"target({'found' if target_loc else 'NOT found'}, "
            f"phone={target_phone}, email={target_email})"
        )
        # Fallback: try stored locations from user_locations table
        return await _fallback_stored_distance(
            initiator_loc=initiator_loc,
            target_loc=target_loc,
            initiator_user_id=initiator_user_id,
            target_user_id=target_user_id,
        )

    coord1 = initiator_loc.get("coordinates")
    coord2 = target_loc.get("coordinates")
    if not coord1 or not coord2 or len(coord1) < 2 or len(coord2) < 2:
        return None
    # Filter out [0, 0] coordinates (null island — means location wasn't actually retrieved)
    if (coord1[0] == 0 and coord1[1] == 0) or (coord2[0] == 0 and coord2[1] == 0):
        logger.info("[LOCATION] Skipping: one or both coordinates are [0,0] (location not available)")
        return None

    miles = calculate_distance_miles(
        (coord1[0], coord1[1]),
        (coord2[0], coord2[1]),
    )
    result = format_distance(miles)
    logger.info(f"[LOCATION] Distance calculated: {result} ({miles:.1f} mi)")
    return result


async def _fallback_stored_distance(
    initiator_loc: Optional[Dict],
    target_loc: Optional[Dict],
    initiator_user_id: Optional[str] = None,
    target_user_id: Optional[str] = None,
) -> Optional[str]:
    """Fallback: calculate distance using stored locations from user_locations table.

    Called when Find My lookup fails for one or both users.

    Args:
        initiator_loc: Find My location dict for initiator (may be None)
        target_loc: Find My location dict for target (may be None)
        initiator_user_id: Initiator's user ID for DB lookup
        target_user_id: Target's user ID for DB lookup

    Returns:
        Formatted distance string, or None if stored locations unavailable
    """
    if not initiator_user_id or not target_user_id:
        return None

    try:
        from app.database.client import DatabaseClient
        db = DatabaseClient()

        # Get coordinates: use Find My if available, otherwise stored location
        coord1 = None
        if initiator_loc:
            c = initiator_loc.get("coordinates")
            if c and len(c) >= 2 and not (c[0] == 0 and c[1] == 0):
                coord1 = (c[0], c[1])

        if not coord1:
            stored = await db.get_user_location(initiator_user_id)
            if stored and stored.get("latitude") and stored.get("longitude"):
                lat, lon = stored["latitude"], stored["longitude"]
                if not (lat == 0 and lon == 0):
                    coord1 = (lat, lon)

        coord2 = None
        if target_loc:
            c = target_loc.get("coordinates")
            if c and len(c) >= 2 and not (c[0] == 0 and c[1] == 0):
                coord2 = (c[0], c[1])

        if not coord2:
            stored = await db.get_user_location(target_user_id)
            if stored and stored.get("latitude") and stored.get("longitude"):
                lat, lon = stored["latitude"], stored["longitude"]
                if not (lat == 0 and lon == 0):
                    coord2 = (lat, lon)

        if not coord1 or not coord2:
            logger.info("[LOCATION] Stored location fallback: still missing coordinates")
            return None

        miles = calculate_distance_miles(coord1, coord2)
        result = format_distance(miles)
        logger.info(f"[LOCATION] Distance from stored locations: {result} ({miles:.1f} mi)")
        return result

    except Exception as e:
        logger.warning(f"[LOCATION] Stored location fallback failed: {e}")
        return None
