"""Apply demand/value history updates and refresh embeddings."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.utils.demand_value_history import (
    append_history,
    apply_value_history_edits,
    combine_texts,
    latest_text,
    normalize_history,
)
from app.utils.demand_value_interpreter import interpret_value_history_edit


async def apply_demand_value_updates(
    *,
    db: DatabaseClient,
    user_id: str,
    demand_update: Optional[str] = None,
    value_update: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply demand/value updates (including value edits) and refresh embeddings."""
    timestamp = datetime.utcnow().isoformat()
    snapshot = await db.get_demand_value_state(user_id)

    demand_history = normalize_history(snapshot.get("demand_history"))
    value_history = normalize_history(snapshot.get("value_history"))
    metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}

    if demand_update:
        demand_history = append_history(demand_history, demand_update, created_at=timestamp)

    edit_plan = None
    applied_plan = None
    if value_update:
        edit_plan = await interpret_value_history_edit(
            value_update=value_update,
            value_history=value_history,
        )
        value_history, applied_plan = apply_value_history_edits(
            value_history,
            edit_plan=edit_plan,
            fallback_text=value_update,
            created_at=timestamp,
        )
        metadata = _record_value_history_edit_metadata(
            metadata,
            created_at=timestamp,
            value_update=value_update,
            edit_plan=edit_plan,
            applied_plan=applied_plan,
        )

    latest_demand = latest_text(demand_history)
    all_demand = combine_texts(demand_history).strip()
    all_value = combine_texts(value_history).strip()

    update_payload: Dict[str, Any] = {
        "demand_history": demand_history,
        "value_history": value_history,
        "latest_demand": latest_demand or None,
        "all_demand": all_demand or None,
        "all_value": all_value or None,
    }
    if value_update:
        update_payload["metadata"] = metadata

    record = await db.update_user_profile(user_id, update_payload)

    openai = AzureOpenAIClient()
    if all_demand:
        demand_embedding = await openai.get_embedding(all_demand)
        if demand_embedding:
            await db.update_demand_embedding(user_id, demand_embedding)
    if latest_demand:
        latest_embedding = await openai.get_embedding(latest_demand)
        if latest_embedding:
            await db.update_latest_demand_embedding(user_id, latest_embedding)
    if all_value:
        value_embedding = await openai.get_embedding(all_value)
        if value_embedding:
            await db.update_value_embedding(user_id, value_embedding)
    else:
        await db.update_value_embedding(user_id, None)

    record["demand_history"] = demand_history
    record["value_history"] = value_history
    record["latest_demand"] = latest_demand
    record["all_demand"] = all_demand
    record["all_value"] = all_value
    return record


def _record_value_history_edit_metadata(
    metadata: Dict[str, Any],
    *,
    created_at: str,
    value_update: str,
    edit_plan: Optional[Dict[str, Any]],
    applied_plan: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    meta = dict(metadata) if isinstance(metadata, dict) else {}
    history = meta.get("value_history_edits")
    history = list(history) if isinstance(history, list) else []
    history.append(
        {
            "created_at": created_at,
            "value_update": value_update,
            "plan": edit_plan if isinstance(edit_plan, dict) else {},
            "applied": applied_plan if isinstance(applied_plan, dict) else {},
        }
    )
    meta["value_history_edits"] = history
    return meta
