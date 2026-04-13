"""Context embedding utilities for networking match.

Builds synthesized context text from user background data (university, major,
location, year, career_interests) and generates/updates context embeddings.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def build_context_text(profile: Dict[str, Any]) -> Optional[str]:
    """Build context text from user profile background data.

    Synthesizes university, major, location, year, and career_interests
    into a single text string suitable for embedding generation.

    Args:
        profile: User profile dictionary containing background fields

    Returns:
        Synthesized context text, or None if no meaningful data
    """
    parts: List[str] = []

    # University
    if university := profile.get("university"):
        if isinstance(university, str) and university.strip():
            parts.append(university.strip())

    # Major
    if major := profile.get("major"):
        if isinstance(major, str) and major.strip():
            parts.append(major.strip())

    # Location
    if location := profile.get("location"):
        if isinstance(location, str) and location.strip():
            parts.append(location.strip())

    # Year (e.g., "Junior", "Senior", "2025")
    if year := profile.get("year"):
        year_str = str(year).strip()
        if year_str:
            parts.append(year_str)

    # Career interests - format as "interested in X, Y, Z"
    if career_interests := profile.get("career_interests"):
        if isinstance(career_interests, list) and career_interests:
            # Filter out empty strings and join
            interests = [
                str(i).strip() for i in career_interests
                if i and str(i).strip()
            ]
            if interests:
                parts.append(f"interested in {', '.join(interests)}")

    if not parts:
        return None

    return ", ".join(parts)


async def generate_context_embedding(
    profile: Dict[str, Any],
    openai_client: Any,
) -> Optional[List[float]]:
    """Generate context embedding from user profile.

    Args:
        profile: User profile dictionary
        openai_client: AzureOpenAIClient instance for generating embeddings

    Returns:
        1536-dimension embedding vector, or None if no context data
    """
    context_text = build_context_text(profile)

    if not context_text:
        logger.debug(f"No context data for user {profile.get('id')}, skipping embedding")
        return None

    try:
        embedding = await openai_client.get_embedding(context_text)
        if embedding:
            logger.info(
                f"Generated context embedding for user {profile.get('id')}: "
                f"'{context_text[:50]}...'"
            )
        return embedding
    except Exception as e:
        logger.error(f"Failed to generate context embedding: {e}", exc_info=True)
        return None


async def update_user_context_embedding(
    user_id: str,
    profile: Dict[str, Any],
    db_client: Any,
    openai_client: Any,
) -> bool:
    """Generate and persist context embedding for a user.

    Also stores the context_summary text used to generate the embedding.

    Args:
        user_id: User ID
        profile: User profile dictionary
        db_client: DatabaseClient instance
        openai_client: AzureOpenAIClient instance

    Returns:
        True if embedding was updated, False otherwise
    """
    # Build context text first so we can store it
    context_text = build_context_text(profile)

    if not context_text:
        logger.debug(f"No context data for user {user_id}, skipping embedding")
        return False

    try:
        # Generate embedding
        embedding = await openai_client.get_embedding(context_text)

        if not embedding:
            logger.warning(f"Failed to generate embedding for user {user_id}")
            return False

        # Store both embedding and the context_summary text
        await db_client.update_context_embedding(
            user_id=user_id,
            embedding=embedding,
            context_summary=context_text
        )
        logger.info(
            f"Updated context embedding for user {user_id}: '{context_text[:50]}...'"
        )
        return True

    except Exception as e:
        logger.error(
            f"Failed to update context embedding for user {user_id}: {e}",
            exc_info=True
        )
        return False
