"""Pending confirmation utilities and reply classification."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient

logger = logging.getLogger(__name__)

PENDING_CONFIRMATION_KEY = "pending_confirmation"
DEFAULT_CONFIRMATION_TTL_HOURS = 24
MAX_UNRELATED_ATTEMPTS = 2

VALID_CONFIRMATION_TYPES = {"profile_update", "match_proposal"}
VALID_CONFIRMATION_LABELS = {"confirm", "decline", "modify", "unrelated", "meta_question"}


def _now_utc() -> datetime:
    return datetime.utcnow()


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _normalize_personal_facts(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def build_pending_confirmation(
    *,
    pending_type: str,
    draft: Dict[str, Any],
    prompt: str,
    ttl_hours: int = DEFAULT_CONFIRMATION_TTL_HOURS,
) -> Dict[str, Any]:
    pending = {
        "type": pending_type,
        "draft": draft,
        "prompt": prompt,
        "attempts": 0,
        "created_at": _now_utc().isoformat(),
        "expires_at": (_now_utc() + timedelta(hours=ttl_hours)).isoformat(),
    }
    return pending


def get_pending_confirmation(user_profile: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    personal_facts = _normalize_personal_facts(user_profile.get("personal_facts"))
    pending = personal_facts.get(PENDING_CONFIRMATION_KEY)
    if not isinstance(pending, dict):
        return None
    pending_type = str(pending.get("type") or "").strip()
    if pending_type not in VALID_CONFIRMATION_TYPES:
        return None
    expires_at = _parse_iso_dt(pending.get("expires_at"))
    if expires_at and _now_utc() > expires_at:
        return None
    return pending


def should_clear_pending(pending: Dict[str, Any]) -> bool:
    attempts = pending.get("attempts")
    try:
        count = int(attempts or 0)
    except Exception:
        count = 0
    return count >= MAX_UNRELATED_ATTEMPTS


async def set_pending_confirmation(
    *,
    db: DatabaseClient,
    user_id: str,
    personal_facts: Any,
    pending: Dict[str, Any],
) -> Dict[str, Any]:
    pf = _normalize_personal_facts(personal_facts)
    pf[PENDING_CONFIRMATION_KEY] = pending
    await db.update_user_profile(user_id, {"personal_facts": pf})
    return pf


async def clear_pending_confirmation(
    *,
    db: DatabaseClient,
    user_id: str,
    personal_facts: Any,
) -> Dict[str, Any]:
    pf = _normalize_personal_facts(personal_facts)
    pf.pop(PENDING_CONFIRMATION_KEY, None)
    await db.update_user_profile(user_id, {"personal_facts": pf})
    return pf


async def bump_pending_attempts(
    *,
    db: DatabaseClient,
    user_id: str,
    personal_facts: Any,
    pending: Dict[str, Any],
) -> Dict[str, Any]:
    pf = _normalize_personal_facts(personal_facts)
    attempts = pending.get("attempts")
    try:
        count = int(attempts or 0)
    except Exception:
        count = 0
    pending["attempts"] = count + 1
    pf[PENDING_CONFIRMATION_KEY] = pending
    await db.update_user_profile(user_id, {"personal_facts": pf})
    return pending


def _fallback_confirmation_classification(message: str) -> str:
    text = str(message or "").strip().lower()
    if not text:
        return "unrelated"
    if text in {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "do it"}:
        return "confirm"
    if text in {"no", "nah", "nope", "dont", "don't", "not now"}:
        return "decline"
    if "what do you mean" in text or "what do u mean" in text or "explain" in text:
        return "meta_question"
    if text.endswith("?"):
        return "meta_question"
    return "unrelated"


def _safe_json_loads(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    text = str(raw).strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                return {}
    return {}


async def classify_confirmation_reply(
    *,
    user_message: str,
    pending_type: str,
    pending_prompt: str,
    pending_draft: Dict[str, Any],
    model: str = "gpt-4o-mini",
) -> Tuple[str, float]:
    system_prompt = (
        "you classify a user's reply to a pending confirmation in a chat concierge\n"
        "output JSON only: {\"classification\":\"confirm|decline|modify|unrelated|meta_question\",\"confidence\":0.0}\n"
        "\n"
        "label meanings:\n"
        "- confirm: user clearly agrees to proceed\n"
        "- decline: user clearly says no / not now\n"
        "- modify: user wants to change the update or the proposed match preferences\n"
        "- meta_question: user asks what you mean, asks for clarification, or asks for explanation\n"
        "- unrelated: off-topic or unclear\n"
        "\n"
        "notes:\n"
        "- if the user asks for a different match or says 'someone else', use modify\n"
        "- if they ask 'who is that' or 'what do you mean', use meta_question\n"
    )
    user_prompt = (
        "pending_type:\n"
        f"{pending_type}\n\n"
        "pending_prompt:\n"
        f"{pending_prompt}\n\n"
        "pending_draft_json:\n"
        f"{json.dumps(pending_draft, ensure_ascii=True)}\n\n"
        "user_reply:\n"
        f"{user_message}\n\n"
        "json only"
    )
    try:
        openai = AzureOpenAIClient()
        raw = await openai.generate_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            temperature=0.0,
            max_tokens=120,
            trace_label="confirmation_reply_classification",
        )
        data = _safe_json_loads(raw)
        classification = str(data.get("classification") or "").strip()
        if classification not in VALID_CONFIRMATION_LABELS:
            classification = _fallback_confirmation_classification(user_message)
        confidence = 0.0
        try:
            confidence = float(data.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        return classification, confidence
    except Exception:
        logger.debug("[CONFIRMATION] classification failed", exc_info=True)
        return _fallback_confirmation_classification(user_message), 0.0
