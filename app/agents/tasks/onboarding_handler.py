"""Streamlined onboarding handler.

This bypasses the ReAct loop for onboarding since the flow is deterministic:
1. Execute stage logic (extraction, classification, DB updates)
2. Generate response using the detailed prompts

This reduces LLM calls from 4-5 to just 2 per message.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def handle_onboarding_message(
    message: str,
    user_profile: Dict[str, Any],
    conversation_history: Optional[List[Dict[str, str]]] = None,
    current_message: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Handle an onboarding message directly without ReAct loop.

    Args:
        message: User's message content
        user_profile: User profile dict with stage, name, school, etc.
        conversation_history: Recent conversation history
        current_message: Current message metadata (message_id, chat_guid, etc.)

    Returns:
        Dict with response_text, additional_messages, status, etc.
    """
    logger.info(f"[ONBOARDING] Handling message for user {user_profile.get('user_id', 'unknown')}")
    logger.info(f"[ONBOARDING] User profile: name={user_profile.get('name')}, university={user_profile.get('university')}, career_interests={user_profile.get('career_interests')}, is_onboarded={user_profile.get('is_onboarded')}")

    # Determine current stage
    stage = _get_current_stage(user_profile)
    logger.info(f"[ONBOARDING] Current stage: {stage}")

    try:
        # Lazy imports to avoid circular dependencies
        from app.agents.tools.onboarding.executor import execute_onboarding_stage
        from app.agents.tools.onboarding.extraction import extract_onboarding_fields

        # Step 1: Extract fields from message (1 LLM call)
        extraction = await extract_onboarding_fields(
            message=message,
            history=conversation_history,
            profile=user_profile,
        )
        logger.info(f"[ONBOARDING] Extraction complete: {list(extraction.keys()) if extraction else 'empty'}")

        # Step 2: Execute stage logic (NO LLM - pure logic)
        temp_data = {"onboarding_extraction": extraction}
        result = await execute_onboarding_stage(
            stage=stage,
            message=message,
            user_profile=user_profile,
            temp_data=temp_data,
            current_message=current_message or {},
        )
        logger.info(f"[ONBOARDING] Stage executed: {result.stage_before} -> {result.stage_after}, action={result.context.get('action')}")

        # Share contact card if needed (uses Photon API)
        from app.agents.tools.onboarding.executor import share_contact_card_if_needed
        await share_contact_card_if_needed(result, current_message or {})

        # Step 3: Generate response (1 LLM call)
        logger.info(f"[ONBOARDING] Generating response for stage={result.stage_after}, action={result.context.get('action')}")
        response_data = await _generate_response(
            stage=result.stage_after,
            context=result.context,
            user_profile=user_profile,
            message=message,
            conversation_history=conversation_history,
        )
        logger.info(f"[ONBOARDING] Response generated: response_text={response_data.get('response_text')[:50] if response_data.get('response_text') else None}...")

        # Combine outbound messages from executor with generated response
        all_messages = []
        if response_data.get("response_text"):
            all_messages.append(response_data["response_text"])
        all_messages.extend(response_data.get("additional_messages", []))
        all_messages.extend(result.outbound_messages or [])

        # Fallback if no messages were generated
        if not all_messages:
            logger.warning("[ONBOARDING] No messages generated, using fallback")
            all_messages = ["hey! what can i help you with"]

        return {
            "success": True,
            "response_text": all_messages[0] if all_messages else None,
            "additional_messages": all_messages[1:] if len(all_messages) > 1 else [],
            "stage_before": result.stage_before,
            "stage_after": result.stage_after,
            "waiting_for": result.waiting_for,
            "is_complete": result.stage_after in ("complete", "rejected"),
            "should_share_contact_card": result.should_share_contact_card,
            "should_send_reaction": result.should_send_reaction,
        }

    except Exception as e:
        logger.error(f"[ONBOARDING] Handler failed: {e}", exc_info=True)
        return {
            "success": False,
            "response_text": "sorry, something went wrong on my end. can you try that again?",
            "error": str(e),
        }


def _get_current_stage(user_profile: Dict[str, Any]) -> str:
    """Determine current onboarding stage from user profile."""
    # Check explicit stage in personal_facts
    personal_facts = user_profile.get("personal_facts") or {}
    if isinstance(personal_facts, dict):
        stage = personal_facts.get("onboarding_stage")
        if stage:
            return stage

    # Check DB onboarding_stage field
    db_stage = user_profile.get("onboarding_stage")
    if db_stage:
        return db_stage

    # Infer from collected fields
    if not user_profile.get("name"):
        return "name"
    if not user_profile.get("university"):
        return "school"
    if not user_profile.get("career_interests"):
        return "career_interest"

    # Check email connect status
    email_state = personal_facts.get("email_connect", {})
    if not email_state.get("status") or email_state.get("status") == "link_sent":
        return "email_connect"

    # Check needs eval
    need_state = personal_facts.get("frank_need_eval", {})
    if need_state.get("status") != "accepted":
        return "needs_eval"

    # Check value eval
    value_state = personal_facts.get("frank_value_eval", {})
    if value_state.get("status") != "accepted":
        return "value_eval"

    # Check share stage
    if not user_profile.get("is_onboarded"):
        return "share_to_complete"

    return "complete"


async def _generate_response(
    stage: str,
    context: Dict[str, Any],
    user_profile: Dict[str, Any],
    message: str,
    conversation_history: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Generate Frank's response using the detailed prompts."""
    import json

    # Lazy imports to avoid circular dependencies
    from app.agents.interaction.prompts import get_onboarding_response_prompt
    from app.integrations.azure_openai_client import AzureOpenAIClient

    try:
        # Build the response prompt
        prompt = get_onboarding_response_prompt(
            stage=stage,
            context=context,
            user_profile=user_profile,
            message=message,
            conversation_history=conversation_history,
        )

        # Generate response
        # Use gpt-4o for the email_connected roast/joke (needs creativity), gpt-4o-mini for everything else
        action = context.get("action", "")
        model = "gpt-4o" if action == "email_connected" else "gpt-4o-mini"

        openai = AzureOpenAIClient()
        response = await openai.generate_response(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            temperature=0.7,
            max_tokens=500,
            trace_label="onboarding_response_gen",
        )

        response_text = response.strip()

        # Fallback if LLM returned empty
        if not response_text:
            logger.warning("[ONBOARDING] LLM returned empty response, using fallback")
            response_text = "hey! what can i help you with"

        # Handle JSON array format for multi-message responses
        if response_text.startswith("[") and response_text.endswith("]"):
            try:
                messages = json.loads(response_text)
                if isinstance(messages, list) and all(isinstance(m, str) for m in messages):
                    # Filter out empty strings
                    messages = [m for m in messages if m and m.strip()]
                    if messages:
                        return {
                            "response_text": messages[0],
                            "additional_messages": messages[1:] if len(messages) > 1 else [],
                        }
            except json.JSONDecodeError:
                pass

        return {
            "response_text": response_text,
            "additional_messages": [],
        }

    except Exception as e:
        logger.error(f"[ONBOARDING] Response generation failed: {e}", exc_info=True)
        # Return a fallback response
        return {
            "response_text": "hey, one sec",
            "additional_messages": [],
        }
