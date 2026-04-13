from __future__ import annotations

import hashlib
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.groupchat.summary.utils import parse_timestamp


def configure_followup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    level_name = str(os.getenv("GROUPCHAT_FOLLOWUP_LOG_LEVEL") or "").strip().upper()
    if not level_name:
        level_name = "DEBUG" if bool(getattr(settings, "debug", False)) else "INFO"
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    for noisy in ("httpx", "httpcore", "hpack", "h2", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def default_worker_id() -> str:
    host = socket.gethostname() or "host"
    pid = os.getpid()
    return f"{host}:{pid}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def compute_backoff_seconds(attempts: int) -> int:
    base = 30
    cap = 900
    try:
        n = int(attempts or 0)
    except Exception:
        n = 0
    return min(cap, int(base * (2 ** max(0, n))))


def extract_latest_user_anchor(messages: List[Dict[str, Any]]) -> Tuple[Optional[datetime], str]:
    best_at: Optional[datetime] = None
    best_id = ""
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "user":
            continue
        ts = parse_timestamp(msg.get("sent_at"))
        if not ts:
            continue
        if best_at is None or ts > best_at:
            best_at = ts
            best_id = str(msg.get("event_id") or "").strip()
    return best_at, best_id


def effective_group_mode(user_a_mode: Any, user_b_mode: Any) -> str:
    def normalize(value: Any) -> str:
        mode = str(value or "").strip().lower()
        return mode if mode in {"active", "quiet", "muted"} else "active"

    rank = {"active": 0, "quiet": 1, "muted": 2}
    a = normalize(user_a_mode)
    b = normalize(user_b_mode)
    return a if rank[a] >= rank[b] else b


def clean_followup(text: str) -> str:
    msg = " ".join(str(text or "").split()).strip()
    if not msg:
        return ""
    if msg.startswith(("\"", "'")) and msg.endswith(("\"", "'")) and len(msg) > 2:
        msg = msg[1:-1].strip()
    if len(msg) > 260:
        msg = msg[:260].rstrip()
    return msg


def nudge_event_id(anchor_event_id: str) -> str:
    blob = f"followup:{anchor_event_id or ''}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_inactivity_minutes() -> int:
    minutes = int(getattr(settings, "groupchat_followup_inactivity_minutes", 0) or 0)
    if minutes > 0:
        return minutes
    hours = int(getattr(settings, "groupchat_followup_inactivity_hours", 24) or 24)
    return max(1, hours * 60)
