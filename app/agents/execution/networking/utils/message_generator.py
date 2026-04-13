"""Message generator for networking invitations.

Generates personalized invitation messages for target users
when an initiator confirms a match.
"""

import logging
from typing import List, Optional

from app.integrations.azure_openai_client import AzureOpenAIClient

logger = logging.getLogger(__name__)


async def generate_invitation_message(
    initiator_name: str,
    target_name: str,
    matching_reasons: List[str],
    openai: Optional[AzureOpenAIClient] = None,
) -> Optional[str]:
    """Generate a personalized invitation message for the target user.

    This message is sent to the target user when the initiator confirms
    they want to connect.

    Args:
        initiator_name: Name of the person requesting the connection
        target_name: Name of the person receiving the invitation
        matching_reasons: Why these two people are a good match
        openai: Optional OpenAI client (creates one if not provided)

    Returns:
        Generated invitation message or None on failure
    """
    try:
        client = openai or AzureOpenAIClient()

        # Format matching reasons
        reasons_str = " ".join(matching_reasons) if matching_reasons else "you'd be a great match"

        system_prompt = """Generate a brief, casual text message inviting someone to connect. Write in Frank's voice - lowercase, friendly, like texting a friend.

Rules:
- Start with "hey [name]," (lowercase)
- CRITICAL: First sentence MUST say "[initiator name] wants to connect with you" - makes it clear this is an INVITATION
- 2-3 sentences with SPECIFIC reasons why they'd be great together (shared interests, projects, skills)
- IMPORTANT: If matching reasons include a distance like "X.X miles away", you MUST include the EXACT distance in parentheses like "(0.1 miles away)" — do NOT paraphrase as "nearby" or "close by"
- Add personality - sound excited about the match, use casual language
- End with a casual ask like "down to connect?" or "want me to introduce you two?"
- NO email formatting, NO emojis, NO markdown
- lowercase everything, no ending punctuation on last sentence

GOOD example:
hey sarah, alex (0.1 miles away) wants to connect with you! he's been grinding on algo trading and saw you're into the same stuff. you'd both probably nerd out over quant strategies. down to connect?

BAD example (too formal):
Hi Sarah, Alex wants to connect with you! Alex is a product manager looking to learn about machine learning. Would you be open to connecting? Reply YES to connect!"""

        user_prompt = f"""Write a casual text message for {target_name}.

IMPORTANT: {initiator_name} wants to connect with {target_name}.
The FIRST sentence MUST say "{initiator_name} wants to connect with you" to make it clear this is an invitation FROM {initiator_name}.

Why they're a good match (use these specific details to make it personal):
{reasons_str}

Remember: lowercase, casual, add personality. Sound like you found them a great connection."""

        response = await client.generate_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model="gpt-4o-mini",
            temperature=0.7,
            max_tokens=200,
            trace_label="generate_invitation_message",
        )

        if not response:
            logger.warning("[MESSAGE_GEN] Empty response from LLM")
            return _fallback_invitation(initiator_name, target_name)

        message = response.strip()
        logger.info(f"[MESSAGE_GEN] Generated invitation for {target_name}")

        return message

    except Exception as e:
        logger.error(f"[MESSAGE_GEN] generate_invitation_message failed: {e}", exc_info=True)
        return _fallback_invitation(initiator_name, target_name)


def _fallback_invitation(initiator_name: str, target_name: str) -> str:
    """Generate a fallback invitation if LLM fails.

    Args:
        initiator_name: Name of the initiator
        target_name: Name of the target

    Returns:
        Simple fallback invitation message
    """
    return (
        f"hey {target_name}, {initiator_name} wants to connect with you! "
        f"think you two would vibe based on what i know about both of you. "
        f"down to connect?"
    )


async def generate_groupchat_welcome_message(
    user_a_name: str,
    user_b_name: str,
    matching_reasons: Optional[List[str]] = None,
    openai: Optional[AzureOpenAIClient] = None,
) -> Optional[str]:
    """Generate a personalized welcome message for a group chat.

    This message is sent when both users have agreed to connect and
    a group chat is created.

    Args:
        user_a_name: Name of the first user (initiator)
        user_b_name: Name of the second user (target)
        matching_reasons: Why these two people are a good match
        openai: Optional OpenAI client (creates one if not provided)

    Returns:
        Generated welcome message or None on failure
    """
    try:
        client = openai or AzureOpenAIClient()

        # Format matching reasons
        reasons_str = " ".join(matching_reasons) if matching_reasons else "you two would be a great match"

        # Get first names for friendlier tone
        a_first = user_a_name.split()[0] if user_a_name else "friend"
        b_first = user_b_name.split()[0] if user_b_name else "friend"

        system_prompt = """Generate a brief, casual welcome message for a group chat introducing two people. Write in Frank's voice - lowercase, friendly, like you're hyped to see this connection happen.

Rules:
- Start with "hey [name] and [name]!" (lowercase)
- 2-3 sentences: mention SPECIFIC reasons they matched and hype them up to chat
- IMPORTANT: If matching reasons include a distance like "X.X miles away", you MUST include the EXACT distance in parentheses like "(0.1 miles away)" — do NOT paraphrase as "nearby" or "close by"
- Add personality - sound excited about bringing them together
- NO emojis, NO markdown, NO email formatting
- lowercase everything, keep it casual

GOOD example:
hey alex and sarah! you're both deep into quant trading and wanted study partners (0.1 miles away), so this felt like a no-brainer. i'll let you two take it from here, introduce yourselves and nerd out

BAD example:
Hey Alex and Sarah! I matched you because you have similar interests. Feel free to introduce yourselves!"""

        user_prompt = f"""Write a casual welcome message for a group chat between {a_first} and {b_first}.

Why they matched (use these specifics to make it personal):
{reasons_str}

Remember: lowercase, excited, add personality. Sound like you're pumped this connection is happening."""

        response = await client.generate_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model="gpt-4o-mini",
            temperature=0.7,
            max_tokens=250,
            trace_label="generate_groupchat_welcome_message",
        )

        if not response:
            logger.warning("[MESSAGE_GEN] Empty response from LLM for welcome message")
            return None

        message = response.strip()
        logger.info(f"[MESSAGE_GEN] Generated welcome message for {a_first} & {b_first}")

        return message

    except Exception as e:
        logger.error(f"[MESSAGE_GEN] generate_groupchat_welcome_message failed: {e}", exc_info=True)
        return None


async def generate_late_joiner_intro(
    new_joiner_name: str,
    existing_members: List[str],
    connection_purpose: Optional[str] = None,
    matching_reasons: Optional[List[str]] = None,
    llm_introduction: Optional[str] = None,
    openai: Optional[AzureOpenAIClient] = None,
) -> str:
    """Generate a detailed warm intro when someone joins an existing group.

    This is for late joiners in multi-match groups - people who accept after
    the group was already created. The intro should be detailed and help
    the existing members understand who this new person is.

    Args:
        new_joiner_name: Name of the person joining
        existing_members: List of names of people already in the group
        connection_purpose: The initiator's goal (e.g., "algo trading study group")
        matching_reasons: Why this person was matched to the group
        llm_introduction: LLM-generated introduction about the person
        openai: Optional OpenAI client (creates one if not provided)

    Returns:
        Generated warm intro message
    """
    try:
        client = openai or AzureOpenAIClient()

        # Get first name for friendlier tone
        new_joiner_first = new_joiner_name.split()[0] if new_joiner_name else "someone"
        existing_first_names = [name.split()[0] if name else "friend" for name in existing_members]

        # Build context about the new joiner
        context_parts = []
        if llm_introduction:
            context_parts.append(f"About them: {llm_introduction}")
        if matching_reasons:
            context_parts.append(f"Why they matched: {', '.join(matching_reasons)}")
        if connection_purpose:
            context_parts.append(f"Group purpose: {connection_purpose}")

        context_str = "\n".join(context_parts) if context_parts else "They're a great fit for this group"

        system_prompt = """Generate a warm intro message for someone joining an existing group chat. Write in Frank's voice - lowercase, friendly, hyped to see the group grow.

Rules:
- Start with "hey all," or "yo everyone," (lowercase)
- Announce who just joined: "[name] just joined!"
- Give a DETAILED intro about the new person - who they are, what they're working on, why they're perfect for this group
- IMPORTANT: If matching reasons include a distance like "X.X miles away", you MUST include the EXACT distance in parentheses like "(0.1 miles away)" — do NOT paraphrase as "nearby" or "close by"
- Encourage the group to welcome them and share context
- Make it feel like you're vouching for this person
- 3-4 sentences, add personality
- NO emojis, NO markdown, NO email formatting
- lowercase everything, keep it casual

GOOD example:
hey all, sarah just joined! she's been grinding on ml for finance and is super interested in building quant strategies (0.3 miles away). she's at penn studying comp sci and has already built a few trading bots. think she'll add a lot to the convo, def introduce yourselves

BAD example (too short/generic):
hey all, sarah just joined the group!

BAD example (too formal):
Hello everyone, I'm pleased to announce that Sarah has joined our group. She has experience in machine learning."""

        user_prompt = f"""Write a warm intro for {new_joiner_first} joining a group chat.

Existing members: {', '.join(existing_first_names)}

Context about the new joiner:
{context_str}

Make it detailed and personal - help the existing members understand who {new_joiner_first} is and why they should be excited to have them join."""

        response = await client.generate_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model="gpt-4o-mini",
            temperature=0.7,
            max_tokens=250,
            trace_label="generate_late_joiner_intro",
        )

        if response:
            message = response.strip()
            logger.info(f"[MESSAGE_GEN] Generated late joiner intro for {new_joiner_first}")
            return message

    except Exception as e:
        logger.error(f"[MESSAGE_GEN] generate_late_joiner_intro failed: {e}", exc_info=True)

    # Fallback message
    return _fallback_late_joiner_intro(new_joiner_name, matching_reasons)


def _fallback_late_joiner_intro(
    new_joiner_name: str,
    matching_reasons: Optional[List[str]] = None,
) -> str:
    """Generate a fallback late joiner intro if LLM fails.

    Args:
        new_joiner_name: Name of the new joiner
        matching_reasons: Why they matched

    Returns:
        Simple fallback intro message
    """
    first_name = new_joiner_name.split()[0].lower() if new_joiner_name else "someone"
    reason = matching_reasons[0] if matching_reasons else "they're a great fit"

    return (
        f"hey all, {first_name} just joined! "
        f"{reason}. "
        f"def introduce yourselves and catch them up"
    )
