"""Conversation to Zep graph synchronization utilities.

Syncs user-Frank conversations to Zep's knowledge graph so Frank can:
- Remember what users discussed in previous sessions
- Extract connection needs mentioned in conversation
- Build richer understanding of user preferences and goals
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)


def format_conversation_exchange(
    user_message: str,
    bot_response: str,
    user_name: Optional[str] = None,
    intent: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> str:
    """
    Format a conversation exchange for Zep graph ingestion.

    Optimized for Zep's fact extraction:
    - Clear structure showing who said what
    - Intent/task context for understanding purpose
    - Timestamp for temporal reasoning

    Args:
        user_message: The user's message
        bot_response: Frank's response
        user_name: User's name if known
        intent: Detected intent/task (e.g., "networking", "onboarding")
        timestamp: When the exchange occurred

    Returns:
        Formatted conversation string for Zep
    """
    user_label = user_name or "User"
    date_str = ""
    if timestamp:
        date_str = timestamp.strftime("%Y-%m-%d %H:%M")

    lines = ["Conversation with Frank:"]

    if date_str:
        lines[0] = f"Conversation with Frank on {date_str}:"

    if intent:
        lines.append(f"Context: {intent}")

    # Truncate long messages to keep context focused
    user_msg = user_message[:500] if len(user_message) > 500 else user_message
    bot_msg = bot_response[:500] if len(bot_response) > 500 else bot_response

    lines.append(f"{user_label}: {user_msg}")
    lines.append(f"Frank: {bot_msg}")
    lines.append("---")

    return "\n".join(lines)


def format_conversation_batch(
    messages: List[Dict[str, Any]],
    user_name: Optional[str] = None,
) -> str:
    """
    Format multiple conversation messages for Zep.

    Args:
        messages: List of message dicts with content, message_type, created_at, metadata
        user_name: User's name if known

    Returns:
        Formatted conversation text for Zep
    """
    if not messages:
        return ""

    # Sort by created_at to ensure chronological order
    sorted_messages = sorted(
        messages,
        key=lambda m: m.get("created_at") or "",
    )

    lines = ["Recent Conversation History:"]
    user_label = user_name or "User"

    for msg in sorted_messages:
        content = msg.get("content", "")
        msg_type = msg.get("message_type", "user")
        metadata = msg.get("metadata") or {}
        created_at = msg.get("created_at")

        # Skip empty messages
        if not content.strip():
            continue

        # Truncate long messages
        if len(content) > 400:
            content = content[:397] + "..."

        # Format based on sender
        if msg_type == "user":
            lines.append(f"{user_label}: {content}")
        else:
            intent = metadata.get("intent") or metadata.get("task")
            if intent:
                lines.append(f"Frank ({intent}): {content}")
            else:
                lines.append(f"Frank: {content}")

    if len(lines) <= 1:
        return ""

    lines.append("---")
    return "\n".join(lines)


async def sync_conversation_to_zep(
    user_id: str,
    user_message: str,
    bot_response: str,
    user_name: Optional[str] = None,
    intent: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Sync a single conversation exchange to Zep's knowledge graph.

    This should be called after each user-Frank exchange to keep
    Zep's knowledge graph current with conversation context.

    Args:
        user_id: User's UUID
        user_message: The user's message
        bot_response: Frank's response
        user_name: User's name if known
        intent: Detected intent/task

    Returns:
        Dict with success status and details
    """
    if not getattr(settings, "zep_graph_enabled", False):
        return {"success": False, "error": "Zep graph not enabled", "synced": False}

    # Skip very short exchanges that add little value
    if len(user_message) < 10 and len(bot_response) < 20:
        return {"success": True, "synced": False, "reason": "Exchange too short"}

    # Skip certain intents that don't add conversational value
    skip_intents = {"greeting", "goodbye", "thanks"}
    if intent and intent.lower() in skip_intents:
        return {"success": True, "synced": False, "reason": f"Skipped intent: {intent}"}

    try:
        from app.integrations.zep_graph_client import get_zep_graph_client

        zep = get_zep_graph_client()

        if not zep.is_graph_enabled():
            return {"success": False, "error": "Zep graph client not available"}

        # Format the exchange
        formatted = format_conversation_exchange(
            user_message=user_message,
            bot_response=bot_response,
            user_name=user_name,
            intent=intent,
            timestamp=datetime.utcnow(),
        )

        # Add to Zep graph
        result = await zep.add_to_graph(
            user_id=user_id,
            data=formatted,
            data_type="text",
        )

        if result.success:
            logger.info(
                "[CONVERSATION_ZEP] Synced exchange user=%s intent=%s chars=%d",
                user_id[:8] if user_id else "unknown",
                intent or "unknown",
                len(formatted),
            )
            return {
                "success": True,
                "synced": True,
                "episode_id": result.episode_id,
                "chars": len(formatted),
            }
        else:
            logger.warning(
                "[CONVERSATION_ZEP] Failed to sync: %s",
                result.error,
            )
            return {"success": False, "error": result.error, "synced": False}

    except Exception as e:
        logger.error(
            "[CONVERSATION_ZEP] Error syncing conversation: %s",
            str(e),
            exc_info=True,
        )
        return {"success": False, "error": str(e), "synced": False}


async def sync_conversation_batch_to_zep(
    user_id: str,
    messages: List[Dict[str, Any]],
    user_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Sync a batch of conversation messages to Zep.

    Useful for backfilling or periodic sync of conversation history.

    Args:
        user_id: User's UUID
        messages: List of message dicts
        user_name: User's name if known

    Returns:
        Dict with success status and details
    """
    if not getattr(settings, "zep_graph_enabled", False):
        return {"success": False, "error": "Zep graph not enabled", "synced": 0}

    if not messages:
        return {"success": True, "synced": 0, "reason": "No messages to sync"}

    try:
        from app.integrations.zep_graph_client import get_zep_graph_client

        zep = get_zep_graph_client()

        if not zep.is_graph_enabled():
            return {"success": False, "error": "Zep graph client not available"}

        # Format all messages
        formatted = format_conversation_batch(messages, user_name)

        if not formatted:
            return {"success": True, "synced": 0, "reason": "No content after formatting"}

        # Chunk if too large (Zep limit is 10,000 chars)
        max_chars = getattr(settings, "zep_graph_chunk_size", 9000)

        if len(formatted) <= max_chars:
            chunks = [formatted]
        else:
            # Split by conversation separators
            chunks = _chunk_conversation_text(formatted, max_chars)

        synced_count = 0
        errors = []

        for chunk in chunks:
            result = await zep.add_to_graph(
                user_id=user_id,
                data=chunk,
                data_type="text",
            )
            if result.success:
                synced_count += 1
            else:
                errors.append(result.error)

        logger.info(
            "[CONVERSATION_ZEP] Batch sync user=%s messages=%d chunks=%d synced=%d",
            user_id[:8] if user_id else "unknown",
            len(messages),
            len(chunks),
            synced_count,
        )

        return {
            "success": len(errors) == 0,
            "synced": synced_count,
            "total_chunks": len(chunks),
            "errors": errors if errors else None,
        }

    except Exception as e:
        logger.error(
            "[CONVERSATION_ZEP] Error in batch sync: %s",
            str(e),
            exc_info=True,
        )
        return {"success": False, "error": str(e), "synced": 0}


def _chunk_conversation_text(text: str, max_chars: int) -> List[str]:
    """Split conversation text into chunks respecting message boundaries."""
    chunks = []
    lines = text.split("\n")
    current_chunk = []
    current_size = 0

    for line in lines:
        line_size = len(line) + 1  # +1 for newline

        if current_size + line_size > max_chars:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_size = line_size
        else:
            current_chunk.append(line)
            current_size += line_size

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks
