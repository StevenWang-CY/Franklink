from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.database.client import DatabaseClient
from app.groupchat.summary.utils import parse_timestamp, utc_iso


async def fetch_recent_messages(
    db: DatabaseClient,
    *,
    chat_guid: str,
    limit: int = 120,
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 120), 500))
    return await db.get_group_chat_raw_messages_window_v1(chat_guid=chat_guid, limit=limit)


async def load_participants(
    db: DatabaseClient,
    *,
    chat_guid: str,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    try:
        chat = await db.get_group_chat_by_guid(chat_guid)
    except Exception:
        chat = None
    if not isinstance(chat, dict):
        return None, "user a", "user b"

    user_a_name = "user a"
    user_b_name = "user b"
    try:
        user_a_id = str(chat.get("user_a_id") or "").strip()
        user_b_id = str(chat.get("user_b_id") or "").strip()
        if user_a_id:
            user_a = await db.get_user_by_id(user_a_id)
            user_a_name = str((user_a or {}).get("name") or "").strip() or user_a_name
        if user_b_id:
            user_b = await db.get_user_by_id(user_b_id)
            user_b_name = str((user_b or {}).get("name") or "").strip() or user_b_name
    except Exception:
        pass
    return chat, user_a_name, user_b_name


async def build_summary_segments(
    db: DatabaseClient,
    *,
    chat_guid: str,
    limit: int = 200,
    window_days: int = 7,
    now: Optional[datetime] = None,
) -> List[str]:
    if now is None:
        now = datetime.now(timezone.utc)
    window_days = max(1, int(window_days or 7))
    since = now - timedelta(days=window_days)
    try:
        segments = await db.get_group_chat_summary_segments_v1(
            chat_guid=chat_guid,
            start_at=utc_iso(since),
            limit=limit,
        )
    except Exception:
        segments = []

    parts: List[str] = []
    total = 0
    for seg in segments or []:
        md = str(seg.get("summary_md") or "").strip()
        if not md:
            continue
        end_at = str(seg.get("segment_end_at") or "").strip()
        end_dt = parse_timestamp(end_at) if end_at else None
        if end_dt and end_dt < since:
            continue
        header = f"### segment_end_at={end_at}" if end_at else "### segment"
        block = f"{header}\n{md}".strip()
        if not block:
            continue
        total += len(block)
        if total > 6000 and parts:
            break
        parts.append(block)
    return parts
