"""Duplicate outreach detection.

Prevents reaching out about similar signals or same targets within cooldown period.
Uses semantic similarity (Jaccard + SequenceMatcher) instead of hash-based comparison.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database.client import DatabaseClient
from app.proactive.config import PROACTIVE_OUTREACH_COOLDOWN_DAYS
from app.proactive.outreach.semantic_dedup import is_semantic_duplicate

logger = logging.getLogger(__name__)


async def check_duplicate_outreach(
    db: DatabaseClient,
    *,
    user_id: str,
    signal_text: str,
    target_user_id: Optional[str] = None,
    cooldown_days: int = PROACTIVE_OUTREACH_COOLDOWN_DAYS,
) -> bool:
    """
    Check if we've already reached out about a semantically similar signal or target recently.

    Uses semantic similarity (Jaccard >= 0.5 OR SequenceMatcher >= 0.6) instead of
    exact hash matching for more robust duplicate detection.

    Args:
        db: Database client
        user_id: User ID receiving the outreach
        signal_text: The signal text to check
        target_user_id: Optional target user ID to check
        cooldown_days: Days to look back for duplicates

    Returns:
        True if duplicate (should skip), False if OK to proceed
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
    cutoff_iso = cutoff.isoformat()

    # Check for semantically similar past signals
    try:
        recent_texts = await db.get_recent_outreach_texts_v1(
            user_id=user_id,
            since=cutoff_iso,
        )
        for past in recent_texts:
            past_text = past.get("signal_text") or ""
            if past_text and is_semantic_duplicate(signal_text, past_text):
                logger.info(
                    "[DUPLICATE_CHECKER] found_semantic_match user_id=%s past_text=%s",
                    user_id[:8] if user_id else "?",
                    past_text[:30] if past_text else "?",
                )
                return True
    except Exception as e:
        logger.warning(
            "[DUPLICATE_CHECKER] signal_check_failed user_id=%s error=%s",
            user_id[:8] if user_id else "?",
            str(e),
        )
        # Fail open - allow outreach if check fails
        pass

    # Check for same target
    if target_user_id:
        try:
            recent_by_target = await db.get_recent_outreach_by_target_v1(
                user_id=user_id,
                target_user_id=target_user_id,
                since=cutoff_iso,
            )
            if recent_by_target:
                logger.info(
                    "[DUPLICATE_CHECKER] found_target_match user_id=%s target=%s",
                    user_id[:8] if user_id else "?",
                    target_user_id[:8] if target_user_id else "?",
                )
                return True
        except Exception as e:
            logger.warning(
                "[DUPLICATE_CHECKER] target_check_failed user_id=%s error=%s",
                user_id[:8] if user_id else "?",
                str(e),
            )
            # Fail open - allow outreach if check fails
            pass

    return False


# Backwards compatibility alias
async def check_duplicate_outreach_by_demand(
    db: DatabaseClient,
    *,
    user_id: str,
    demand_text: str,
    target_user_id: Optional[str] = None,
    cooldown_days: int = PROACTIVE_OUTREACH_COOLDOWN_DAYS,
) -> bool:
    """Backwards compatibility alias for check_duplicate_outreach."""
    return await check_duplicate_outreach(
        db,
        user_id=user_id,
        signal_text=demand_text,
        target_user_id=target_user_id,
        cooldown_days=cooldown_days,
    )
