"""
Onboarding Classification Utilities

Handles LLM-based classification of user responses during onboarding.
- Email connect stage classification
- Share-to-complete stage classification
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from app.integrations.azure_openai_client import AzureOpenAIClient

logger = logging.getLogger(__name__)

# Email connect decisions
_EMAIL_DECISIONS = {"connect", "connected", "decline", "question", "concern", "unclear"}

# Share stage decisions
_SHARE_DECISIONS = {"shared", "skip", "question", "intent", "unclear"}

_REMINDER_BUBBLE = (
    "just a quick reminder, you'll need to connect your work or school gmail "
    "to continue with the onboarding. after u connect, say done."
)


def _normalize_text(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def get_email_connect_reminder() -> str:
    return _REMINDER_BUBBLE


def _extract_json_payload(text: str) -> Optional[str]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return None


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    payload = _extract_json_payload(text)
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _repair_json(raw: str, *, schema: str, trace_label: str) -> Optional[Dict[str, Any]]:
    openai = AzureOpenAIClient()
    system_prompt = (
        "you are a strict json formatter\n"
        "return valid json only that matches this schema:\n"
        f"{schema}\n"
        "rules:\n"
        "- output json only, no markdown, no code fences\n"
        "- include all required keys\n"
        "- use allowed enum values only"
    )
    repaired = await openai.generate_response(
        system_prompt=system_prompt,
        user_prompt=str(raw or "").strip(),
        model="gpt-4o-mini",
        temperature=0.0,
        trace_label=trace_label,
    )
    return _parse_json(str(repaired or ""))


# =============================================================================
# Email Connect Classification
# =============================================================================


async def classify_email_connect_reply(
    *,
    message: str,
    user_profile: Dict[str, Any],
    email_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    LLM-only classifier for the email-connect stage.
    Returns dict with: decision, confidence, reason.
    """
    email_state = email_state or {}
    profile_context = {
        "name": user_profile.get("name"),
        "university": user_profile.get("university"),
        "career_interests": user_profile.get("career_interests") or [],
        "email_connect_status": email_state.get("status"),
    }
    system_prompt = (
        "you classify user replies during franklink onboarding: gmail connect step\n"
        "output json only with keys: decision, confidence, reason\n"
        "decision must be one of: connect, connected, decline, question, concern, unclear\n"
        "definitions:\n"
        "- connect: user wants the auth link or says yes/ok/sure to connect\n"
        "- connected: user confirms they connected - look for: 'done', 'finished', 'i did it', 'connected', 'i connected', 'fine i did it', 'ok i connected', 'linked', 'completed'. Be generous here - if they seem to be saying they did it, classify as connected\n"
        "- decline: user explicitly refuses or says they will not connect (e.g., 'no', 'nah', 'i don't want to', 'skip')\n"
        "- question: user is asking WHY they need to connect or what it's for (e.g., 'why do you need my email?', 'what is this for?', 'why?', 'what do you do with it?')\n"
        "- concern: user expresses privacy/trust/safety concerns (e.g., 'seems sketchy', 'is this safe?', 'do you read my emails?', 'i don't trust this', 'sounds spammy')\n"
        "- unclear: truly ambiguous or unrelated messages\n"
        "IMPORTANT: be generous with 'connected' classification - users often say things like 'fine i did it' or 'ok done' which should be connected\n"
        "be strict and do not invent details"
    )
    user_prompt = (
        "profile_context:\n"
        f"{json.dumps(profile_context, ensure_ascii=False)}\n\n"
        "user_message:\n"
        f"{message}\n\n"
        "return json only:\n"
        "{\"decision\":\"connect|connected|decline|question|concern|unclear\",\"confidence\":0-1,\"reason\":\"...\"}"
    )
    openai = AzureOpenAIClient()
    raw = await openai.generate_response(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model="gpt-4o-mini",
        temperature=0.0,
        trace_label="onboarding_email_connect_classify",
    )
    parsed = _parse_json(str(raw or ""))
    if not parsed:
        parsed = await _repair_json(
            str(raw or ""),
            schema='{"decision":"connect|connected|decline|question|concern|unclear","confidence":0-1,"reason":"..."}',
            trace_label="onboarding_email_connect_classify_repair",
        )
    if not parsed:
        raise ValueError("email_connect_classify_parse_failed")
    decision = str(parsed.get("decision") or "").strip().lower()
    if decision not in _EMAIL_DECISIONS:
        repaired = await _repair_json(
            json.dumps(parsed, ensure_ascii=False),
            schema='{"decision":"connect|connected|decline|question|concern|unclear","confidence":0-1,"reason":"..."}',
            trace_label="onboarding_email_connect_classify_repair_decision",
        )
        if repaired:
            parsed = repaired
            decision = str(parsed.get("decision") or "").strip().lower()
    if decision not in _EMAIL_DECISIONS:
        raise ValueError(f"email_connect_invalid_decision:{decision}")

    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "decision": decision,
        "confidence": confidence,
        "reason": str(parsed.get("reason") or "").strip(),
    }


async def build_email_connect_message(
    *,
    user_profile: Dict[str, Any],
    mode: str,
    auth_link: Optional[str] = None,
    email_state: Optional[Dict[str, Any]] = None,
) -> str:
    """
    LLM-only message builder for the email-connect stage.

    mode: ask | reask | link | link_error
    """
    email_state = email_state or {}
    profile_context = {
        "name": user_profile.get("name"),
        "university": user_profile.get("university"),
        "career_interests": user_profile.get("career_interests") or [],
        "email_connect_status": email_state.get("status"),
    }
    system_prompt = (
        "you are frank, a casual iMessage onboarding assistant\n"
        "write one short message (1-2 sentences, lowercase)\n"
        "goal: get the user to connect a work/school gmail inbox to continue onboarding\n"
        "no emojis, no bullet lists, under 320 characters\n"
        "if a link is provided, include the token {{AUTH_LINK}} on its own line exactly once\n"
        "output json only: {\"message\":\"...\"}"
    )
    user_prompt = (
        "mode:\n"
        f"{mode}\n\n"
        "profile_context:\n"
        f"{json.dumps(profile_context, ensure_ascii=False)}\n\n"
        "auth_link:\n"
        f"{auth_link or 'none'}\n\n"
        "mode guidance:\n"
        "- ask: ask if they want to connect their work/school gmail and say it is required to continue\n"
        "- reask: they did not confirm; remind them it is required and ask again to connect\n"
        "- link: remind them it is required to continue, include the link, and ask them to reply 'done' after connecting\n"
        "- link_error: say you could not generate the link and ask them to try again\n"
        "return json only:\n"
        "{\"message\":\"...\"}"
    )
    openai = AzureOpenAIClient()
    raw = await openai.generate_response(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model="gpt-4o-mini",
        temperature=0.4,
        trace_label="onboarding_email_connect_message",
    )
    parsed = _parse_json(str(raw or ""))
    if not parsed:
        parsed = await _repair_json(
            str(raw or ""),
            schema='{"message":"..."}',
            trace_label="onboarding_email_connect_message_repair",
        )
    if not parsed:
        raise ValueError("email_connect_message_parse_failed")
    message = str(parsed.get("message") or "").strip().strip('"').strip("'").strip()
    if not message:
        raise ValueError("email_connect_message_empty")
    if auth_link:
        if "{{AUTH_LINK}}" not in message:
            rewrite_prompt = (
                "rewrite the message to include the token {{AUTH_LINK}} on its own line exactly once\n"
                "keep the tone and length similar\n"
                "output json only: {\"message\":\"...\"}"
            )
            raw = await openai.generate_response(
                system_prompt=rewrite_prompt,
                user_prompt=message,
                model="gpt-4o-mini",
                temperature=0.2,
                trace_label="onboarding_email_connect_message_rewrite_link",
            )
            parsed = _parse_json(str(raw or ""))
            if not parsed:
                parsed = await _repair_json(
                    str(raw or ""),
                    schema='{"message":"..."}',
                    trace_label="onboarding_email_connect_message_repair_link",
                )
            if parsed and isinstance(parsed.get("message"), str):
                message = parsed["message"]
        if "{{AUTH_LINK}}" not in message:
            raise ValueError("email_connect_link_missing")
        message = message.replace("{{AUTH_LINK}}", auth_link)

        if "done" not in _normalize_text(message):
            rewrite_prompt = (
                "rewrite the message to explicitly ask the user to reply 'done' after connecting\n"
                "keep the exact line containing the link unchanged\n"
                "output json only: {\"message\":\"...\"}"
            )
            raw = await openai.generate_response(
                system_prompt=rewrite_prompt,
                user_prompt=f"message:\n{message}\n\nlink:\n{auth_link}",
                model="gpt-4o-mini",
                temperature=0.2,
                trace_label="onboarding_email_connect_message_rewrite_done",
            )
            parsed = _parse_json(str(raw or ""))
            if not parsed:
                parsed = await _repair_json(
                    str(raw or ""),
                    schema='{"message":"..."}',
                    trace_label="onboarding_email_connect_message_repair_done",
                )
            if parsed and isinstance(parsed.get("message"), str):
                message = parsed["message"]
            if auth_link not in message:
                raise ValueError("email_connect_link_missing_after_done_rewrite")
            if "done" not in _normalize_text(message):
                raise ValueError("email_connect_done_missing")

    if mode == "link_error":
        normalized = _normalize_text(message)
        if "link" not in normalized or ("try again" not in normalized and "retry" not in normalized):
            rewrite_prompt = (
                "rewrite the message to explicitly say you couldn't generate the link and ask them to try again\n"
                "output json only: {\"message\":\"...\"}"
            )
            raw = await openai.generate_response(
                system_prompt=rewrite_prompt,
                user_prompt=message,
                model="gpt-4o-mini",
                temperature=0.2,
                trace_label="onboarding_email_connect_message_rewrite_link_error",
            )
            parsed = _parse_json(str(raw or ""))
            if not parsed:
                parsed = await _repair_json(
                    str(raw or ""),
                    schema='{"message":"..."}',
                    trace_label="onboarding_email_connect_message_repair_link_error",
                )
            if parsed and (parsed.get("message") is not None):
                message = str(parsed.get("message")).strip()
    return message


# =============================================================================
# Share Stage Classification
# =============================================================================


async def classify_share_reply(
    *,
    message: str,
    user_profile: Dict[str, Any],
    has_media: bool = False,
) -> Dict[str, Any]:
    """
    LLM classifier for the share-to-complete stage.

    Returns dict with: decision, confidence, reason.

    Decisions:
    - shared: user has shared screenshot (media attached or says they did)
    - skip: user wants to skip sharing and pay the intro fee
    - question: user is asking about the share/fee
    - unclear: truly ambiguous messages
    """
    # If media is attached, it's definitely a share
    if has_media:
        return {
            "decision": "shared",
            "confidence": 1.0,
            "reason": "media attachment detected",
        }

    profile_context = {
        "name": user_profile.get("name"),
        "intro_fee_cents": user_profile.get("intro_fee_cents") or user_profile.get("personal_facts", {}).get("frank_value_eval", {}).get("intro_fee_cents", 499),
    }

    system_prompt = (
        "you classify user replies during franklink onboarding: share-to-complete step\n"
        "context: frank asked user to screenshot the conversation and share to their story for $0 intro fee, or skip and pay the regular fee\n"
        "output json only with keys: decision, confidence, reason\n"
        "decision must be one of: shared, skip, question, intent, unclear\n"
        "definitions:\n"
        "- shared: user CONFIRMS they already shared/posted (e.g., 'done', 'shared', 'posted', 'i shared it', 'just posted it'). ONLY use this when they say they ALREADY DID IT, not when they say they WANT to do it\n"
        "- intent: user expresses intent/willingness to share but HASN'T done it yet (e.g., 'yes', 'yep', 'i want to share', 'i'll share it', 'sure i'll do it', 'ok', 'bet', 'i can do that', 'yep i want to share it to others'). Use this when they're agreeing to share but haven't sent the screenshot yet\n"
        "- skip: user wants to skip sharing and pay the fee instead (e.g., 'skip', 'nah', 'no thanks', 'pass', 'later', 'not now', 'im good', 'i'm good', 'nah im good', 'ill pass'). Be generous here - if they seem to be declining to share, classify as skip\n"
        "- question: user is asking about the share requirement or fee (e.g., 'why?', 'what fee?', 'where do i share?')\n"
        "- unclear: truly ambiguous or unrelated messages\n"
        "CRITICAL: distinguish between INTENT (they agree/want to share) vs SHARED (they confirm they already did it). 'i want to share' is INTENT, 'i shared it' is SHARED\n"
        "be strict and do not invent details"
    )

    user_prompt = (
        "profile_context:\n"
        f"{json.dumps(profile_context, ensure_ascii=False)}\n\n"
        "user_message:\n"
        f"{message}\n\n"
        "return json only:\n"
        '{"decision":"shared|skip|question|intent|unclear","confidence":0-1,"reason":"..."}'
    )

    openai = AzureOpenAIClient()
    raw = await openai.generate_response(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model="gpt-4o-mini",
        temperature=0.0,
        trace_label="onboarding_share_classify",
    )

    parsed = _parse_json(str(raw or ""))
    if not parsed:
        parsed = await _repair_json(
            str(raw or ""),
            schema='{"decision":"shared|skip|question|intent|unclear","confidence":0-1,"reason":"..."}',
            trace_label="onboarding_share_classify_repair",
        )

    if not parsed:
        raise ValueError("share_classify_parse_failed")

    decision = str(parsed.get("decision") or "").strip().lower()
    if decision not in _SHARE_DECISIONS:
        repaired = await _repair_json(
            json.dumps(parsed, ensure_ascii=False),
            schema='{"decision":"shared|skip|question|intent|unclear","confidence":0-1,"reason":"..."}',
            trace_label="onboarding_share_classify_repair_decision",
        )
        if repaired:
            parsed = repaired
            decision = str(parsed.get("decision") or "").strip().lower()

    if decision not in _SHARE_DECISIONS:
        raise ValueError(f"share_invalid_decision:{decision}")

    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5

    return {
        "decision": decision,
        "confidence": confidence,
        "reason": str(parsed.get("reason") or "").strip(),
    }
