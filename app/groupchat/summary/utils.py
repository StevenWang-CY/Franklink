from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def clip(value: str, max_len: int) -> str:
    s = str(value or "")
    return s if len(s) <= max_len else s[:max_len]


def parse_timestamp(value: Any) -> Optional[datetime]:
    """
    Best-effort parser for timestamps coming from Photon/Zep/Supabase.

    Returns a timezone-aware UTC datetime when possible.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    # Epoch seconds (int/float as string).
    try:
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        if "." in raw and all(part.isdigit() for part in raw.split(".", 1) if part):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except Exception:
        pass

    # ISO 8601-ish.
    try:
        s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(s)
    except Exception:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def zep_message_timestamp(msg: Dict[str, Any]) -> Optional[datetime]:
    if not isinstance(msg, dict):
        return None
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    for key in ("timestamp", "created_at", "createdAt"):
        dt = parse_timestamp(meta.get(key)) if meta else None
        if dt:
            return dt
        dt = parse_timestamp(msg.get(key))
        if dt:
            return dt
    return None


def zep_message_event_id(msg: Dict[str, Any]) -> str:
    """
    Best-effort stable message identifier from a Zep message dict.
    Prefers our recorder's metadata fields.
    """
    if not isinstance(msg, dict):
        return ""
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    for k in ("event_id", "message_id", "messageId"):
        v = str((meta or {}).get(k) or "").strip()
        if v:
            return v
    v = str(msg.get("id") or "").strip()
    if v:
        return v

    blob = "|".join(
        [
            str(msg.get("role") or ""),
            str(msg.get("content") or ""),
            str((meta or {}).get("timestamp") or ""),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

