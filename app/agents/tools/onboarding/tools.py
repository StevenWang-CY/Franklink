"""Onboarding tools for collecting user profile information.

These tools handle:
- Extracting profile fields from user messages
- Updating user profile data
- Managing onboarding stage transitions
- Email connection flow
"""

import logging
from typing import Any, Dict, List, Optional

from app.agents.tools.base import tool, ToolResult
from app.database.client import DatabaseClient

logger = logging.getLogger(__name__)


@tool(
    name="extract_profile_fields",
    description="Extract onboarding profile fields (name, school, career interests) from a user message using LLM. "
    "Returns extracted fields with quality indicators.",
)
async def extract_profile_fields(
    message: str,
    history: Optional[List[Dict[str, Any]]] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    """Extract profile fields from user message.

    Args:
        message: User's message content
        history: Recent conversation history
        profile: Current user profile for context

    Returns:
        ToolResult with extracted fields and quality indicators
    """
    try:
        from app.agents.tools.onboarding.extraction import (
            extract_onboarding_fields,
        )

        extraction = await extract_onboarding_fields(
            message=message,
            history=history,
            profile=profile,
        )

        if not extraction:
            return ToolResult(
                success=True,
                data={},
                metadata={"no_fields_extracted": True},
            )

        return ToolResult(
            success=True,
            data=extraction,
            metadata={
                "has_name": bool(extraction.get("name")),
                "has_school": bool(extraction.get("school")),
                "has_interests": bool(extraction.get("career_interests")),
            },
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] extract_profile_fields failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Field extraction failed: {str(e)}",
        )


@tool(
    name="update_profile",
    description="Update user profile fields in the database. Handles both basic fields and complex nested data.",
)
async def update_profile(
    user_id: str,
    updates: Dict[str, Any],
) -> ToolResult:
    """Update user profile with new field values.

    Args:
        user_id: User's ID
        updates: Dictionary of field updates

    Returns:
        ToolResult indicating success
    """
    try:
        db = DatabaseClient()
        await db.update_user_profile(user_id, updates)

        return ToolResult(
            success=True,
            data={"updated_fields": list(updates.keys())},
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] update_profile failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Profile update failed: {str(e)}",
        )


@tool(
    name="get_next_missing_field",
    description="Determine the next missing field in the onboarding sequence. "
    "Returns the field name and suggested prompt.",
)
async def get_next_missing_field(
    user_profile: Dict[str, Any],
) -> ToolResult:
    """Determine what field to collect next in onboarding.

    Args:
        user_profile: Current user profile

    Returns:
        ToolResult with next field info
    """
    try:
        personal_facts = user_profile.get("personal_facts") or {}
        current_stage = user_profile.get("onboarding_stage", "name")

        # Check completion status
        if user_profile.get("is_onboarded"):
            return ToolResult(
                success=True,
                data={
                    "complete": True,
                    "stage": "complete",
                    "next_field": None,
                },
            )

        # Check rejection status
        if current_stage == "rejected":
            return ToolResult(
                success=True,
                data={
                    "complete": False,
                    "stage": "rejected",
                    "next_field": None,
                    "rejected": True,
                },
            )

        # Determine next field based on what's missing
        if not user_profile.get("name"):
            return ToolResult(
                success=True,
                data={
                    "complete": False,
                    "stage": "name",
                    "next_field": "name",
                    "prompt_hint": "ask_name",
                },
            )

        if not user_profile.get("university"):
            return ToolResult(
                success=True,
                data={
                    "complete": False,
                    "stage": "school",
                    "next_field": "university",
                    "prompt_hint": "ask_school",
                },
            )

        if not user_profile.get("career_interests"):
            return ToolResult(
                success=True,
                data={
                    "complete": False,
                    "stage": "career_interest",
                    "next_field": "career_interests",
                    "prompt_hint": "ask_career_interest",
                },
            )

        # Check email connect status
        email_state = personal_facts.get("email_connect", {})
        if not email_state.get("status") or email_state.get("status") == "link_sent":
            return ToolResult(
                success=True,
                data={
                    "complete": False,
                    "stage": "email_connect",
                    "next_field": None,
                    "prompt_hint": "email_connect",
                    "email_status": email_state.get("status"),
                },
            )

        # Check needs eval
        need_state = personal_facts.get("frank_need_eval", {})
        if need_state.get("status") != "accepted":
            return ToolResult(
                success=True,
                data={
                    "complete": False,
                    "stage": "needs_eval",
                    "next_field": None,
                    "prompt_hint": "needs_eval",
                },
            )

        # Check value eval
        value_state = personal_facts.get("frank_value_eval", {})
        if value_state.get("status") != "accepted":
            return ToolResult(
                success=True,
                data={
                    "complete": False,
                    "stage": "value_eval",
                    "next_field": None,
                    "prompt_hint": "value_eval",
                },
            )

        # Check share stage
        if current_stage == "share_to_complete":
            return ToolResult(
                success=True,
                data={
                    "complete": False,
                    "stage": "share_to_complete",
                    "next_field": None,
                    "prompt_hint": "share_to_complete",
                },
            )

        # Default: onboarding complete
        return ToolResult(
            success=True,
            data={
                "complete": True,
                "stage": "complete",
                "next_field": None,
            },
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] get_next_missing_field failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Failed to determine next field: {str(e)}",
        )


@tool(
    name="initiate_email_connect",
    description="Generate an email OAuth link for the user to connect their Gmail account.",
)
async def initiate_email_connect(user_id: str) -> ToolResult:
    """Initiate email connection flow.

    Args:
        user_id: User's ID

    Returns:
        ToolResult with auth link or error
    """
    try:
        from app.integrations.composio_client import ComposioClient

        composio = ComposioClient()
        auth_link = await composio.initiate_gmail_connect(user_id=user_id)

        if auth_link:
            return ToolResult(
                success=True,
                data={
                    "auth_link": auth_link,
                    "status": "link_sent",
                },
            )
        else:
            return ToolResult(
                success=False,
                error="Failed to generate auth link",
                metadata={"error_code": composio.get_last_connect_error_code()},
            )

    except Exception as e:
        logger.error(f"[ONBOARDING] initiate_email_connect failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Email connect failed: {str(e)}",
        )


@tool(
    name="classify_email_reply",
    description="Classify user's reply to email connect prompt (connected, declined, question, etc).",
)
async def classify_email_reply(
    message: str,
    user_profile: Dict[str, Any],
    email_state: Dict[str, Any],
) -> ToolResult:
    """Classify user's response to email connect request.

    Args:
        message: User's message
        user_profile: User's profile
        email_state: Current email connect state

    Returns:
        ToolResult with classification decision
    """
    try:
        from app.agents.tools.onboarding.classification import (
            classify_email_connect_reply,
        )

        result = await classify_email_connect_reply(
            message=message,
            user_profile=user_profile,
            email_state=email_state,
        )

        return ToolResult(
            success=True,
            data={
                "decision": result.get("decision", "reask"),
                "confidence": result.get("confidence", 0.0),
            },
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] classify_email_reply failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Classification failed: {str(e)}",
        )


@tool(
    name="evaluate_user_need",
    description="Evaluate user's professional need in a multi-turn conversation. "
    "Returns decision (accept/ask) with follow-up question if needed.",
)
async def evaluate_user_need(
    user_message: str,
    user_profile: Dict[str, Any],
    prior_state: Dict[str, Any],
) -> ToolResult:
    """Evaluate user's need through conversation.

    Args:
        user_message: User's message
        user_profile: User's profile
        prior_state: Prior evaluation state

    Returns:
        ToolResult with evaluation decision and question
    """
    try:
        from app.agents.tools.onboarding.evaluation import evaluate_user_need as _evaluate

        result = await _evaluate(
            user_message=user_message,
            user_profile=user_profile,
            prior_state=prior_state,
        )

        return ToolResult(
            success=True,
            data={
                "decision": result.get("decision", "ask"),
                "question": result.get("question", ""),
                "question_type": result.get("question_type", ""),
                "user_need": result.get("user_need"),
            },
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] evaluate_user_need failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Need evaluation failed: {str(e)}",
        )


@tool(
    name="evaluate_user_value",
    description="Evaluate user's professional value in a multi-turn conversation. "
    "Returns decision (accept/reject/ask) with intro fee calculation.",
)
async def evaluate_user_value(
    phone_number: str,
    user_message: str,
    user_profile: Dict[str, Any],
    prior_state: Dict[str, Any],
) -> ToolResult:
    """Evaluate user's value through conversation.

    Args:
        phone_number: User's phone number
        user_message: User's message
        user_profile: User's profile
        prior_state: Prior evaluation state

    Returns:
        ToolResult with evaluation decision, fee, and signals
    """
    try:
        from app.agents.tools.onboarding.evaluation import evaluate_user_value as _evaluate

        result = await _evaluate(
            phone_number=phone_number,
            user_message=user_message,
            user_profile=user_profile,
            prior_state=prior_state,
        )

        return ToolResult(
            success=True,
            data={
                "decision": result.get("decision", "ask"),
                "question": result.get("question", ""),
                "question_type": result.get("question_type", ""),
                "user_value": result.get("user_value"),
                "intro_fee_cents": result.get("intro_fee_cents", 9900),
                "signals": result.get("signals", {}),
                "rejection_reason": result.get("rejection_reason"),
            },
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] evaluate_user_value failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Value evaluation failed: {str(e)}",
        )


@tool(
    name="classify_share_reply",
    description="Classify user's reply to the share-to-complete request. "
    "Returns decision (shared/skip/question/unclear).",
)
async def classify_share_reply(
    message: str,
    user_profile: Dict[str, Any],
    has_media: bool = False,
) -> ToolResult:
    """Classify user's response to share request.

    Args:
        message: User's message
        user_profile: User's profile
        has_media: Whether message includes media attachment

    Returns:
        ToolResult with classification decision
    """
    try:
        from app.agents.tools.onboarding.classification import (
            classify_share_reply as _classify,
        )

        result = await _classify(
            message=message,
            user_profile=user_profile,
            has_media=has_media,
        )

        return ToolResult(
            success=True,
            data={
                "decision": result.get("decision", "unclear"),
                "confidence": result.get("confidence", 0.0),
            },
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] classify_share_reply failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Classification failed: {str(e)}",
        )


@tool(
    name="execute_onboarding_stage",
    description="Execute a complete onboarding stage - handles extraction, validation, persistence, and returns context for response generation.",
)
async def execute_onboarding_stage(
    stage: str,
    message: str,
    user_profile: Dict[str, Any],
    temp_data: Dict[str, Any],
    current_message: Dict[str, Any],
) -> ToolResult:
    """Execute a full onboarding stage.

    Args:
        stage: Current onboarding stage
        message: User's message
        user_profile: User's profile
        temp_data: Temporary data with extractions
        current_message: Current message metadata

    Returns:
        ToolResult with execution result
    """
    try:
        from app.agents.tools.onboarding.executor import execute_onboarding_stage as _execute

        result = await _execute(
            stage=stage,
            message=message,
            user_profile=user_profile,
            temp_data=temp_data,
            current_message=current_message,
        )

        return ToolResult(
            success=True,
            data={
                "stage_before": result.stage_before,
                "stage_after": result.stage_after,
                "extracted_fields": result.extracted_fields,
                "persisted": result.persisted,
                "waiting_for": result.waiting_for,
                "context": result.context,
                "outbound_messages": result.outbound_messages,
                "should_share_contact_card": result.should_share_contact_card,
                "should_send_reaction": result.should_send_reaction,
            },
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] execute_onboarding_stage failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Stage execution failed: {str(e)}",
        )


@tool(
    name="send_reaction",
    description="Send a tapback reaction to a message (love, like, etc).",
)
async def send_reaction(
    to_number: str,
    message_guid: str,
    reaction_type: str,
    chat_guid: Optional[str] = None,
    message_content: Optional[str] = None,
) -> ToolResult:
    """Send a reaction to a message.

    Args:
        to_number: Recipient phone number
        message_guid: Message GUID to react to
        reaction_type: Type of reaction (love, like, etc)
        chat_guid: Optional chat GUID
        message_content: Optional message content for context

    Returns:
        ToolResult indicating success
    """
    try:
        from app.config import settings
        from app.integrations.photon_client import PhotonClient
        from app.reactions.service import ReactionService

        photon = PhotonClient(
            server_url=settings.photon_server_url,
            default_number=settings.photon_default_number,
        )

        await ReactionService(photon=photon).maybe_send_reaction(
            to_number=to_number,
            message_guid=message_guid,
            message_content=message_content or "",
            chat_guid=chat_guid,
            forced_reaction=reaction_type,
            context={"task": "onboarding"},
        )

        return ToolResult(
            success=True,
            data={"reaction_sent": reaction_type},
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] send_reaction failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Reaction send failed: {str(e)}",
        )


@tool(
    name="share_contact_card",
    description="Share Frank's contact card in a chat.",
)
async def share_contact_card(chat_guid: str) -> ToolResult:
    """Share contact card in a chat.

    Args:
        chat_guid: Chat GUID to share contact in

    Returns:
        ToolResult indicating success
    """
    try:
        from app.config import settings
        from app.integrations.photon_client import PhotonClient

        photon = PhotonClient(
            server_url=settings.photon_server_url,
            default_number=settings.photon_default_number,
            api_key=settings.photon_api_key,
        )

        await photon.share_contact_card(chat_guid)

        return ToolResult(
            success=True,
            data={"contact_card_shared": True},
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] share_contact_card failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Contact card share failed: {str(e)}",
        )


@tool(
    name="generate_onboarding_response",
    description="Generate Frank's response using the stage context from execute_onboarding_stage. "
    "Call this AFTER execute_onboarding_stage to get the actual message to send to the user.",
)
async def generate_onboarding_response(
    stage: str,
    context: Dict[str, Any],
    user_profile: Dict[str, Any],
    message: str,
    conversation_history: Optional[List[Dict[str, str]]] = None,
) -> ToolResult:
    """Generate Frank's response for the current onboarding stage.

    Args:
        stage: Current onboarding stage (name, school, career_interest, etc.)
        context: Execution context from execute_onboarding_stage containing action and data
        user_profile: User's profile with name, school, interests, etc.
        message: User's original message
        conversation_history: Optional recent conversation history

    Returns:
        ToolResult with response_text to send to user
    """
    try:
        import json
        from app.agents.interaction.prompts import get_onboarding_response_prompt
        from app.integrations.azure_openai_client import AzureOpenAIClient

        # Build the response prompt using the detailed onboarding prompts
        prompt = get_onboarding_response_prompt(
            stage=stage,
            context=context,
            user_profile=user_profile,
            message=message,
            conversation_history=conversation_history,
        )

        # Generate response using LLM
        openai = AzureOpenAIClient()
        response = await openai.generate_response(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini",
            temperature=0.7,
            max_tokens=500,
            trace_label="onboarding_response_gen",
        )

        # Clean up the response
        response_text = response.strip()

        # Handle JSON array format for multi-message responses
        # The prompt may request ["msg1", "msg2"] format
        if response_text.startswith("[") and response_text.endswith("]"):
            try:
                messages = json.loads(response_text)
                if isinstance(messages, list) and all(isinstance(m, str) for m in messages):
                    # Return as multi-message response
                    return ToolResult(
                        success=True,
                        data={
                            "response_text": messages[0] if messages else "",
                            "additional_messages": messages[1:] if len(messages) > 1 else [],
                            "is_multi_message": True,
                        },
                    )
            except json.JSONDecodeError:
                pass

        # Single message response
        return ToolResult(
            success=True,
            data={
                "response_text": response_text,
                "is_multi_message": False,
            },
        )

    except Exception as e:
        logger.error(f"[ONBOARDING] generate_onboarding_response failed: {e}", exc_info=True)
        return ToolResult(
            success=False,
            error=f"Response generation failed: {str(e)}",
        )
