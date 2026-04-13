"""
LLM-based interpretation of demand/value updates using session context.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, TypedDict

from app.integrations.azure_openai_client import AzureOpenAIClient
from app.utils.demand_value_history import normalize_history

logger = logging.getLogger(__name__)


class DemandValueInterpretation(TypedDict, total=False):
    demand_update: Optional[str]
    value_update: Optional[str]


class ValueHistoryEditPlan(TypedDict, total=False):
    action: str
    edits: list[dict[str, Any]]


_INTERPRET_SYSTEM_PROMPT = """you summarize a user's networking demand/value update for storage.

output json only:
{
  "demand_update": "string or null",
  "value_update": "string or null"
}

definitions:
- demand_update: who the user wants to meet and what they want to gain
- value_update: what the user can offer others (skills, experience, resources)

rules:
- use the full session context to interpret the latest user message
- only include details explicitly stated by the user in this session
- do not invent or infer new details
- assistant messages are context only; do not treat them as user facts
- if demand_hint/value_hint are provided, keep their meaning and refine them using session context
- if the user refines or clarifies, keep the most specific version
- remove filler and noise; keep concrete roles, skills, domains, outcomes
- keep each field to 1-2 sentences max
- plain text, lowercase, no markdown
"""

_VALUE_HISTORY_EDIT_SYSTEM_PROMPT = """you edit a user's stored value history based on their latest update.

output json only:
{
  "action": "apply | clear | none",
  "edits": [
    {"op": "append", "text": "string"},
    {"op": "replace", "index": 0, "text": "string"},
    {"op": "remove", "index": 0}
  ]
}

definitions:
- value_history: prior value entries with index and text
- value_update: the user's latest statement about what they can offer

rules:
- use only value_update content; do not invent details
- if the user says they no longer offer anything or wants to delete value, use action "clear"
- if the user retracts or corrects a specific entry, use remove or replace for that index
- if the user adds a new value not covered, append
- edits may include multiple operations
- indices must reference the provided value_history
- keep text 1-2 sentences max, plain text, lowercase, no markdown
"""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _safe_json_loads(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except Exception:
                return {}
        return {}


def _clean_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned.replace("\n", " ").strip()


def _normalize_session_messages(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = _clean_text(item.get("content"))
        if not content:
            continue
        out.append({"role": role, "content": content})
    return out


def _serialize_value_history(value_history: Any) -> list[dict[str, Any]]:
    items = normalize_history(value_history)
    return [{"index": idx, "text": item.get("text", "")} for idx, item in enumerate(items)]


async def interpret_demand_value_update(
    *,
    session_messages: list[dict[str, str]],
    demand_hint: Optional[str] = None,
    value_hint: Optional[str] = None,
) -> DemandValueInterpretation:
    normalized_session = _normalize_session_messages(session_messages)
    if not normalized_session and not demand_hint and not value_hint:
        return {"demand_update": None, "value_update": None}

    payload = {
        "session_messages": normalized_session,
        "demand_hint": _clean_text(demand_hint),
        "value_hint": _clean_text(value_hint),
    }
    user_prompt = f"session data:\n{json.dumps(payload, ensure_ascii=True)}\n\njson only"

    try:
        openai = AzureOpenAIClient()
        response = await openai.generate_response(
            system_prompt=_INTERPRET_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model="gpt-4o-mini",
            temperature=0.2,
            max_tokens=260,
            trace_label="demand_value_interpret",
        )
        data = _safe_json_loads(response)
        demand_update = _clean_text(data.get("demand_update"))
        value_update = _clean_text(data.get("value_update"))

        if not demand_update and demand_hint:
            demand_update = _clean_text(demand_hint)
        if not value_update and value_hint:
            value_update = _clean_text(value_hint)

        return {
            "demand_update": demand_update,
            "value_update": value_update,
        }
    except Exception as e:
        logger.warning("[DEMAND_VALUE] interpretation failed: %s", e, exc_info=True)
        return {
            "demand_update": _clean_text(demand_hint),
            "value_update": _clean_text(value_hint),
        }


async def interpret_value_history_edit(
    *,
    value_update: Optional[str],
    value_history: Any,
) -> Optional[ValueHistoryEditPlan]:
    cleaned_update = _clean_text(value_update)
    if not cleaned_update:
        return None

    payload = {
        "value_update": cleaned_update,
        "value_history": _serialize_value_history(value_history),
    }
    user_prompt = f"data:\n{json.dumps(payload, ensure_ascii=True)}\n\njson only"

    try:
        openai = AzureOpenAIClient()
        response = await openai.generate_response(
            system_prompt=_VALUE_HISTORY_EDIT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model="gpt-4o-mini",
            temperature=0.2,
            max_tokens=320,
            trace_label="value_history_edit",
        )
        data = _safe_json_loads(response)
        if not isinstance(data, dict):
            return None
        return data
    except Exception as e:
        logger.warning("[DEMAND_VALUE] value history edit interpretation failed: %s", e, exc_info=True)
        return None
