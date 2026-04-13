"""Refresh derived demand/value fields from Supabase history."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient

logger = logging.getLogger(__name__)


def _coerce_text(value: Any) -> str:
    return str(value or "").strip()


def normalize_history(raw: Any) -> List[Dict[str, str]]:
    """Normalize history into a list of {text, created_at?} dicts."""
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict):
            text = _coerce_text(item.get("text"))
            created_at = _coerce_text(item.get("created_at"))
            if not text:
                continue
            entry = {"text": text}
            if created_at:
                entry["created_at"] = created_at
            out.append(entry)
            continue
        if isinstance(item, str):
            text = _coerce_text(item)
            if text:
                out.append({"text": text})
    return out


def latest_text(history: Any) -> Optional[str]:
    """Return the latest history entry's text, if any."""
    items = normalize_history(history)
    if not items:
        return None
    return items[-1].get("text") or None


def all_texts(history: Any) -> List[str]:
    """Return all history entry texts in order."""
    items = normalize_history(history)
    return [item["text"] for item in items if item.get("text")]


def combine_texts(history: Any, separator: str = "\n") -> str:
    """Combine all history entry texts into a single string."""
    return separator.join(all_texts(history))


def history_text(
    history: Any,
    *,
    separator: str = "\n",
    default: str = "",
) -> str:
    """Return combined history text or a default value."""
    combined = combine_texts(history, separator=separator).strip()
    return combined if combined else default


def append_history(
    history: Any,
    text: Optional[str],
    *,
    created_at: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Append a new history entry if text is non-empty."""
    items = normalize_history(history)
    cleaned = _coerce_text(text)
    if not cleaned:
        return items
    entry = {"text": cleaned}
    if created_at:
        entry["created_at"] = _coerce_text(created_at)
    items.append(entry)
    return items


def _load_history_row(db: DatabaseClient, user_id: str) -> Dict[str, Any]:
    result = (
        db.client.table("users")
        .select("demand_history,value_history")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise ValueError(f"User {user_id} not found")
    row = result.data[0] or {}
    return row if isinstance(row, dict) else {}


async def update_demand_value_derived_fields(
    *,
    db: DatabaseClient,
    user_id: str,
    demand_history: Optional[Any] = None,
    value_history: Optional[Any] = None,
) -> Dict[str, Any]:
    """Recompute demand/value derived fields from history and persist them."""
    if not user_id:
        raise ValueError("update_demand_value_derived_fields requires user_id")

    if demand_history is None or value_history is None:
        row = _load_history_row(db, user_id)
        if demand_history is None:
            demand_history = row.get("demand_history")
        if value_history is None:
            value_history = row.get("value_history")

    demand_history = normalize_history(demand_history)
    value_history = normalize_history(value_history)

    latest_demand = latest_text(demand_history)
    all_demand = combine_texts(demand_history).strip()
    all_value = combine_texts(value_history).strip()

    update_payload: Dict[str, Any] = {
        "latest_demand": latest_demand or None,
        "all_demand": all_demand or None,
        "all_value": all_value or None,
        "updated_at": datetime.utcnow().isoformat(),
    }

    embedding_updates: Dict[str, Any] = {}
    openai = AzureOpenAIClient()

    if all_demand:
        demand_embedding = await openai.get_embedding(all_demand)
        if demand_embedding is None:
            logger.warning("Failed to generate demand embedding for user %s", user_id)
        else:
            embedding_updates["demand_embedding"] = demand_embedding
    else:
        embedding_updates["demand_embedding"] = None

    if latest_demand:
        latest_embedding = await openai.get_embedding(latest_demand)
        if latest_embedding is None:
            logger.warning("Failed to generate latest demand embedding for user %s", user_id)
        else:
            embedding_updates["latest_demand_embedding"] = latest_embedding
    else:
        embedding_updates["latest_demand_embedding"] = None

    if all_value:
        value_embedding = await openai.get_embedding(all_value)
        if value_embedding is None:
            logger.warning("Failed to generate value embedding for user %s", user_id)
        else:
            embedding_updates["value_embedding"] = value_embedding
    else:
        embedding_updates["value_embedding"] = None

    update_payload.update(embedding_updates)

    result = (
        db.client.table("users")
        .update(update_payload)
        .eq("id", user_id)
        .execute()
    )
    if not result.data:
        raise ValueError(f"User {user_id} not found")

    record = dict(result.data[0])
    record["demand_history"] = demand_history
    record["value_history"] = value_history
    record["latest_demand"] = latest_demand
    record["all_demand"] = all_demand
    record["all_value"] = all_value
    for key, value in embedding_updates.items():
        record[key] = value
    return record
