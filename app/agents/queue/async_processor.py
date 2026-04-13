"""Async operation processor for long-running tasks.

Learned from poke-backend's message_processor.py pattern:
- Fire-and-forget with UUID tracking
- Background processing loop
- Status polling for results

This allows long-running operations (group chat creation, multi-match)
to complete without blocking webhooks or causing timeouts.

Example usage:
    # Queue an operation
    op_id = await processor.queue_operation(
        operation_type="group_chat_creation",
        user_id="user-123",
        payload={"target_ids": ["user-456", "user-789"]},
    )

    # Return immediately to user
    return "Creating your group chat..."

    # Later, poll for result
    result = processor.get_operation_status(op_id)
    if result["status"] == "completed":
        group_guid = result["result"]["group_chat_guid"]
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Callable, Awaitable
from datetime import datetime, timedelta
from enum import Enum
from uuid import uuid4
import asyncio
import logging

logger = logging.getLogger(__name__)


class OperationStatus(str, Enum):
    """Status of a queued operation."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


# Type alias for completion callbacks
# Receives (operation, result_or_none, error_or_none)
CompletionCallback = Callable[
    ["QueuedOperation", Optional[Dict[str, Any]], Optional[str]],
    Awaitable[None],
]


@dataclass
class QueuedOperation:
    """A queued operation awaiting processing.

    Attributes:
        operation_id: Unique identifier for tracking
        operation_type: Type of operation (e.g., "group_chat_creation")
        user_id: User who initiated the operation
        payload: Operation-specific data
        status: Current status
        result: Operation result (when completed)
        error: Error message (when failed)
        created_at: When the operation was queued
        completed_at: When the operation finished
        expires_at: When the operation result expires (for cleanup)
        on_complete: Optional callback invoked when operation finishes
    """

    operation_id: str
    operation_type: str
    user_id: str
    payload: Dict[str, Any]
    status: OperationStatus = OperationStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    expires_at: datetime = field(
        default_factory=lambda: datetime.utcnow() + timedelta(hours=1)
    )
    on_complete: Optional["CompletionCallback"] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses."""
        return {
            "operation_id": self.operation_id,
            "operation_type": self.operation_type,
            "user_id": self.user_id,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }


# Type alias for operation handlers
OperationHandler = Callable[
    [QueuedOperation, Any], Awaitable[Dict[str, Any]]
]


class AsyncOperationProcessor:
    """Fire-and-forget async operation queue for long-running tasks.

    Provides:
    - Immediate operation_id return for tracking
    - Background processing loop
    - Status polling for results
    - Automatic cleanup of expired results

    Example:
        processor = AsyncOperationProcessor()
        processor.register_handler("group_chat_creation", create_group_handler)
        await processor.start_processing()

        # Queue operation
        op_id = await processor.queue_operation("group_chat_creation", user_id, payload)

        # Poll for result
        status = processor.get_operation_status(op_id)
    """

    def __init__(self, context: Optional[Any] = None):
        """Initialize the processor.

        Args:
            context: Shared context (db, photon, etc.) passed to handlers
        """
        self.context = context
        self._operations: Dict[str, QueuedOperation] = {}
        self._queue: asyncio.Queue[QueuedOperation] = asyncio.Queue()
        self._handlers: Dict[str, OperationHandler] = {}
        self._processing = False
        self._cleanup_interval = 300  # 5 minutes
        self._cleanup_task: Optional[asyncio.Task[None]] = None

    def register_handler(
        self,
        operation_type: str,
        handler: OperationHandler,
    ) -> None:
        """Register a handler for an operation type.

        Args:
            operation_type: Type of operation (e.g., "group_chat_creation")
            handler: Async function that processes the operation
        """
        self._handlers[operation_type] = handler
        logger.info(f"[ASYNC_QUEUE] Registered handler for {operation_type}")

    async def queue_operation(
        self,
        operation_type: str,
        user_id: str,
        payload: Dict[str, Any],
        on_complete: Optional[CompletionCallback] = None,
    ) -> str:
        """Queue an operation and return its ID immediately.

        This is the "fire" part of fire-and-forget. The caller gets
        an operation_id immediately and can poll for results later.

        Optionally provide an on_complete callback to be notified when
        the operation finishes (success or failure). This enables
        automatic user notifications without polling.

        Args:
            operation_type: Type of operation to queue
            user_id: User who initiated the operation
            payload: Operation-specific data
            on_complete: Optional async callback(operation, result, error)
                        invoked when operation completes

        Returns:
            Operation ID for tracking

        Raises:
            ValueError: If no handler is registered for the operation type
        """
        # Validate that a handler exists for this operation type
        if operation_type not in self._handlers:
            raise ValueError(
                f"No handler registered for operation type: {operation_type}. "
                f"Available types: {list(self._handlers.keys())}"
            )

        op_id = str(uuid4())
        operation = QueuedOperation(
            operation_id=op_id,
            operation_type=operation_type,
            user_id=user_id,
            payload=payload,
            on_complete=on_complete,
        )

        self._operations[op_id] = operation
        await self._queue.put(operation)

        logger.info(
            f"[ASYNC_QUEUE] Queued {operation_type} operation {op_id} for user {user_id}"
        )
        return op_id

    def get_operation_status(self, operation_id: str) -> Dict[str, Any]:
        """Get current status of an operation.

        Args:
            operation_id: Operation ID to check

        Returns:
            Status dictionary with operation details
        """
        operation = self._operations.get(operation_id)
        if not operation:
            return {"status": "not_found", "operation_id": operation_id}

        return operation.to_dict()

    def get_pending_operations_for_user(
        self,
        user_id: str,
        operation_type: Optional[str] = None,
    ) -> list[Dict[str, Any]]:
        """Get all pending operations for a user.

        Args:
            user_id: User ID to check
            operation_type: Optional filter by operation type

        Returns:
            List of pending operation dictionaries
        """
        results = []
        for op in self._operations.values():
            if op.user_id != user_id:
                continue
            if op.status not in (OperationStatus.PENDING, OperationStatus.PROCESSING):
                continue
            if operation_type and op.operation_type != operation_type:
                continue
            results.append(op.to_dict())

        return results

    async def start_processing(self) -> None:
        """Start the background processing loop.

        Call this on application startup. Runs until stop_processing()
        is called.
        """
        self._processing = True
        logger.info("[ASYNC_QUEUE] Starting operation processor...")

        # Start cleanup task and track it for proper shutdown
        self._cleanup_task = asyncio.create_task(self._cleanup_expired())

        while self._processing:
            try:
                operation = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
                await self._process_operation(operation)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"[ASYNC_QUEUE] Error in processing loop: {e}")
                await asyncio.sleep(1)

    async def stop_processing(self) -> None:
        """Stop the processing loop gracefully."""
        self._processing = False
        logger.info("[ASYNC_QUEUE] Stopping operation processor...")

        # Cancel the cleanup task to prevent orphaned coroutine
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def _process_operation(self, operation: QueuedOperation) -> None:
        """Process a single operation.

        After processing (success or failure), invokes the on_complete
        callback if one was provided. This enables automatic user
        notifications without polling.

        Args:
            operation: Operation to process
        """
        handler = self._handlers.get(operation.operation_type)
        if not handler:
            logger.error(
                f"[ASYNC_QUEUE] No handler for operation type: {operation.operation_type}"
            )
            operation.status = OperationStatus.FAILED
            operation.error = f"Unknown operation type: {operation.operation_type}"
            operation.completed_at = datetime.utcnow()
            await self._invoke_callback(operation)
            return

        logger.info(
            f"[ASYNC_QUEUE] Processing {operation.operation_type} "
            f"operation {operation.operation_id}"
        )
        operation.status = OperationStatus.PROCESSING

        try:
            result = await handler(operation, self.context)
            operation.status = OperationStatus.COMPLETED
            operation.result = result
            operation.completed_at = datetime.utcnow()

            logger.info(
                f"[ASYNC_QUEUE] Completed {operation.operation_type} "
                f"operation {operation.operation_id}"
            )

        except Exception as e:
            logger.error(
                f"[ASYNC_QUEUE] Failed {operation.operation_type} "
                f"operation {operation.operation_id}: {e}"
            )
            operation.status = OperationStatus.FAILED
            operation.error = str(e)
            operation.completed_at = datetime.utcnow()

        # Invoke completion callback (for both success and failure)
        await self._invoke_callback(operation)

    async def _invoke_callback(self, operation: QueuedOperation) -> None:
        """Invoke the operation's completion callback if present.

        Catches and logs errors to prevent callback failures from
        affecting the queue processing.

        Args:
            operation: Completed operation
        """
        if not operation.on_complete:
            return

        try:
            await operation.on_complete(
                operation,
                operation.result,
                operation.error,
            )
            logger.info(
                f"[ASYNC_QUEUE] Callback completed for operation {operation.operation_id}"
            )
        except Exception as e:
            logger.error(
                f"[ASYNC_QUEUE] Callback failed for operation {operation.operation_id}: {e}"
            )

    async def _cleanup_expired(self) -> None:
        """Periodically clean up expired operations from memory."""
        while self._processing:
            await asyncio.sleep(self._cleanup_interval)

            now = datetime.utcnow()
            expired_ids = [
                op_id
                for op_id, op in self._operations.items()
                if op.expires_at < now
            ]

            for op_id in expired_ids:
                del self._operations[op_id]

            if expired_ids:
                logger.info(
                    f"[ASYNC_QUEUE] Cleaned up {len(expired_ids)} expired operations"
                )
