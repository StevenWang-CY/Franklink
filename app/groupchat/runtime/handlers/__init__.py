from __future__ import annotations

from typing import Protocol

from app.groupchat.runtime.deps import GroupChatRuntimeDeps
from app.groupchat.runtime.types import GroupChatEvent, GroupChatManagedContext


class GroupChatHandler(Protocol):
    name: str

    async def handle(
        self,
        *,
        event: GroupChatEvent,
        managed: GroupChatManagedContext | None,
        deps: GroupChatRuntimeDeps,
    ) -> bool:
        """
        Return True if the event was handled/claimed (including "ignore" decisions).
        """
        ...
