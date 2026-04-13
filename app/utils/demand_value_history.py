"""Helpers for demand/value history data stored as JSON arrays."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


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


def _coerce_index(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _normalize_action(value: Any) -> str:
    action = _coerce_text(value).lower()
    if action in {"apply", "clear", "none"}:
        return action
    return "apply"


def apply_value_history_edits(
    history: Any,
    *,
    edit_plan: Optional[Dict[str, Any]] = None,
    fallback_text: Optional[str] = None,
    created_at: Optional[str] = None,
) -> tuple[List[Dict[str, str]], Dict[str, Any]]:
    """
    Apply edit operations to value history.

    Returns (updated_history, applied_plan).
    """
    items = normalize_history(history)
    plan = edit_plan if isinstance(edit_plan, dict) else {}
    action = _normalize_action(plan.get("action"))
    edits_raw = plan.get("edits") if isinstance(plan.get("edits"), list) else []

    if action == "clear":
        return [], {"action": "clear", "edits": []}

    if action == "none":
        return items, {"action": "none", "edits": []}

    updated = list(items)
    removed_indices: set[int] = set()
    applied_edits: list[dict[str, Any]] = []

    for raw in edits_raw:
        if not isinstance(raw, dict):
            continue
        op = _coerce_text(raw.get("op") or raw.get("action")).lower()
        if op == "remove":
            index = _coerce_index(raw.get("index"))
            if index is None or index >= len(updated):
                continue
            removed_indices.add(index)
            applied_edits.append({"op": "remove", "index": index})
        elif op == "replace":
            index = _coerce_index(raw.get("index"))
            text = _coerce_text(raw.get("text"))
            if index is None or index >= len(updated) or not text:
                continue
            if index in removed_indices:
                continue
            created = updated[index].get("created_at")
            entry = {"text": text}
            if created:
                entry["created_at"] = created
            updated[index] = entry
            applied_edits.append({"op": "replace", "index": index, "text": text})

    filtered = [entry for idx, entry in enumerate(updated) if idx not in removed_indices]

    for raw in edits_raw:
        if not isinstance(raw, dict):
            continue
        op = _coerce_text(raw.get("op") or raw.get("action")).lower()
        if op != "append":
            continue
        text = _coerce_text(raw.get("text"))
        if not text:
            continue
        entry = {"text": text}
        if created_at:
            entry["created_at"] = _coerce_text(created_at)
        filtered.append(entry)
        applied_edits.append({"op": "append", "text": text})

    if not applied_edits and fallback_text:
        fallback = append_history(items, fallback_text, created_at=created_at)
        return fallback, {"action": "append_fallback", "edits": [{"op": "append", "text": _coerce_text(fallback_text)}]}

    return filtered, {"action": "apply", "edits": applied_edits}
