"""Group chat runtime (routing + handler dispatch).

Note: keep imports lazy to avoid circular import issues when importing submodules
like `app.groupchat.runtime.types`.
"""

__all__ = ["GroupChatRouter"]


def __getattr__(name: str):  # pragma: no cover
    if name == "GroupChatRouter":
        from app.groupchat.runtime.router import GroupChatRouter

        return GroupChatRouter
    raise AttributeError(name)
