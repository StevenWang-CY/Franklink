"""Async operation queue for long-running tasks.

Provides fire-and-forget pattern with polling for results,
learned from poke-backend's message processor pattern.

Features:
- Immediate acknowledgment to user
- Background processing without blocking webhooks
- Optional completion callbacks for automatic user notifications
- Status polling for results

Example with automatic notification:
    from app.agents.queue import (
        AsyncOperationProcessor,
        make_notification_callback,
    )

    # Create callback that messages user when done
    callback = make_notification_callback(user_phone="+1234567890")

    # Queue operation - returns immediately
    op_id = await processor.queue_operation(
        operation_type="group_chat_creation",
        user_id="user-123",
        payload={...},
        on_complete=callback,  # User gets messaged when complete!
    )
"""

from .async_processor import (
    QueuedOperation,
    OperationStatus,
    AsyncOperationProcessor,
    CompletionCallback,
)
from .handlers import register_all_handlers
from .callbacks import (
    make_notification_callback,
    make_db_notification_callback,
)

__all__ = [
    "QueuedOperation",
    "OperationStatus",
    "AsyncOperationProcessor",
    "CompletionCallback",
    "register_all_handlers",
    "make_notification_callback",
    "make_db_notification_callback",
]
