"""Completion callbacks for async operations.

Provides callback functions that can be passed to queue_operation()
to automatically notify users when long-running tasks complete.

Example usage:
    from app.agents.queue import AsyncOperationProcessor
    from app.agents.queue.callbacks import create_user_notification_callback

    # Create callback that will message the user when done
    callback = create_user_notification_callback(
        user_phone="+1234567890",
        success_message="Your group chat is ready!",
    )

    # Queue operation with callback
    op_id = await processor.queue_operation(
        operation_type="group_chat_creation",
        user_id="user-123",
        payload={...},
        on_complete=callback,
    )

    # User immediately gets: "Creating your group chat..."
    # Later, when done: "Your group chat is ready!"
"""

import logging
from typing import Any, Callable, Awaitable, Dict, Optional

from app.agents.queue.async_processor import QueuedOperation

logger = logging.getLogger(__name__)


async def create_user_notification_callback(
    user_phone: str,
    success_message: Optional[str] = None,
    failure_message: Optional[str] = None,
    use_result_message: bool = True,
) -> Callable[
    [QueuedOperation, Optional[Dict[str, Any]], Optional[str]], Awaitable[None]
]:
    """Create a callback that notifies the user via iMessage when operation completes.

    The callback will send a message to the user's phone number when
    the operation finishes (success or failure).

    Args:
        user_phone: User's phone number to send notification to
        success_message: Message to send on success (overrides result message)
        failure_message: Message to send on failure (default: generic error)
        use_result_message: If True, use notification_message from result if available

    Returns:
        Async callback function to pass to queue_operation()

    Example:
        callback = await create_user_notification_callback(
            user_phone="+1234567890",
            failure_message="Sorry, something went wrong. Please try again.",
        )
    """
    from app.integrations.photon_client import PhotonClient

    async def callback(
        operation: QueuedOperation,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> None:
        """Send notification to user when operation completes."""
        photon = PhotonClient()

        if error:
            # Operation failed
            message = failure_message or (
                "Sorry, something went wrong while processing your request. "
                "Please try again or let me know if you need help!"
            )
            logger.info(
                f"[CALLBACK] Sending failure notification to {user_phone} "
                f"for operation {operation.operation_id}"
            )
        else:
            # Operation succeeded
            # Priority: explicit success_message > result notification_message > generic
            if success_message:
                message = success_message
            elif use_result_message and result and result.get("notification_message"):
                message = result["notification_message"]
            else:
                message = "✅ Your request has been processed!"

            logger.info(
                f"[CALLBACK] Sending success notification to {user_phone} "
                f"for operation {operation.operation_id}"
            )

        try:
            await photon.send_message(to_number=user_phone, content=message)
            logger.info(f"[CALLBACK] Notification sent to {user_phone}")
        except Exception as e:
            logger.error(f"[CALLBACK] Failed to send notification to {user_phone}: {e}")

    return callback


def make_notification_callback(
    user_phone: str,
    success_message: Optional[str] = None,
    failure_message: Optional[str] = None,
    use_result_message: bool = True,
) -> Callable[
    [QueuedOperation, Optional[Dict[str, Any]], Optional[str]], Awaitable[None]
]:
    """Synchronous factory for creating user notification callbacks.

    Use this when you need to create the callback synchronously (e.g., in
    non-async context). The callback itself is still async.

    Args:
        user_phone: User's phone number to send notification to
        success_message: Message to send on success (overrides result message)
        failure_message: Message to send on failure (default: generic error)
        use_result_message: If True, use notification_message from result if available

    Returns:
        Async callback function to pass to queue_operation()

    Example:
        callback = make_notification_callback(user_phone="+1234567890")
        op_id = await processor.queue_operation(..., on_complete=callback)
    """
    from app.integrations.photon_client import PhotonClient

    async def callback(
        operation: QueuedOperation,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> None:
        """Send notification to user when operation completes."""
        photon = PhotonClient()

        if error:
            message = failure_message or (
                "Sorry, something went wrong while processing your request. "
                "Please try again or let me know if you need help!"
            )
            logger.info(
                f"[CALLBACK] Sending failure notification to {user_phone} "
                f"for operation {operation.operation_id}"
            )
        else:
            if success_message:
                message = success_message
            elif use_result_message and result and result.get("notification_message"):
                message = result["notification_message"]
            else:
                message = "✅ Your request has been processed!"

            logger.info(
                f"[CALLBACK] Sending success notification to {user_phone} "
                f"for operation {operation.operation_id}"
            )

        try:
            await photon.send_message(to_number=user_phone, content=message)
            logger.info(f"[CALLBACK] Notification sent to {user_phone}")
        except Exception as e:
            logger.error(f"[CALLBACK] Failed to send notification to {user_phone}: {e}")

    return callback


def make_db_notification_callback(
    user_id: str,
    success_message: Optional[str] = None,
    failure_message: Optional[str] = None,
    use_result_message: bool = True,
) -> Callable[
    [QueuedOperation, Optional[Dict[str, Any]], Optional[str]], Awaitable[None]
]:
    """Create callback that looks up user phone from DB and sends notification.

    Use this when you have user_id but not phone number. The callback
    will look up the user's phone number from the database.

    Args:
        user_id: User ID to look up phone number for
        success_message: Message to send on success (overrides result message)
        failure_message: Message to send on failure (default: generic error)
        use_result_message: If True, use notification_message from result if available

    Returns:
        Async callback function to pass to queue_operation()

    Example:
        callback = make_db_notification_callback(user_id="user-123")
        op_id = await processor.queue_operation(..., on_complete=callback)
    """
    from app.database.client import DatabaseClient
    from app.integrations.photon_client import PhotonClient

    async def callback(
        operation: QueuedOperation,
        result: Optional[Dict[str, Any]],
        error: Optional[str],
    ) -> None:
        """Look up user phone and send notification."""
        db = DatabaseClient()
        photon = PhotonClient()

        # Look up user phone
        user = await db.get_user_by_id(user_id)
        if not user or not user.get("phone_number"):
            logger.warning(
                f"[CALLBACK] Cannot send notification: no phone for user {user_id}"
            )
            return

        user_phone = user["phone_number"]

        if error:
            message = failure_message or (
                "Sorry, something went wrong while processing your request. "
                "Please try again or let me know if you need help!"
            )
            logger.info(
                f"[CALLBACK] Sending failure notification to {user_phone} "
                f"for operation {operation.operation_id}"
            )
        else:
            if success_message:
                message = success_message
            elif use_result_message and result and result.get("notification_message"):
                message = result["notification_message"]
            else:
                message = "✅ Your request has been processed!"

            logger.info(
                f"[CALLBACK] Sending success notification to {user_phone} "
                f"for operation {operation.operation_id}"
            )

        try:
            await photon.send_message(to_number=user_phone, content=message)
            logger.info(f"[CALLBACK] Notification sent to {user_phone}")
        except Exception as e:
            logger.error(f"[CALLBACK] Failed to send notification to {user_phone}: {e}")

    return callback
