from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.config import settings
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.integrations.photon_client import PhotonClient
from app.utils.redis_client import redis_client

logger = logging.getLogger(__name__)

_VALID_REACTIONS = {"love", "like", "dislike", "laugh", "emphasize", "question"}
_LOCK_TTL_SEC = 30
_SENT_TTL_SEC = 30 * 24 * 60 * 60  # 30 days

_LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+\s*[\)\.\-:])\s+")


def _strip_code_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _safe_json_loads(raw: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _has_list_formatting(text: str) -> bool:
    for line in (text or "").splitlines():
        if _LIST_LINE_RE.match(line.strip()):
            return True
    return False


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())

def _is_reaction_candidate(message_text: str) -> bool:
    """
    Cheap pre-filter to avoid LLM calls and over-reacting.
    """
    msg = str(message_text or "").strip()
    if not msg:
        return False

    # Reactions to questions/requests often feel confusing or spammy.
    if "?" in msg:
        return False

    lowered = _normalize_text(msg)
    request_markers = (
        "help",
        "can you",
        "could you",
        "please",
        "connect me",
        "introduce",
        "schedule",
        "set up",
        "find me",
    )
    if any(m in lowered for m in request_markers):
        return False

    # Only consider messages with obvious social/affective signals.
    candidates = (
        "thank",
        "thx",
        "appreciate",
        "lol",
        "haha",
        "lmao",
        "awesome",
        "great",
        "nice",
        "cool",
        "love",
        "amazing",
        "perfect",
        "yay",
        "excited",
        "sounds good",
        "fire",
        "legend",
        "sweet",
    )
    return any(c in lowered for c in candidates)


@dataclass
class ReactionService:
    """
    Decides whether to send a Tapback reaction to an inbound user message.

    - Uses strict idempotency on message GUID (never react twice).
    - Uses LLM for general messages, but is conservative by default.
    - Allows deterministic overrides (e.g., onboarding name/career interest).
    """

    photon: PhotonClient
    openai: Optional[AzureOpenAIClient] = None

    async def maybe_send_reaction(
        self,
        *,
        to_number: str,
        message_guid: str | None,
        message_content: str | None,
        chat_guid: str | None = None,
        forced_reaction: str | None = None,
        context: Optional[Dict[str, Any]] = None,
        part_index: int = 0,
    ) -> None:
        if not getattr(settings, "reactions_enabled", True):
            return

        msg_guid = str(message_guid or "").strip()
        if not msg_guid:
            return

        # Do not LLM-react during onboarding (node-level forced reactions handle the UX).
        task = str((context or {}).get("task") or "").strip().lower()
        if task == "onboarding" and not forced_reaction:
            return

        content = str(message_content or "").strip()
        if not forced_reaction:
            # Skip empty/attachments and very long messages (avoid over-reacting).
            if not content or content in {"[attachment]", "[empty]"}:
                return
            if len(content) > 220:
                return
            if _has_list_formatting(content):
                return
            if not _is_reaction_candidate(content):
                return

        # Idempotency + concurrency guard
        lock_key = f"reaction:v1:lock:{msg_guid}"
        sent_key = f"reaction:v1:sent:{msg_guid}"

        redis_available = True
        try:
            if redis_client.client.get(sent_key):
                return
        except Exception:
            # If Redis is down, only allow forced reactions (onboarding UX).
            if not forced_reaction:
                return
            redis_available = False

        if redis_available:
            try:
                got_lock = redis_client.client.set(lock_key, "1", nx=True, ex=_LOCK_TTL_SEC)
            except Exception:
                if not forced_reaction:
                    return
                redis_available = False
                got_lock = True
            if got_lock is not True:
                return

        try:
            reaction = (forced_reaction or "").strip().lower() or None
            if reaction is None:
                reaction = await self._decide_reaction_llm(content, context=context)

            if not reaction or reaction not in _VALID_REACTIONS:
                return

            result = await self.photon.send_reaction(
                to_number=to_number,
                message_guid=msg_guid,
                reaction=reaction,
                chat_guid=chat_guid,
                part_index=int(part_index or 0),
            )

            if isinstance(result, dict) and result.get("success") is True:
                try:
                    if redis_available:
                        redis_client.client.setex(sent_key, _SENT_TTL_SEC, reaction)
                except Exception:
                    pass
        except Exception as e:
            logger.debug("[REACTION] Failed to send reaction: %s", e)
        finally:
            try:
                if redis_available:
                    redis_client.client.delete(lock_key)
            except Exception:
                pass

    async def _decide_reaction_llm(self, message_text: str, *, context: Optional[Dict[str, Any]] = None) -> Optional[str]:
        if not getattr(settings, "reactions_llm_enabled", True):
            return None
        if self.openai is None:
            return None

        msg = str(message_text or "").strip()
        if not msg:
            return None

        # Keep it conservative and safe.
        system_prompt = (
            "you are choosing whether to send an iMessage tapback reaction to the user's last message.\n"
            "options: love, like, dislike, laugh, emphasize, question.\n"
            "\n"
            "rules:\n"
            "- react only when it clearly improves the vibe; most messages should be no reaction\n"
            "- avoid negative reactions (dislike/question) unless it is obviously appropriate and non-hurtful\n"
            "- do not react to sensitive, sad, or serious messages; choose react=false\n"
            "- do not react if the user is asking for help / making a request; choose react=false\n"
            "- be safe and non-spammy\n"
            "\n"
            "output JSON only: {\"react\": true|false, \"reaction\": \"love|like|dislike|laugh|emphasize|question|\"}\n"
            "if react=false, set reaction to empty string."
        )

        intent = str((context or {}).get("intent") or "").strip()
        task = str((context or {}).get("task") or "").strip()
        stage = str((context or {}).get("onboarding_stage") or "").strip()

        user_prompt = (
            f"context:\n"
            f"- intent: {intent or 'unknown'}\n"
            f"- task: {task or 'unknown'}\n"
            f"- onboarding_stage: {stage or 'n/a'}\n"
            f"\n"
            f"user_message:\n{msg}\n"
        )

        try:
            raw = await self.openai.generate_response(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=str(getattr(settings, "reactions_model", "gpt-4o-mini") or "gpt-4o-mini"),
                temperature=0.2,
                trace_label="tapback_reaction_decide",
            )
        except Exception:
            return None

        data = _safe_json_loads(_strip_code_fences(str(raw or "")))
        if not data:
            return None

        should_react = bool(data.get("react") is True or str(data.get("react") or "").strip().lower() == "true")
        if not should_react:
            return None

        reaction = str(data.get("reaction") or "").strip().lower()
        if reaction not in _VALID_REACTIONS:
            return None

        # Final hard guardrails against spammy/unhelpful reactions.
        lowered = _normalize_text(msg)
        if any(token in lowered for token in ("help", "can you", "could you", "please", "connect me", "introduce", "schedule")):
            return None

        return reaction
