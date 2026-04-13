"""Summarize user demand and value from onboarding conversations.

Extracts first-person, embedding-optimized summaries from turn histories
collected during needs_eval and value_eval stages.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from app.integrations.azure_openai_client import AzureOpenAIClient

logger = logging.getLogger(__name__)


def _format_turn_history(turn_history: List[Dict[str, Any]]) -> str:
    """Format turn history into a readable conversation transcript."""
    if not turn_history:
        return ""

    lines = []
    for turn in turn_history:
        role = turn.get("role", "unknown")
        content = turn.get("content", "")
        if content:
            speaker = "Frank" if role == "frank" else "User"
            lines.append(f"{speaker}: {content}")

    return "\n".join(lines)


async def summarize_onboarding_demand_value(
    need_turn_history: List[Dict[str, Any]],
    value_turn_history: List[Dict[str, Any]],
    user_profile: Dict[str, Any],
) -> Dict[str, Optional[str]]:
    """
    Use LLM to summarize the user's networking demand and professional value
    from their onboarding conversations.

    Args:
        need_turn_history: Conversation history from needs_eval stage
        value_turn_history: Conversation history from value_eval stage
        user_profile: User's profile data for context

    Returns:
        {"demand_summary": "...", "value_summary": "..."}
        Values may be None if summarization fails or no data available.
    """
    result: Dict[str, Optional[str]] = {
        "demand_summary": None,
        "value_summary": None,
    }

    # Format conversations
    need_conversation = _format_turn_history(need_turn_history)
    value_conversation = _format_turn_history(value_turn_history)

    if not need_conversation and not value_conversation:
        logger.warning("No conversation history available for summarization")
        return result

    # Get user context for better summaries
    name = user_profile.get("name", "")
    university = user_profile.get("university", "")
    major = user_profile.get("major", "")
    year = user_profile.get("year", "")

    context_parts = []
    if name:
        context_parts.append(f"Name: {name}")
    if university:
        context_parts.append(f"University: {university}")
    if major:
        context_parts.append(f"Major: {major}")
    if year:
        context_parts.append(f"Year: {year}")

    user_context = "\n".join(context_parts) if context_parts else "No profile context available"

    # Build prompt
    prompt = f"""You are summarizing a user's networking needs and professional value from their onboarding conversations.

USER CONTEXT:
{user_context}

NEEDS CONVERSATION (what they want from networking):
{need_conversation if need_conversation else "(No conversation recorded)"}

VALUE CONVERSATION (what they can offer):
{value_conversation if value_conversation else "(No conversation recorded)"}

TASK:
Extract two first-person summaries optimized for semantic embedding matching:

1. DEMAND SUMMARY: A first-person statement of who they want to meet and why.
   Format: "I want to meet [specific types of people] to [specific goals]"
   Example: "I want to meet VCs and startup founders to get funding and advice for my AI startup"

2. VALUE SUMMARY: A first-person statement of their background and what they offer.
   Format: "I'm a [background] who [specific accomplishments/skills]"
   Example: "I'm a Stanford CS senior who built a consumer app with 10k users and has experience in ML"

IMPORTANT:
- Be specific and concrete, not generic
- Include actual details from the conversation (company names, skills, numbers)
- Keep each summary to 1-2 sentences
- If no meaningful information is available for a field, return null for that field

Respond in JSON format:
{{"demand_summary": "..." or null, "value_summary": "..." or null}}"""

    try:
        client = AzureOpenAIClient()
        response = await client.generate_response(
            user_prompt=prompt,
            temperature=0.3,
            trace_label="onboarding_summarization",
        )

        if not response:
            logger.warning("Empty response from LLM for onboarding summarization")
            return result

        # Parse JSON response
        response_text = response.strip()
        # Handle potential markdown code blocks
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()

        parsed = json.loads(response_text)

        if isinstance(parsed.get("demand_summary"), str):
            result["demand_summary"] = parsed["demand_summary"].strip() or None
        if isinstance(parsed.get("value_summary"), str):
            result["value_summary"] = parsed["value_summary"].strip() or None

        logger.info(
            "Summarized onboarding: demand=%s, value=%s",
            bool(result["demand_summary"]),
            bool(result["value_summary"]),
        )

    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM response as JSON: %s", e)
    except Exception as e:
        logger.error("Failed to summarize onboarding demand/value: %s", e, exc_info=True)

    return result
