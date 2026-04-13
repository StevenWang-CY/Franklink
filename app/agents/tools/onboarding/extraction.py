"""
Onboarding Extraction Utilities

Handles LLM-based extraction of profile fields from user messages.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.integrations.azure_openai_client import AzureOpenAIClient
from app.database.client import DatabaseClient

logger = logging.getLogger(__name__)


def _normalize_interests(raw: Any) -> List[str]:
    """
    Normalize career interests to a clean list of strings.
    Accepts list or string; splits on commas and slashes.
    """
    if raw is None:
        return []

    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]

    text = str(raw)
    # Split on commas or slashes
    parts = re.split(r"[,/]", text)
    return [p.strip() for p in parts if p.strip()]


async def extract_onboarding_fields(
    message: str,
    history: Optional[List[Dict[str, Any]]] = None,
    profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Extract name, school, and career_interests from a free-form user message.

    Returns a dict with keys: name, school, career_interests, confidence, needs_general.
    """
    if not message or not message.strip():
        return {}

    history = history or []
    profile = profile or {}

    # Build a compact conversation history string (last few messages)
    history_lines = []
    for msg in history[-6:]:
        role = msg.get("role") or msg.get("message_type") or "user"
        content = msg.get("content", "")
        if content:
            history_lines.append(f"{role}: {content}")
    history_str = "\n".join(history_lines) if history_lines else "none"

    system_prompt = (
        "You extract onboarding details (name, school, career interests) from the USER's latest message. "
        "Use only the USER's words; do not invent new facts. "
        "Normalize school names (expand abbreviations like MIT -> Massachusetts Institute of Technology). "
        "CAREER_INTERESTS RULES: only include career fields/industries/roles (e.g., software engineering, product management, finance, tech). "
        "NEVER include a school/university/location in career_interests. If the text is only a school (e.g., 'Penn', 'UCLA'), leave career_interests empty. "
        "Normalize career interests into full terms: e.g. 'CS' -> 'computer scientist', 'SWE' -> 'software engineer', 'eng' -> 'engineer'. "
        "Good career interest examples: ['software engineer', 'data scientist', 'financial analyst', 'product manager', 'investment banking', 'consulting']. "
        "Split multiple interests on commas or slashes. "
        "\n"
        "QUALITY VALIDATION RULES:\n"
        "- name_quality: 'valid' (real name), 'greeting' (just hi/hey/yo/sup/hello alone), 'too_short' (1-2 chars like 'J'), 'unclear' (can't determine)\n"
        "- school_quality: 'valid' (real school), 'not_a_school' (clearly not a school), 'name_correction' (user says 'call me X', 'my name is X', 'i'm X' - they're correcting their name, not giving school), 'unclear'\n"
        "- career_quality: 'valid' (specific industry/role), 'too_vague' (just 'money', 'success', 'rich', 'networking' without specifics), 'unclear'\n"
        "- If school_quality is 'name_correction', extract the corrected name into 'detected_name_correction'\n"
        "\n"
        "USER INTENT DETECTION:\n"
        "- user_intent: 'answer' (providing requested info), 'question' (asking something - ends with ? or starts with why/what/how/can), 'concern' (expressing worry/distrust - words like sketchy, trust, safe, privacy, spam, scam), 'off_topic' (unrelated to onboarding)\n"
        "\n"
        "needs_general=1 means the user mentioned additional content/information/questions that should be answered by the general assistant. Here are some examples of general questions: "
        "- 'What are your main features?' "
        "- 'How do you handle user data?' "
        "- 'I'm interested in cycling' "
        "- 'I love tea.' "
        "- 'Tell me a joke' "
        "If the user only say 'Hi' or 'Hello' or similar greetings, which don't need a response, set needs_general=0. "
        "\n"
        "Return JSON only with keys: name, name_quality, school, school_quality, detected_name_correction, career_interests (array), career_quality, user_intent, confidence (0-1), needs_general (0 or 1). "
    )

    # Get current onboarding stage from profile for context
    current_stage = profile.get("onboarding_stage") or profile.get("personal_facts", {}).get("onboarding_stage") or "unknown"

    user_prompt = f"""
CONVERSATION HISTORY (recent):
{history_str}

PROFILE CONTEXT (may be empty):
{json.dumps({k: v for k, v in profile.items() if k in ['name', 'university', 'career_interests']}, ensure_ascii=False)}

CURRENT ONBOARDING STAGE: {current_stage}
(Use this to understand what Frank just asked - e.g., if stage is 'school', Frank asked for school)

LATEST USER MESSAGE TO PARSE:
{message}

Return JSON only:
{{"name": <string|null>, "name_quality": "valid|greeting|too_short|unclear", "school": <string|null>, "school_quality": "valid|not_a_school|name_correction|unclear", "detected_name_correction": <string|null>, "career_interests": <array>, "career_quality": "valid|too_vague|unclear", "user_intent": "answer|question|concern|off_topic", "confidence": <0-1 float>, "needs_general": <0 or 1>}}
""".strip()

    try:
        openai = AzureOpenAIClient()
        response = await openai.generate_response(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model="gpt-4o-mini",
            temperature=0.2,
            trace_label="onboarding_extraction",
        )

        cleaned = response.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        data = json.loads(cleaned)
        result = {
            "name": data.get("name"),
            "name_quality": data.get("name_quality", "unclear"),
            "school": data.get("school"),
            "school_quality": data.get("school_quality", "unclear"),
            "detected_name_correction": data.get("detected_name_correction"),
            "career_interests": _normalize_interests(data.get("career_interests")),
            "career_quality": data.get("career_quality", "unclear"),
            "user_intent": data.get("user_intent", "answer"),
            "confidence": float(data.get("confidence", 1.0)),
            "needs_general": int(data.get("needs_general", 0)),
        }
        logger.info(
            "[ONBOARDING][EXTRACTOR] raw_extraction=%s name_quality=%s school_quality=%s career_quality=%s user_intent=%s confidence=%.2f",
            data,
            result.get("name_quality"),
            result.get("school_quality"),
            result.get("career_quality"),
            result.get("user_intent"),
            result.get("confidence", 0.0),
        )
        return result
    except Exception as e:
        logger.error(f"[ONBOARDING][EXTRACTOR] Failed to extract onboarding fields: {e}", exc_info=True)
        return {}


async def update_user_profile(state: Dict[str, Any], updates: Dict[str, Any]):
    """
    Update user profile in database.

    Args:
        state: Current onboarding state containing user_profile
        updates: Dictionary of fields to update

    Note:
        Onboarding_stage is stored in personal_facts, not as a top-level field.
    """
    try:
        db = DatabaseClient()
        user_id = state["user_profile"]["user_id"]

        # Store onboarding_stage in personal_facts
        if "onboarding_stage" in updates:
            stage = updates.pop("onboarding_stage")
            personal_facts = state["user_profile"].get("personal_facts", {})
            if not isinstance(personal_facts, dict):
                personal_facts = {}
            personal_facts["onboarding_stage"] = stage
            updates["personal_facts"] = personal_facts

        logger.info(f"[PROFILE_UPDATER] Updating user {user_id}")
        await db.update_user_profile(user_id, updates)

    except Exception as e:
        logger.error(f"[PROFILE_UPDATER] Update failed: {e}")
        state.setdefault("errors", []).append(str(e))


async def apply_extracted_fields(state: Dict[str, Any], extraction: Dict[str, Any]) -> List[str]:
    """
    Apply extracted fields to onboarding state and persist.
    Latest user message wins; confidence is used only for logging.

    Returns list of applied field names.
    """
    if not extraction:
        return []

    profile = state.get("user_profile", {})
    prior_stage = str(profile.get("onboarding_stage") or "").strip().lower()
    personal_facts = profile.get("personal_facts") or {}
    applied: List[str] = []
    updates: Dict[str, Any] = {}

    name = extraction.get("name")
    school = extraction.get("school")
    interests_raw = extraction.get("career_interests")

    def log_overwrite(field: str, old: Any, new: Any):
        if old and old != new:
            logger.info(f"[ONBOARDING][EXTRACTOR] Overwriting {field}: {old!r} -> {new!r}")

    if name:
        log_overwrite("name", profile.get("name"), name)
        profile["name"] = name
        updates["name"] = name
        personal_facts["asked_for_name"] = False
        applied.append("name")

    if school:
        log_overwrite("university", profile.get("university"), school)
        profile["university"] = school
        updates["university"] = school
        applied.append("school")

    if interests_raw is not None:
        interests = _normalize_interests(interests_raw)
        if interests:
            log_overwrite("career_interests", profile.get("career_interests"), interests)
            profile["career_interests"] = interests
            updates["career_interests"] = interests
            applied.append("career_interests")
        else:
            logger.info("[ONBOARDING][EXTRACTOR] Skipping empty career_interests from extraction")

    profile["personal_facts"] = personal_facts

    # Determine next stage based on filled fields (latest message wins).
    # Note: being "ready for value eval" is NOT being onboarded.
    if profile.get("is_onboarded"):
        stage = "complete"
        waiting_for = None
    elif prior_stage in {"needs_eval", "value_eval", "share_to_complete"}:
        stage = prior_stage
        waiting_for = "user_input"
    elif prior_stage == "email_connect":
        stage = "email_connect"
        waiting_for = "email_connect"
    elif prior_stage == "rejected":
        stage = "rejected"
        waiting_for = None
    elif not profile.get("name"):
        stage = "name"
        waiting_for = "user_input"
    elif not profile.get("university"):
        stage = "school"
        waiting_for = "school"
    elif not profile.get("career_interests"):
        stage = "career_interest"
        waiting_for = "career_interest"
    else:
        stage = "email_connect"
        waiting_for = "email_connect"

    profile["onboarding_stage"] = stage
    updates["onboarding_stage"] = stage
    state["waiting_for"] = waiting_for
    state["user_profile"] = profile

    if updates:
        try:
            await update_user_profile(state, updates)
            logger.info(
                "[ONBOARDING][EXTRACTOR] Applied fields=%s stage=%s waiting_for=%s conf=%.2f",
                applied,
                stage,
                waiting_for,
                float(extraction.get("confidence", 1.0) or 0.0),
            )
        except Exception as e:
            logger.error(f"[ONBOARDING][EXTRACTOR] Failed to persist updates: {e}", exc_info=True)

    return applied
