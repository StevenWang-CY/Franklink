"""
Onboarding Executor - Pure task execution without response generation.

This module provides pure functions for onboarding data operations:
- Extract data from user messages
- Validate extracted data
- Persist to database
- Return structured context for InteractionAgent to generate responses

NO hardcoded response strings - InteractionAgent owns all conversation.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.agents.tools.onboarding.extraction import update_user_profile, extract_onboarding_fields
from app.agents.tools.onboarding.classification import classify_email_connect_reply, classify_share_reply
from app.agents.tools.onboarding.email_context import ensure_email_signals
from app.agents.tools.email_highlights import process_new_email_highlights
from app.agents.tools.onboarding.evaluation import evaluate_user_need, evaluate_user_value
from app.integrations.composio_client import ComposioClient
from app.integrations.photon_client import PhotonClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.database.client import DatabaseClient
from app.utils.context_embedding import update_user_context_embedding
from app.utils.onboarding_summarizer import summarize_onboarding_demand_value
from app.utils.demand_value_derived_fields import update_demand_value_derived_fields
from app.config import settings
from app.reactions.service import ReactionService

logger = logging.getLogger(__name__)

# Legacy hardcoded payment link (replaced by dynamic checkout sessions)
# INTRO_PAYMENT_LINK = "https://buy.stripe.com/9B65kD0N4613ciSeaN5os01"


@dataclass
class OnboardingExecutionResult:
    """Pure data result from onboarding execution."""
    stage_before: str
    stage_after: str
    extracted_fields: Dict[str, Any] = field(default_factory=dict)
    persisted: bool = False
    waiting_for: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    outbound_messages: List[str] = field(default_factory=list)
    should_share_contact_card: bool = False
    should_send_reaction: Optional[str] = None


async def _summarize_and_store_demand_value(
    user_id: str,
    user_profile: Dict[str, Any],
    personal_facts: Dict[str, Any],
    db: DatabaseClient,
) -> None:
    """
    Summarize demand/value from onboarding conversations and store with derived fields.

    Called at onboarding completion to:
    1. LLM summarize demand and value from turn histories
    2. Store as first entries in demand_history/value_history
    3. Update derived fields (all_demand, all_value, latest_demand)
    4. Generate and store embeddings
    """
    from datetime import timezone

    try:
        # Get turn histories from personal_facts
        need_state = personal_facts.get("frank_need_eval", {})
        value_state = personal_facts.get("frank_value_eval", {})

        # Summarize using LLM
        summaries = await summarize_onboarding_demand_value(
            need_turn_history=need_state.get("turn_history", []),
            value_turn_history=value_state.get("turn_history", []),
            user_profile=user_profile,
        )

        now = datetime.now(timezone.utc).isoformat()

        # Create history arrays with summarized entries
        demand_history = []
        if summaries.get("demand_summary"):
            demand_history = [{"text": summaries["demand_summary"], "created_at": now}]

        value_history = []
        if summaries.get("value_summary"):
            value_history = [{"text": summaries["value_summary"], "created_at": now}]

        # Persist histories to database
        await db.update_user_profile(user_id, {
            "demand_history": demand_history,
            "value_history": value_history,
        })

        # Update derived fields and generate embeddings
        await update_demand_value_derived_fields(
            db=db,
            user_id=user_id,
            demand_history=demand_history,
            value_history=value_history,
        )

        logger.info(
            "[ONBOARDING_EXECUTOR] Summarized and stored demand/value for user %s: "
            "demand=%s, value=%s",
            user_id,
            bool(demand_history),
            bool(value_history),
        )

    except Exception as e:
        logger.warning(
            "[ONBOARDING_EXECUTOR] Failed to summarize demand/value for user %s: %s",
            user_id,
            e,
        )


async def _schedule_proactive_jobs(
    user_id: str,
    personal_facts: Dict[str, Any],
    db: DatabaseClient,
) -> None:
    """
    Schedule proactive email and outreach jobs for a newly onboarded user.

    Jobs are only scheduled if user has connected email.

    Args:
        user_id: User ID
        personal_facts: User's personal_facts containing email_connect status
        db: Database client
    """
    try:
        # Check if user has connected email
        email_connect = personal_facts.get("email_connect") or {}
        if email_connect.get("status") != "connected":
            logger.debug(
                "[ONBOARDING_EXECUTOR] Skipping proactive job scheduling - no email connected for user %s",
                user_id,
            )
            return

        # Schedule daily email extraction job
        await db.schedule_daily_email_job_v1(user_id=user_id)
        logger.info("[ONBOARDING_EXECUTOR] Scheduled daily email job for user %s", user_id)

        # Schedule proactive outreach job
        await db.schedule_proactive_outreach_job_v1(user_id=user_id)
        logger.info("[ONBOARDING_EXECUTOR] Scheduled proactive outreach job for user %s", user_id)

    except Exception as e:
        logger.warning(
            "[ONBOARDING_EXECUTOR] Failed to schedule proactive jobs for user %s: %s",
            user_id,
            e,
        )


async def execute_onboarding_stage(
    stage: str,
    message: str,
    user_profile: Dict[str, Any],
    temp_data: Dict[str, Any],
    current_message: Dict[str, Any],
) -> OnboardingExecutionResult:
    """
    Main dispatcher - routes to appropriate stage executor.

    Args:
        stage: Current onboarding stage
        message: User's message content
        user_profile: User profile dict
        temp_data: Temporary data dict (includes extraction results)
        current_message: Current message metadata (for reactions, etc.)

    Returns:
        OnboardingExecutionResult with execution data and context
    """
    executors = {
        "name": execute_name_stage,
        "school": execute_school_stage,
        "career_interest": execute_career_interest_stage,
        "email_connect": execute_email_connect_stage,
        "needs_eval": execute_needs_eval_stage,
        "value_eval": execute_value_eval_stage,
        "share_to_complete": execute_share_stage,
    }

    executor = executors.get(stage)
    if not executor:
        logger.warning(f"[ONBOARDING_EXECUTOR] Unknown stage: {stage}")
        return OnboardingExecutionResult(
            stage_before=stage,
            stage_after=stage,
            waiting_for=None,
            context={"error": f"Unknown stage: {stage}"},
        )

    return await executor(message, user_profile, temp_data, current_message)


async def execute_name_stage(
    message: str,
    user_profile: Dict[str, Any],
    temp_data: Dict[str, Any],
    current_message: Dict[str, Any],
) -> OnboardingExecutionResult:
    """Execute name collection stage - extract name, persist, return context."""
    personal_facts = user_profile.get("personal_facts") or {}
    asked_for_name = personal_facts.get("asked_for_name", False)
    introduced = bool(personal_facts.get("frank_introduced", False))

    # Check if extraction already got the name
    extraction = temp_data.get("onboarding_extraction", {}) or {}
    extracted_name = extraction.get("name")
    name_quality = extraction.get("name_quality", "valid")
    user_intent = extraction.get("user_intent", "answer")

    # Handle greeting instead of name (e.g., "yo", "hey", "hi")
    if name_quality == "greeting" and not extracted_name and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="name",
            stage_after="name",
            waiting_for="user_input",
            context={
                "action": "name_was_greeting",
                "greeting": message.strip(),
                "first_introduction": not introduced,
            },
        )

    # Handle user asking a question instead of giving name
    if user_intent == "question" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="name",
            stage_after="name",
            waiting_for="user_input",
            context={"action": "question_at_name", "question": message.strip()},
        )

    # Handle user expressing concern
    if user_intent == "concern" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="name",
            stage_after="name",
            waiting_for="user_input",
            context={"action": "concern_at_name", "concern": message.strip()},
        )

    # Handle off-topic message (unrelated to onboarding)
    if user_intent == "off_topic" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="name",
            stage_after="name",
            waiting_for="user_input",
            context={
                "action": "off_topic_at_name",
                "off_topic_message": message.strip(),
                "first_introduction": not introduced,
            },
        )

    # Use extracted name if available, otherwise use message
    name = extracted_name or (message.strip() if message else None)

    # First call: need to ask for name
    if not asked_for_name and not user_profile.get("name") and not name:
        personal_facts["asked_for_name"] = True
        if not introduced:
            personal_facts["frank_introduced"] = True
        user_profile["personal_facts"] = personal_facts

        # Build state for persistence
        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        await update_user_profile(state, {"personal_facts": personal_facts})

        return OnboardingExecutionResult(
            stage_before="name",
            stage_after="name",
            persisted=True,
            waiting_for="user_input",
            context={
                "action": "ask_name",
                "first_introduction": not introduced,
            },
        )

    # Re-ask if no input
    if not name:
        return OnboardingExecutionResult(
            stage_before="name",
            stage_after="name",
            waiting_for="user_input",
            context={"action": "reask_name"},
        )

    # Got name - persist and advance
    personal_facts["asked_for_name"] = False
    if not introduced:
        personal_facts["frank_introduced"] = True
    user_profile["personal_facts"] = personal_facts
    user_profile["name"] = name
    user_profile["onboarding_stage"] = "school"

    # Build state for persistence
    from app.models.state import OnboardingState
    state: OnboardingState = {
        "user_profile": user_profile,
        "current_message": current_message,
        "response": {},
        "temp_data": temp_data,
    }
    await update_user_profile(state, {
        "name": name,
        "onboarding_stage": "school",
        "personal_facts": personal_facts,
    })

    return OnboardingExecutionResult(
        stage_before="name",
        stage_after="school",
        extracted_fields={"name": name},
        persisted=True,
        waiting_for="school",
        context={
            "action": "name_collected",
            "name": name,
        },
        should_send_reaction="love",
        should_share_contact_card=True,
    )


async def execute_school_stage(
    message: str,
    user_profile: Dict[str, Any],
    temp_data: Dict[str, Any],
    current_message: Dict[str, Any],
) -> OnboardingExecutionResult:
    """Execute school collection stage."""
    extraction = temp_data.get("onboarding_extraction", {}) or {}
    applied_fields = extraction.get("applied_fields", [])
    source_message = extraction.get("source_message")

    personal_facts = user_profile.get("personal_facts") or {}
    introduced = bool(personal_facts.get("frank_introduced", False))

    # Check for quality and intent
    school_quality = extraction.get("school_quality", "valid")
    detected_name_correction = extraction.get("detected_name_correction")
    user_intent = extraction.get("user_intent", "answer")

    # Handle name correction (user says "call me X" instead of giving school)
    if school_quality == "name_correction" and detected_name_correction:
        # Update the name and re-ask for school
        user_profile["name"] = detected_name_correction
        personal_facts["name_corrected"] = True
        user_profile["personal_facts"] = personal_facts

        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        await update_user_profile(state, {"name": detected_name_correction, "personal_facts": personal_facts})

        return OnboardingExecutionResult(
            stage_before="school",
            stage_after="school",
            extracted_fields={"name": detected_name_correction},
            persisted=True,
            waiting_for="school",
            context={"action": "name_corrected_reask_school", "name": detected_name_correction},
        )

    # Handle user asking a question
    if user_intent == "question" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="school",
            stage_after="school",
            waiting_for="user_input",
            context={"action": "question_at_school", "question": message.strip()},
        )

    # Handle user expressing concern
    if user_intent == "concern" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="school",
            stage_after="school",
            waiting_for="user_input",
            context={"action": "concern_at_school", "concern": message.strip()},
        )

    # Handle off-topic message (unrelated to onboarding)
    if user_intent == "off_topic" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="school",
            stage_after="school",
            waiting_for="user_input",
            context={
                "action": "off_topic_at_school",
                "off_topic_message": message.strip(),
            },
        )

    # Check if extraction already got the school
    extracted_school = extraction.get("school")

    # If message was consumed for other fields but not school, don't reuse
    if source_message and source_message == message and applied_fields and "school" not in applied_fields:
        school = extracted_school
    else:
        school = extracted_school or (message.strip() if message else None)

    # No school provided - ask for it
    if not school:
        if not introduced:
            personal_facts["frank_introduced"] = True
            user_profile["personal_facts"] = personal_facts

            from app.models.state import OnboardingState
            state: OnboardingState = {
                "user_profile": user_profile,
                "current_message": current_message,
                "response": {},
                "temp_data": temp_data,
            }
            await update_user_profile(state, {"personal_facts": personal_facts})

        return OnboardingExecutionResult(
            stage_before="school",
            stage_after="school",
            waiting_for="school",
            context={
                "action": "ask_school",
                "first_introduction": not introduced,
            },
        )

    # Got school - persist and advance
    user_profile["university"] = school
    user_profile["onboarding_stage"] = "career_interest"

    from app.models.state import OnboardingState
    state: OnboardingState = {
        "user_profile": user_profile,
        "current_message": current_message,
        "response": {},
        "temp_data": temp_data,
    }
    await update_user_profile(state, {
        "university": school,
        "onboarding_stage": "career_interest",
    })

    return OnboardingExecutionResult(
        stage_before="school",
        stage_after="career_interest",
        extracted_fields={"university": school},
        persisted=True,
        waiting_for="career_interest",
        context={
            "action": "school_collected",
            "school": school,
        },
    )


async def execute_career_interest_stage(
    message: str,
    user_profile: Dict[str, Any],
    temp_data: Dict[str, Any],
    current_message: Dict[str, Any],
) -> OnboardingExecutionResult:
    """Execute career interest collection stage, then initiate email connect."""
    extraction = temp_data.get("onboarding_extraction", {}) or {}
    extracted_interests = extraction.get("career_interests") or []
    career_quality = extraction.get("career_quality", "valid")
    user_intent = extraction.get("user_intent", "answer")

    # Handle user asking a question
    if user_intent == "question" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="career_interest",
            stage_after="career_interest",
            waiting_for="user_input",
            context={"action": "question_at_career", "question": message.strip()},
        )

    # Handle user expressing concern
    if user_intent == "concern" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="career_interest",
            stage_after="career_interest",
            waiting_for="user_input",
            context={"action": "concern_at_career", "concern": message.strip()},
        )

    # Handle off-topic message (unrelated to onboarding)
    if user_intent == "off_topic" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="career_interest",
            stage_after="career_interest",
            waiting_for="user_input",
            context={
                "action": "off_topic_at_career",
                "off_topic_message": message.strip(),
            },
        )

    # Handle vague career answers (e.g., "money", "success", "rich")
    if career_quality == "too_vague" and message and message.strip():
        return OnboardingExecutionResult(
            stage_before="career_interest",
            stage_after="career_interest",
            waiting_for="user_input",
            context={"action": "career_too_vague", "vague_answer": message.strip()},
        )

    # No interests provided
    if not extracted_interests:
        return OnboardingExecutionResult(
            stage_before="career_interest",
            stage_after="career_interest",
            waiting_for="career_interest",
            context={"action": "ask_career_interest"},
        )

    # Got interests - persist and initiate email connect
    personal_facts = user_profile.get("personal_facts") or {}
    introduced = bool(personal_facts.get("frank_introduced"))
    if not introduced:
        personal_facts["frank_introduced"] = True

    # Initialize email connect state
    email_state = personal_facts.get("email_connect")
    if not isinstance(email_state, dict):
        email_state = {}
    now = datetime.utcnow().isoformat()

    # Try to generate auth link
    user_id = str(user_profile.get("user_id") or "").strip()
    composio = ComposioClient()
    auth_link = await composio.initiate_gmail_connect(user_id=user_id) if user_id else None

    outbound_messages = []
    if auth_link:
        email_state.update({"status": "link_sent", "updated_at": now, "last_link_sent_at": now})
        outbound_messages.append(auth_link)
        link_status = "link_sent"
    else:
        email_state.update({
            "status": "link_error",
            "updated_at": now,
            "last_error_code": composio.get_last_connect_error_code(),
            "last_error_at": now,
        })
        link_status = "link_error"
        logger.warning(
            "[ONBOARDING_EXECUTOR][CAREER_INTEREST] failed to generate auth link (error_code=%s)",
            email_state.get("last_error_code"),
        )

    personal_facts["email_connect"] = email_state
    user_profile["personal_facts"] = personal_facts
    user_profile["career_interests"] = extracted_interests
    user_profile["onboarding_stage"] = "email_connect"
    user_profile["is_onboarded"] = False

    from app.models.state import OnboardingState
    state: OnboardingState = {
        "user_profile": user_profile,
        "current_message": current_message,
        "response": {},
        "temp_data": temp_data,
    }
    await update_user_profile(state, {
        "career_interests": extracted_interests,
        "onboarding_stage": "email_connect",
        "personal_facts": personal_facts,
    })

    return OnboardingExecutionResult(
        stage_before="career_interest",
        stage_after="email_connect",
        extracted_fields={"career_interests": extracted_interests},
        persisted=True,
        waiting_for="email_connect",
        outbound_messages=outbound_messages,
        context={
            "action": "career_interest_collected",
            "interests": extracted_interests,
            "email_link_status": link_status,
            "first_introduction": not introduced,
        },
    )


async def _fetch_and_store_emails_background(
    user_id: str,
    personal_facts: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Fetch and store emails in background without blocking user flow."""
    logger.info("[BACKGROUND] Starting email fetch for user %s", user_id)
    stored_emails: List[Dict[str, Any]] = []
    try:
        await ensure_email_signals(personal_facts=personal_facts, user_id=user_id)
        email_signals = personal_facts.get("email_signals", {})

        logger.info("[BACKGROUND] Email signals status for user %s: %s", user_id, email_signals.get("status"))

        if email_signals.get("status") == "ready":
            emails = email_signals.get("emails", [])
            logger.info("[BACKGROUND] Found %d emails to store for user %s", len(emails), user_id)
            if emails:
                db = DatabaseClient()
                stored_emails = await db.store_user_emails(user_id=user_id, emails=emails)
                logger.info("[BACKGROUND] Stored %d emails for user %s", len(stored_emails), user_id)
            else:
                logger.info("[BACKGROUND] No emails found to store for user %s", user_id)
        else:
            logger.warning("[BACKGROUND] Email signals not ready for user %s: status=%s, error=%s",
                          user_id, email_signals.get("status"), email_signals.get("error"))
    except Exception as exc:
        logger.error("[BACKGROUND] Email fetch failed for user %s: %s", user_id, exc, exc_info=True)
    return stored_emails


async def execute_email_connect_stage(
    message: str,
    user_profile: Dict[str, Any],
    temp_data: Dict[str, Any],
    current_message: Dict[str, Any],
) -> OnboardingExecutionResult:
    """Execute email connection stage."""
    personal_facts = user_profile.get("personal_facts") or {}
    email_state = personal_facts.get("email_connect")
    if not isinstance(email_state, dict):
        email_state = {}

    now = datetime.utcnow().isoformat()
    user_id = str(user_profile.get("user_id") or "").strip()

    # If no status yet, initiate connection
    if not email_state.get("status"):
        composio = ComposioClient()
        auth_link = await composio.initiate_gmail_connect(user_id=user_id) if user_id else None

        outbound_messages = []
        if auth_link:
            email_state.update({"status": "link_sent", "updated_at": now, "last_link_sent_at": now})
            outbound_messages.append(auth_link)
            link_status = "link_sent"
        else:
            email_state.update({
                "status": "link_error",
                "updated_at": now,
                "last_error_code": composio.get_last_connect_error_code(),
                "last_error_at": now,
            })
            link_status = "link_error"

        personal_facts["email_connect"] = email_state
        user_profile["personal_facts"] = personal_facts
        user_profile["onboarding_stage"] = "email_connect"
        user_profile["is_onboarded"] = False

        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        await update_user_profile(state, {
            "onboarding_stage": "email_connect",
            "personal_facts": personal_facts,
        })

        return OnboardingExecutionResult(
            stage_before="email_connect",
            stage_after="email_connect",
            persisted=True,
            waiting_for="email_connect",
            outbound_messages=outbound_messages,
            context={
                "action": "email_connect_initiated",
                "link_status": link_status,
            },
        )

    # Classify user's reply
    try:
        result = await classify_email_connect_reply(
            message=message,
            user_profile=user_profile,
            email_state=email_state,
        )
        decision = str(result.get("decision") or "").strip().lower()
    except Exception as e:
        logger.warning("[ONBOARDING_EXECUTOR][EMAIL_CONNECT] classification failed: %s", e)
        decision = "reask"

    # Guardrail: don't accept "connected" if we never sent a link
    def _can_accept_connected(es: dict) -> bool:
        if not isinstance(es, dict):
            return False
        status = str(es.get("status") or "").strip().lower()
        return status in {"connected", "link_sent"} or bool(es.get("last_link_sent_at"))

    if decision == "connected" and not _can_accept_connected(email_state):
        decision = "connect"

    if decision == "connected":
        # Verify connection via Composio API before accepting user's claim
        composio = ComposioClient()
        is_actually_connected = False
        if user_id:
            try:
                is_actually_connected = await composio.verify_gmail_connection(user_id=user_id)
            except Exception as e:
                logger.warning("[ONBOARDING_EXECUTOR] connection verification failed: %s", e)

        if not is_actually_connected:
            # User claims connected but Composio shows no active connection
            logger.info("[ONBOARDING_EXECUTOR] user claims connected but no active Gmail connection found")
            return OnboardingExecutionResult(
                stage_before="email_connect",
                stage_after="email_connect",
                persisted=False,
                waiting_for="email_connect",
                context={
                    "action": "connection_not_verified",
                    "reason": "no_active_connection",
                },
            )

        # Connection verified - advance to needs_eval
        email_state.update({"status": "connected", "updated_at": now})
        personal_facts["email_connect"] = email_state

        # Fetch and store emails FIRST, then analyze them
        # We need to wait for emails to be in the DB before LLM can analyze them
        connected_email = None
        stored_emails: List[Dict[str, Any]] = []
        sent_email_insights: Dict[str, Any] = {}
        if user_id:
            try:
                stored_emails = await _fetch_and_store_emails_background(
                    user_id=user_id,
                    personal_facts=personal_facts,
                )
            except Exception:
                logger.debug("[ONBOARDING_EXECUTOR] failed to fetch/store emails", exc_info=True)

            # Sync emails to Zep immediately so knowledge graph is available for networking
            # This is critical - without it, suggest_connection_purposes() returns empty
            if stored_emails:
                try:
                    from app.agents.tools.email_zep_sync import sync_emails_to_zep

                    zep_result = await sync_emails_to_zep(
                        user_id=user_id,
                        emails=stored_emails,
                        mark_synced=True,
                    )
                    logger.info(
                        "[ONBOARDING_EXECUTOR] synced %d emails to Zep for user %s",
                        zep_result.get("emails_synced", 0),
                        user_id[:8] if user_id else "unknown",
                    )
                except Exception as e:
                    logger.warning("[ONBOARDING_EXECUTOR] Zep sync failed: %s", e)

            # Analyze emails for immediate response (so Frank can reference specific details)
            # This MUST happen AFTER emails are stored in the database
            try:
                from app.agents.tools.onboarding.email_context import analyze_sent_emails_with_llm
                sent_email_insights = await analyze_sent_emails_with_llm(user_id)
                logger.info("[EMAIL_CONNECT] LLM email insights: %s", sent_email_insights)
            except Exception as e:
                logger.debug("[EMAIL_CONNECT] Failed to analyze sent emails: %s", e)

            try:
                connected_email = await composio.get_connected_gmail_address(user_id=user_id)
            except Exception:
                logger.debug("[ONBOARDING_EXECUTOR] failed to resolve connected email", exc_info=True)

        highlight_rows: List[Dict[str, Any]] = []
        if stored_emails:
            try:
                profile_for_highlights = dict(user_profile or {})
                if connected_email:
                    profile_for_highlights["email"] = connected_email
                highlight_result = await process_new_email_highlights(
                    user_id=user_id,
                    emails=stored_emails,
                    user_profile=profile_for_highlights,
                )
                highlight_rows = list(highlight_result.get("highlights") or [])
                # Sync new highlights to Zep knowledge graph
                # (Zep now powers signal extraction, replacing the old intent_events flow)
                if highlight_rows:
                    try:
                        from app.agents.tools.email_zep_sync import sync_unsynced_highlights_to_zep
                        await sync_unsynced_highlights_to_zep(user_id=user_id)
                    except Exception:
                        logger.debug("[ONBOARDING_EXECUTOR] failed to sync highlights to Zep", exc_info=True)
            except Exception:
                logger.error("[ONBOARDING_EXECUTOR] failed to store email highlights", exc_info=True)

        # Seed needs eval state
        from app.agents.tools.onboarding.evaluation import build_initial_need_prompt, seed_need_state
        first_prompt = await build_initial_need_prompt(user_profile=user_profile)
        need_state = seed_need_state(first_prompt=first_prompt, prior_state=personal_facts.get("frank_need_eval"))
        personal_facts["frank_need_eval"] = need_state

        user_profile["personal_facts"] = personal_facts
        user_profile["onboarding_stage"] = "needs_eval"
        user_profile["is_onboarded"] = False
        if connected_email:
            user_profile["email"] = connected_email

        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        update_payload = {
            "onboarding_stage": "needs_eval",
            "personal_facts": personal_facts,
        }
        if connected_email:
            update_payload["email"] = connected_email
        await update_user_profile(state, update_payload)

        return OnboardingExecutionResult(
            stage_before="email_connect",
            stage_after="needs_eval",
            persisted=True,
            waiting_for="user_input",
            context={
                "action": "email_connected",
                "initial_need_prompt": first_prompt,
                "sent_email_insights": sent_email_insights,
            },
        )

    if decision == "connect":
        # User wants to connect - send new link
        composio = ComposioClient()
        auth_link = await composio.initiate_gmail_connect(user_id=user_id) if user_id else None

        outbound_messages = []
        if auth_link:
            email_state.update({"status": "link_sent", "updated_at": now, "last_link_sent_at": now})
            outbound_messages.append(auth_link)
            link_status = "link_sent"
        else:
            email_state.update({
                "status": "link_error",
                "updated_at": now,
                "last_error_code": composio.get_last_connect_error_code(),
                "last_error_at": now,
            })
            link_status = "link_error"

        personal_facts["email_connect"] = email_state
        user_profile["personal_facts"] = personal_facts

        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        await update_user_profile(state, {
            "onboarding_stage": "email_connect",
            "personal_facts": personal_facts,
        })

        return OnboardingExecutionResult(
            stage_before="email_connect",
            stage_after="email_connect",
            persisted=True,
            waiting_for="email_connect",
            outbound_messages=outbound_messages,
            context={
                "action": "email_link_resent",
                "link_status": link_status,
            },
        )

    # User asking a question about email connect
    if decision == "question":
        return OnboardingExecutionResult(
            stage_before="email_connect",
            stage_after="email_connect",
            waiting_for="user_input",
            context={
                "action": "email_question_answered",
                "question": message,
            },
        )

    # User expressing concern about email connect
    if decision == "concern":
        return OnboardingExecutionResult(
            stage_before="email_connect",
            stage_after="email_connect",
            waiting_for="user_input",
            context={
                "action": "email_concern_addressed",
                "concern": message,
            },
        )

    # Decline or unclear - reask
    email_state.update({
        "status": "declined" if decision == "decline" else "prompted",
        "updated_at": now,
    })
    personal_facts["email_connect"] = email_state
    user_profile["personal_facts"] = personal_facts

    from app.models.state import OnboardingState
    state: OnboardingState = {
        "user_profile": user_profile,
        "current_message": current_message,
        "response": {},
        "temp_data": temp_data,
    }
    await update_user_profile(state, {
        "onboarding_stage": "email_connect",
        "personal_facts": personal_facts,
    })

    return OnboardingExecutionResult(
        stage_before="email_connect",
        stage_after="email_connect",
        persisted=True,
        waiting_for="email_connect",
        context={
            "action": "email_connect_reask",
            "user_decision": decision,
        },
    )


async def execute_needs_eval_stage(
    message: str,
    user_profile: Dict[str, Any],
    temp_data: Dict[str, Any],
    current_message: Dict[str, Any],
) -> OnboardingExecutionResult:
    """Execute needs evaluation stage."""
    personal_facts = user_profile.get("personal_facts") or {}
    need_state = personal_facts.get("frank_need_eval")
    if not isinstance(need_state, dict):
        need_state = {}

    # Ensure email context is loaded (fallback to DB if not in memory)
    user_id = str(user_profile.get("user_id") or "").strip()
    email_signals = personal_facts.get("email_signals", {})
    if user_id and not email_signals.get("emails"):
        try:
            db = DatabaseClient()
            stored_emails = await db.get_user_emails(user_id, limit=50)
            if stored_emails:
                personal_facts["email_signals"] = {
                    "status": "ready",
                    "emails": stored_emails,
                    "summary": f"loaded {len(stored_emails)} emails from cache",
                }
                user_profile["personal_facts"] = personal_facts
                logger.debug("[NEEDS_EVAL] Loaded %d emails from DB for user %s", len(stored_emails), user_id)
        except Exception as e:
            logger.debug("[NEEDS_EVAL] Failed to load emails from DB: %s", e)

    # Analyze sent emails for professional needs/value signals using LLM
    # Only fetch and use email insights if we haven't already referenced them
    email_context_used = need_state.get("email_context_used", False)
    sent_email_insights = {}
    if user_id and not email_context_used:
        try:
            from app.agents.tools.onboarding.email_context import analyze_sent_emails_with_llm
            sent_email_insights = await analyze_sent_emails_with_llm(user_id)
            logger.debug("[NEEDS_EVAL] LLM email insights: %s", sent_email_insights)
        except Exception as e:
            logger.debug("[NEEDS_EVAL] Failed to analyze sent emails: %s", e)
    elif email_context_used:
        logger.debug("[NEEDS_EVAL] Skipping email insights - already used in conversation")

    # Build conversation history for evaluation
    turn_history = need_state.get("turn_history", [])

    # Add user's message to history
    turn_history.append({"role": "user", "content": message})

    # Run evaluation
    try:
        eval_result = await evaluate_user_need(
            user_message=message,
            user_profile=user_profile,
            prior_state=need_state,
        )
    except Exception as e:
        logger.error("[ONBOARDING_EXECUTOR][NEEDS_EVAL] evaluation failed: %s", e)
        return OnboardingExecutionResult(
            stage_before="needs_eval",
            stage_after="needs_eval",
            waiting_for="user_input",
            context={
                "action": "needs_eval_error",
                "error": str(e),
            },
        )

    decision = eval_result.get("decision", "ask")

    if decision == "accept":
        # Needs accepted - advance to value_eval
        user_need = eval_result.get("user_need", {})

        # Update need state
        need_state["status"] = "accepted"
        need_state["user_need"] = user_need
        need_state["turn_history"] = turn_history
        personal_facts["frank_need_eval"] = need_state

        # Seed value eval state
        from app.agents.tools.onboarding.evaluation import build_initial_gate_prompt, seed_value_state
        phone_number = str(user_profile.get("phone_number") or "").strip()
        first_prompt = await build_initial_gate_prompt(phone_number=phone_number, user_profile=user_profile)
        value_state = seed_value_state(first_prompt=first_prompt, prior_state=personal_facts.get("frank_value_eval"))
        personal_facts["frank_value_eval"] = value_state

        user_profile["personal_facts"] = personal_facts
        user_profile["onboarding_stage"] = "value_eval"

        # Note: demand_history is populated at onboarding completion via LLM summarization
        # The user_need object is stored in personal_facts for now

        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        await update_user_profile(state, {
            "onboarding_stage": "value_eval",
            "personal_facts": personal_facts,
        })

        return OnboardingExecutionResult(
            stage_before="needs_eval",
            stage_after="value_eval",
            extracted_fields={"user_need": user_need},
            persisted=True,
            waiting_for="user_input",
            context={
                "action": "needs_accepted",
                "user_need": user_need,
                "initial_value_prompt": first_prompt,
                "eval_result": eval_result,
                "sent_email_insights": sent_email_insights,
            },
        )

    # Continue asking
    question = eval_result.get("question", "")
    question_type = eval_result.get("question_type", "")

    # Update turn history with Frank's question
    turn_history.append({"role": "frank", "content": question})
    need_state["turn_history"] = turn_history
    need_state["asked_questions"] = need_state.get("asked_questions", []) + [question_type]
    # Mark email context as used after first response so we don't repeat it
    if sent_email_insights:
        need_state["email_context_used"] = True
    personal_facts["frank_need_eval"] = need_state
    user_profile["personal_facts"] = personal_facts

    from app.models.state import OnboardingState
    state: OnboardingState = {
        "user_profile": user_profile,
        "current_message": current_message,
        "response": {},
        "temp_data": temp_data,
    }
    await update_user_profile(state, {"personal_facts": personal_facts})

    return OnboardingExecutionResult(
        stage_before="needs_eval",
        stage_after="needs_eval",
        persisted=True,
        waiting_for="user_input",
        context={
            "action": "needs_asking",
            "question": question,
            "question_type": question_type,
            "eval_result": eval_result,
            "sent_email_insights": sent_email_insights,
        },
    )


async def execute_value_eval_stage(
    message: str,
    user_profile: Dict[str, Any],
    temp_data: Dict[str, Any],
    current_message: Dict[str, Any],
) -> OnboardingExecutionResult:
    """Execute value evaluation stage."""
    personal_facts = user_profile.get("personal_facts") or {}
    value_state = personal_facts.get("frank_value_eval")
    if not isinstance(value_state, dict):
        value_state = {}

    # Ensure email context is loaded (fallback to DB if not in memory)
    user_id = str(user_profile.get("user_id") or "").strip()
    email_signals = personal_facts.get("email_signals", {})
    if user_id and not email_signals.get("emails"):
        try:
            db = DatabaseClient()
            stored_emails = await db.get_user_emails(user_id, limit=50)
            if stored_emails:
                personal_facts["email_signals"] = {
                    "status": "ready",
                    "emails": stored_emails,
                    "summary": f"loaded {len(stored_emails)} emails from cache",
                }
                user_profile["personal_facts"] = personal_facts
                logger.debug("[VALUE_EVAL] Loaded %d emails from DB for user %s", len(stored_emails), user_id)
        except Exception as e:
            logger.debug("[VALUE_EVAL] Failed to load emails from DB: %s", e)

    # Analyze sent emails for professional needs/value signals using LLM
    # Only fetch and use email insights if we haven't already referenced them
    email_context_used = value_state.get("email_context_used", False)
    sent_email_insights = {}
    if user_id and not email_context_used:
        try:
            from app.agents.tools.onboarding.email_context import analyze_sent_emails_with_llm
            sent_email_insights = await analyze_sent_emails_with_llm(user_id)
            logger.debug("[VALUE_EVAL] LLM email insights: %s", sent_email_insights)
        except Exception as e:
            logger.debug("[VALUE_EVAL] Failed to analyze sent emails: %s", e)
    elif email_context_used:
        logger.debug("[VALUE_EVAL] Skipping email insights - already used in conversation")

    turn_history = value_state.get("turn_history", [])

    # Check if user is asking a question instead of answering about their value
    # Questions don't count as turns and don't progress the fee ladder
    from app.agents.tools.onboarding.extraction import extract_onboarding_fields
    try:
        extraction = await extract_onboarding_fields(
            message=message, stage="value_eval", user_profile=user_profile
        )
        user_intent = extraction.get("user_intent", "answer")
    except Exception as e:
        logger.debug("[VALUE_EVAL] Failed to extract user intent: %s", e)
        user_intent = "answer"  # Default to treating as answer if extraction fails

    if user_intent == "question" and message and message.strip():
        # User asked a question - address it without progressing the evaluation
        # Don't add to turn_history since questions don't count as turns
        intro_fee_cents = value_state.get("intro_fee_cents", 999)  # Default $9.99
        return OnboardingExecutionResult(
            stage_before="value_eval",
            stage_after="value_eval",
            waiting_for="user_input",
            context={
                "action": "question_at_value_eval",
                "question": message.strip(),
                "intro_fee_cents": intro_fee_cents,
            },
        )

    # Not a question - add to turn history and proceed with evaluation
    turn_history.append({"role": "user", "content": message})

    # Get phone number for evaluation
    phone_number = str(user_profile.get("phone_number") or "").strip()

    # Run evaluation
    try:
        eval_result = await evaluate_user_value(
            phone_number=phone_number,
            user_message=message,
            user_profile=user_profile,
            prior_state=value_state,
        )
    except Exception as e:
        logger.error("[ONBOARDING_EXECUTOR][VALUE_EVAL] evaluation failed: %s", e)
        return OnboardingExecutionResult(
            stage_before="value_eval",
            stage_after="value_eval",
            waiting_for="user_input",
            context={
                "action": "value_eval_error",
                "error": str(e),
            },
        )

    decision = eval_result.get("decision", "ask")

    if decision == "accept":
        # Value accepted - advance to share_to_complete
        user_value = eval_result.get("user_value", {})
        intro_fee_cents = eval_result.get("intro_fee_cents", 99)  # Default $0.99 for accepted users

        value_state["status"] = "accepted"
        value_state["user_value"] = user_value
        value_state["turn_history"] = turn_history
        value_state["intro_fee_cents"] = intro_fee_cents
        personal_facts["frank_value_eval"] = value_state

        user_profile["personal_facts"] = personal_facts
        user_profile["onboarding_stage"] = "share_to_complete"
        user_profile["intro_fee_cents"] = intro_fee_cents

        # Note: value_history is populated at onboarding completion via LLM summarization
        # The user_value object is stored in personal_facts for now

        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        await update_user_profile(state, {
            "onboarding_stage": "share_to_complete",
            "personal_facts": personal_facts,
            "intro_fee_cents": intro_fee_cents,
        })

        return OnboardingExecutionResult(
            stage_before="value_eval",
            stage_after="share_to_complete",
            extracted_fields={"user_value": user_value, "intro_fee_cents": intro_fee_cents},
            persisted=True,
            waiting_for="user_input",
            context={
                "action": "value_accepted",
                "user_value": user_value,
                "intro_fee_cents": intro_fee_cents,
                "signals": eval_result.get("signals", {}),
                "eval_result": eval_result,
                "sent_email_insights": sent_email_insights,
            },
        )

    if decision == "reject":
        # User rejected - set stage to rejected
        value_state["status"] = "rejected"
        value_state["turn_history"] = turn_history
        personal_facts["frank_value_eval"] = value_state

        user_profile["personal_facts"] = personal_facts
        user_profile["onboarding_stage"] = "rejected"

        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        await update_user_profile(state, {
            "onboarding_stage": "rejected",
            "personal_facts": personal_facts,
        })

        return OnboardingExecutionResult(
            stage_before="value_eval",
            stage_after="rejected",
            persisted=True,
            waiting_for=None,
            context={
                "action": "value_rejected",
                "rejection_reason": eval_result.get("rejection_reason", ""),
                "eval_result": eval_result,
            },
        )

    # Continue asking - extract new context fields from eval_result
    question = eval_result.get("question", "")
    question_type = eval_result.get("question_type", "")
    signals = eval_result.get("signals", {})
    intro_fee_cents = eval_result.get("intro_fee_cents") or value_state.get("intro_fee_cents", 999)
    turn_number = eval_result.get("turn_number", 1)
    last_response_score = eval_result.get("last_response_score", 5)
    cumulative_score = eval_result.get("cumulative_score", 0)
    score_history = eval_result.get("score_history", [])
    extracted_claims = eval_result.get("extracted_claims", [])

    turn_history.append({"role": "frank", "content": question})
    value_state["turn_history"] = turn_history
    value_state["asked_questions"] = value_state.get("asked_questions", []) + [question_type]
    value_state["signals"] = signals
    value_state["intro_fee_cents"] = intro_fee_cents  # Persist fee between turns
    value_state["score_history"] = score_history  # Track scores for each turn
    value_state["cumulative_score"] = cumulative_score  # Running total
    value_state["extracted_claims"] = extracted_claims  # Claims they've made
    # Mark email context as used after first response so we don't repeat it
    if sent_email_insights:
        value_state["email_context_used"] = True
    personal_facts["frank_value_eval"] = value_state
    user_profile["personal_facts"] = personal_facts

    from app.models.state import OnboardingState
    state: OnboardingState = {
        "user_profile": user_profile,
        "current_message": current_message,
        "response": {},
        "temp_data": temp_data,
    }
    await update_user_profile(state, {"personal_facts": personal_facts})

    return OnboardingExecutionResult(
        stage_before="value_eval",
        stage_after="value_eval",
        persisted=True,
        waiting_for="user_input",
        context={
            "action": "value_asking",
            "question": question,
            "question_type": question_type,
            "signals": signals,
            "eval_result": eval_result,
            "intro_fee_cents": intro_fee_cents,
            "sent_email_insights": sent_email_insights,
            # New context fields for psychology-based prompts
            "turn_number": turn_number,
            "last_response_score": last_response_score,
            "extracted_claims": extracted_claims,
        },
    )


async def execute_share_stage(
    message: str,
    user_profile: Dict[str, Any],
    temp_data: Dict[str, Any],
    current_message: Dict[str, Any],
) -> OnboardingExecutionResult:
    """Execute share-to-complete stage using LLM classification."""
    # classify_share_reply is imported at the top from classification.py

    # Check for media attachment - media_url is stored in metadata dict
    metadata = current_message.get("metadata") or {}
    media_url = metadata.get("media_url") or current_message.get("media_url")
    has_media = bool(media_url)

    personal_facts = user_profile.get("personal_facts") or {}
    value_state = personal_facts.get("frank_value_eval", {})
    intro_fee_cents = value_state.get("intro_fee_cents", user_profile.get("intro_fee_cents", 99))

    logger.info(f"[ONBOARDING_EXECUTOR][SHARE] has_media={has_media}, media_url={media_url}")

    # Use LLM to classify the user's reply
    try:
        classification = await classify_share_reply(
            message=message,
            user_profile=user_profile,
            has_media=has_media,
        )
        decision = classification.get("decision", "unclear")
        logger.info(f"[ONBOARDING_EXECUTOR][SHARE] LLM classification: {decision} (confidence={classification.get('confidence')})")
    except Exception as e:
        logger.warning(f"[ONBOARDING_EXECUTOR][SHARE] Classification failed: {e}, defaulting to waiting")
        decision = "unclear"

    if decision == "shared":
        # User shared screenshot - complete onboarding with $0 fee
        user_profile["is_onboarded"] = True
        user_profile["onboarding_stage"] = "complete"
        user_profile["intro_fee_cents"] = 0  # Reward for sharing

        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        await update_user_profile(state, {
            "is_onboarded": True,
            "onboarding_stage": "complete",
            "intro_fee_cents": 0,
        })

        # Generate embeddings and derived fields for networking matching
        user_id = str(user_profile.get("user_id") or "").strip()
        if user_id:
            db = DatabaseClient()
            openai_client = AzureOpenAIClient()

            # Generate context embedding
            try:
                await update_user_context_embedding(
                    user_id=user_id,
                    profile=user_profile,
                    db_client=db,
                    openai_client=openai_client,
                )
            except Exception as e:
                logger.warning(f"[ONBOARDING_EXECUTOR][SHARE] Failed to generate context embedding: {e}")

            # Summarize and store demand/value with derived fields and embeddings
            await _summarize_and_store_demand_value(
                user_id=user_id,
                user_profile=user_profile,
                personal_facts=personal_facts,
                db=db,
            )

            # Sync user profile to Zep knowledge graph for better context retrieval
            try:
                from app.agents.tools.email_zep_sync import sync_profile_to_zep

                await sync_profile_to_zep(user_id=user_id, profile=user_profile)
                logger.info(
                    "[ONBOARDING_EXECUTOR][SHARE] Synced profile to Zep for user %s",
                    user_id[:8] if user_id else "unknown",
                )
            except Exception as e:
                logger.warning(f"[ONBOARDING_EXECUTOR][SHARE] Failed to sync profile to Zep: {e}")

            # Schedule proactive email and outreach jobs
            await _schedule_proactive_jobs(
                user_id=user_id,
                personal_facts=personal_facts,
                db=db,
            )

        return OnboardingExecutionResult(
            stage_before="share_to_complete",
            stage_after="complete",
            persisted=True,
            waiting_for=None,
            context={
                "action": "shared_and_completed",
                "shared_screenshot": has_media,
                "original_fee_cents": intro_fee_cents,
                "final_fee_cents": 0,
            },
        )

    if decision == "skip":
        # User skipping - complete with original fee and send dynamic payment link
        user_profile["is_onboarded"] = True
        user_profile["onboarding_stage"] = "complete"

        from app.models.state import OnboardingState
        state: OnboardingState = {
            "user_profile": user_profile,
            "current_message": current_message,
            "response": {},
            "temp_data": temp_data,
        }
        await update_user_profile(state, {
            "is_onboarded": True,
            "onboarding_stage": "complete",
        })

        # Generate embeddings and derived fields for networking matching
        user_id = str(user_profile.get("user_id") or "").strip()
        if user_id:
            db = DatabaseClient()
            openai_client = AzureOpenAIClient()

            # Generate context embedding
            try:
                await update_user_context_embedding(
                    user_id=user_id,
                    profile=user_profile,
                    db_client=db,
                    openai_client=openai_client,
                )
            except Exception as e:
                logger.warning(f"[ONBOARDING_EXECUTOR][SHARE] Failed to generate context embedding: {e}")

            # Summarize and store demand/value with derived fields and embeddings
            await _summarize_and_store_demand_value(
                user_id=user_id,
                user_profile=user_profile,
                personal_facts=personal_facts,
                db=db,
            )

            # Sync user profile to Zep knowledge graph for better context retrieval
            try:
                from app.agents.tools.email_zep_sync import sync_profile_to_zep

                await sync_profile_to_zep(user_id=user_id, profile=user_profile)
                logger.info(
                    "[ONBOARDING_EXECUTOR][SKIP] Synced profile to Zep for user %s",
                    user_id[:8] if user_id else "unknown",
                )
            except Exception as e:
                logger.warning(f"[ONBOARDING_EXECUTOR][SKIP] Failed to sync profile to Zep: {e}")

            # Schedule proactive email and outreach jobs
            await _schedule_proactive_jobs(
                user_id=user_id,
                personal_facts=personal_facts,
                db=db,
            )

        # Generate dynamic Stripe Checkout Session with negotiated fee
        payment_link = None
        if intro_fee_cents > 0:
            try:
                from app.integrations.stripe_client import StripeClient
                stripe_client = StripeClient()

                payment_link = await stripe_client.create_intro_checkout_session(
                    user_id=str(user_profile.get("user_id") or user_profile.get("id") or ""),
                    phone_number=str(user_profile.get("phone_number") or ""),
                    intro_fee_cents=intro_fee_cents,
                    email=user_profile.get("email"),
                )
                logger.info(f"[ONBOARDING_EXECUTOR][SHARE] Generated payment link: {payment_link}")
            except Exception as e:
                logger.error(f"[ONBOARDING_EXECUTOR][SHARE] Failed to create checkout session: {e}")

        outbound_messages = [payment_link] if payment_link else []

        return OnboardingExecutionResult(
            stage_before="share_to_complete",
            stage_after="complete",
            persisted=True,
            waiting_for=None,
            outbound_messages=outbound_messages,
            context={
                "action": "skipped_share",
                "intro_fee_cents": intro_fee_cents,
                "payment_link": payment_link,
                "payment_link_generated": bool(payment_link),
            },
        )

    if decision == "question":
        # User has a question about sharing/fee
        return OnboardingExecutionResult(
            stage_before="share_to_complete",
            stage_after="share_to_complete",
            waiting_for="user_input",
            context={
                "action": "share_question_asked",
                "intro_fee_cents": intro_fee_cents,
            },
        )

    if decision == "intent":
        # User expressed intent to share but hasn't sent the screenshot yet
        return OnboardingExecutionResult(
            stage_before="share_to_complete",
            stage_after="share_to_complete",
            waiting_for="user_input",
            context={
                "action": "intent_to_share",
                "intro_fee_cents": intro_fee_cents,
            },
        )

    # Still waiting for share (unclear response)
    return OnboardingExecutionResult(
        stage_before="share_to_complete",
        stage_after="share_to_complete",
        waiting_for="user_input",
        context={
            "action": "waiting_for_share",
            "intro_fee_cents": intro_fee_cents,
        },
    )


async def send_reaction_if_needed(
    result: OnboardingExecutionResult,
    current_message: Dict[str, Any],
) -> None:
    """Send a tapback reaction if the result indicates one should be sent."""
    if not result.should_send_reaction:
        return

    message_guid = current_message.get("message_id")
    if not message_guid:
        return

    try:
        photon = PhotonClient(
            server_url=settings.photon_server_url,
            default_number=settings.photon_default_number,
        )
        await ReactionService(photon=photon).maybe_send_reaction(
            to_number=current_message.get("from_number"),
            message_guid=message_guid,
            message_content=current_message.get("content", ""),
            chat_guid=current_message.get("chat_guid"),
            forced_reaction=result.should_send_reaction,
            context={"task": "onboarding"},
        )
    except Exception as e:
        logger.debug(f"[ONBOARDING_EXECUTOR] Reaction send failed: {e}")


async def share_contact_card_if_needed(
    result: OnboardingExecutionResult,
    current_message: Dict[str, Any],
) -> None:
    """Share contact card if the result indicates it should be shared."""
    if not result.should_share_contact_card:
        return

    chat_guid = current_message.get("chat_guid")
    if not chat_guid:
        return

    try:
        photon = PhotonClient(
            server_url=settings.photon_server_url,
            default_number=settings.photon_default_number,
            api_key=settings.photon_api_key,
        )
        await photon.share_contact_card(chat_guid)
        logger.info(f"[ONBOARDING_EXECUTOR] Contact card shared successfully")
    except Exception as e:
        logger.warning(f"[ONBOARDING_EXECUTOR] Contact card sharing failed: {e}")
