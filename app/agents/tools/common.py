"""Common tools shared across agent tasks.

These tools handle:
- Message sending via Photon
- User profile fetching
- Memory persistence with Zep
- General utilities
"""

import logging
from typing import Any, Dict, List, Optional

from app.agents.tools.base import tool, ToolResult
from app.database.client import DatabaseClient

logger = logging.getLogger(__name__)


@tool(
    name="send_message",
    description="Send an SMS/iMessage to a phone number via Photon.",
)
async def send_message(
    to_number: str,
    message: str,
    send_style: str = "normal",
) -> ToolResult:
    """Send a message to a user.

    Args:
        to_number: Recipient phone number
        message: Message content
        send_style: Style (normal, invisible, loud, gentle, celebration)

    Returns:
        ToolResult indicating success
    """
    try:
        from app.integrations.photon_client import PhotonClient

        photon = PhotonClient()
        await photon.send_message(
            to=to_number,
            body=message,
            effect=send_style if send_style != "normal" else None,
        )

        return ToolResult(
            success=True,
            data={
                "sent": True,
                "to": to_number,
            },
        )

    except Exception as e:
        logger.error(f"[COMMON] send_message failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Message send failed: {str(e)}",
        )


@tool(
    name="get_user_profile",
    description="Fetch a user's profile from the database by ID or phone number.",
)
async def get_user_profile(
    user_id: Optional[str] = None,
    phone_number: Optional[str] = None,
) -> ToolResult:
    """Get user profile from database.

    Args:
        user_id: User's ID (preferred)
        phone_number: User's phone number (fallback)

    Returns:
        ToolResult with user profile data
    """
    try:
        db = DatabaseClient()

        if user_id:
            profile = await db.get_user_by_id(user_id)
        elif phone_number:
            result = await db.get_or_create_user(phone_number)
            profile = result
        else:
            return ToolResult(
                success=False,
                error="Must provide user_id or phone_number",
            )

        if not profile:
            return ToolResult(
                success=False,
                error="User not found",
            )

        return ToolResult(
            success=True,
            data=profile,
        )

    except Exception as e:
        logger.error(f"[COMMON] get_user_profile failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Profile fetch failed: {str(e)}",
        )


@tool(
    name="get_enriched_user_profile",
    description="Fetch a user's profile enriched with Zep knowledge graph context.",
)
async def get_enriched_user_profile(
    user_id: Optional[str] = None,
    phone_number: Optional[str] = None,
    query: Optional[str] = None,
) -> ToolResult:
    """Get user profile enriched with Zep knowledge graph facts.

    This extends get_user_profile by adding context from Zep's knowledge graph,
    including email-derived insights and extracted facts about the user.

    Args:
        user_id: User's ID (preferred)
        phone_number: User's phone number (fallback)
        query: Optional query to focus context retrieval (e.g., demand/value)

    Returns:
        ToolResult with enriched user profile data including:
        - All standard profile fields
        - zep_context: Full context string from Zep
        - zep_summary: High-level user summary
        - zep_facts: List of extracted facts
        - zep_email_insights: Email-derived insights
        - context_source: "zep_graph" or "profile_fallback"
    """
    # First get the base profile
    base_result = await get_user_profile(user_id, phone_number)
    if not base_result.success:
        return base_result

    profile = base_result.data
    resolved_user_id = profile.get("id")

    if not resolved_user_id:
        return base_result

    # Try to enrich with Zep context
    try:
        from app.config import settings

        if not getattr(settings, 'zep_graph_enabled', False):
            return base_result

        from app.agents.tools.user_context import get_enriched_user_context

        enriched = await get_enriched_user_context(
            user_id=resolved_user_id,
            query=query,
            include_facts=True,
            include_summary=True,
        )

        # Merge enriched data into profile
        enriched_profile = {
            **profile,
            "zep_context": enriched.get("context"),
            "zep_summary": enriched.get("user_summary"),
            "zep_facts": enriched.get("facts", []),
            "zep_email_insights": enriched.get("email_insights", []),
            "context_source": enriched.get("source"),
        }

        return ToolResult(success=True, data=enriched_profile)

    except Exception as e:
        logger.debug(f"[COMMON] Zep enrichment failed, returning base profile: {e}")
        return base_result


@tool(
    name="save_to_zep",
    description="Save conversation messages to Zep memory.",
)
async def save_to_zep(
    thread_id: str,
    messages: List[Dict[str, Any]],
    metadata: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    """Save messages to Zep memory.

    Args:
        thread_id: Zep thread ID
        messages: Messages to save
        metadata: Optional metadata

    Returns:
        ToolResult indicating success
    """
    try:
        from app.integrations.zep_client_simple import ZepMemoryClient

        zep = ZepMemoryClient()
        # Add each message individually since add_message takes one message at a time
        for msg in messages:
            await zep.add_message(
                thread_id=thread_id,
                content=msg.get("content", ""),
                role=msg.get("role", "user"),
                metadata=metadata,
            )

        return ToolResult(
            success=True,
            data={"saved": True, "message_count": len(messages)},
        )

    except Exception as e:
        logger.error(f"[COMMON] save_to_zep failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Zep save failed: {str(e)}",
        )


@tool(
    name="get_zep_memory",
    description="Retrieve conversation memory from Zep.",
)
async def get_zep_memory(
    thread_id: str,
    limit: int = 10,
) -> ToolResult:
    """Get memory from Zep.

    Args:
        thread_id: Zep thread ID
        limit: Maximum messages to retrieve

    Returns:
        ToolResult with memory data
    """
    try:
        from app.integrations.zep_client_simple import ZepMemoryClient

        zep = ZepMemoryClient()
        memory = await zep.get_memory(thread_id=thread_id, limit=limit)

        return ToolResult(
            success=True,
            data={
                "messages": memory.get("messages", []),
                "summary": memory.get("summary"),
                "facts": memory.get("facts", []),
            },
        )

    except Exception as e:
        logger.error(f"[COMMON] get_zep_memory failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Zep fetch failed: {str(e)}",
        )


@tool(
    name="generate_response",
    description="Generate a conversational response using LLM.",
)
async def generate_response(
    system_prompt: str,
    user_message: str,
    context: Optional[Dict[str, Any]] = None,
    model: str = "gpt-4o-mini",
    temperature: float = 0.7,
) -> ToolResult:
    """Generate an LLM response.

    Args:
        system_prompt: System prompt
        user_message: User's message
        context: Optional context to include
        model: Model to use
        temperature: Temperature setting

    Returns:
        ToolResult with generated response
    """
    try:
        from app.integrations.azure_openai_client import AzureOpenAIClient

        openai = AzureOpenAIClient()

        # Build user prompt with context
        user_prompt = user_message
        if context:
            context_str = "\n".join(f"- {k}: {v}" for k, v in context.items())
            user_prompt = f"Context:\n{context_str}\n\nUser message: {user_message}"

        response = await openai.generate_response(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            temperature=temperature,
        )

        return ToolResult(
            success=True,
            data={"response": response.strip()},
        )

    except Exception as e:
        logger.error(f"[COMMON] generate_response failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Response generation failed: {str(e)}",
        )
