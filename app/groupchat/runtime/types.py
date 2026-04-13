from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class GroupChatEvent:
    """
    Normalized group chat inbound event used by the group chat runtime.

    This is intentionally stable and decoupled from Photon payload shape.
    """

    chat_guid: str
    event_id: str
    message_id: Optional[str]
    timestamp: Optional[str]
    sender_handle: Optional[str]
    sender_user_id: Optional[str]
    sender_name: Optional[str]
    resolved_participant: str  # "user_a" | "user_b" | "unknown"
    text: str
    media_url: Optional[str] = None
    raw_payload: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class GroupChatManagedContext:
    """
    Context about a Franklink-managed group chat loaded from persistent stores.
    """

    chat_guid: str
    user_a_id: str
    user_b_id: str
    user_a_mode: Optional[str] = None
    user_b_mode: Optional[str] = None
    connection_request_id: Optional[str] = None
