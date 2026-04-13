"""Invitation and welcome message prompts for networking.

Contains template functions for generating welcome messages
when creating group chats between connected users.
"""

from typing import List, Optional


def get_welcome_prompt(
    user_a_name: str,
    user_b_name: str,
    university: Optional[str] = None,
    matching_reasons: Optional[List[str]] = None,
) -> str:
    """Generate a fallback welcome message for a group chat.

    This is used when the LLM-generated message fails or is unavailable.

    Args:
        user_a_name: Name of the first user (initiator)
        user_b_name: Name of the second user (target)
        university: Shared university if any
        matching_reasons: Why these two people are a good match

    Returns:
        Welcome message string
    """
    # Get first names for friendlier tone
    a_first = user_a_name.split()[0] if user_a_name else "friend"
    b_first = user_b_name.split()[0] if user_b_name else "friend"

    # Separate distance from other reasons
    distance_str = ""
    reason_parts = []
    if matching_reasons:
        for reason in matching_reasons:
            if "miles away" in reason.lower():
                distance_str = f" ({reason})"
            else:
                reason_parts.append(reason)

    # Build the message
    intro = f"hey {a_first} and {b_first}! excited to introduce you two."

    # Add context about the match (use first 2 reasons to avoid wall of text)
    context_parts = []
    if university:
        context_parts.append(f"you're both from {university}")
    context_parts.extend(reason_parts[:2])

    context = " " + " ".join(context_parts) if context_parts else ""

    closing = f"{distance_str}. introduce yourselves and nerd out"

    return intro + context + closing
