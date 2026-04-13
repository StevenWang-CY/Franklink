"""Operation handlers for the async processor.

Registers handlers for long-running operations:
- group_chat_creation: Create group chat (single or multi-person)
- multi_match_invitations: Send invitations to multiple targets

Each handler receives a QueuedOperation and optional context,
returning a result dict on success or raising on failure.
"""

import logging
from typing import Any, Dict, Optional

from app.agents.queue.async_processor import QueuedOperation

logger = logging.getLogger(__name__)


async def handle_group_chat_creation(
    operation: QueuedOperation,
    context: Optional[Any] = None,
) -> Dict[str, Any]:
    """Handle group chat creation operation.

    Payload should contain:
        - connection_request_id: The connection request ID
        - multi_match_status: Optional dict with multi-match info

    Returns:
        Dict with chat_guid and creation details
    """
    from app.groupchat.features.provisioning import GroupChatService
    from app.agents.execution.networking.utils.handshake_manager import HandshakeManager
    from app.database.client import DatabaseClient

    payload = operation.payload
    connection_request_id = payload.get("connection_request_id")
    multi_match_status = payload.get("multi_match_status")

    if not connection_request_id:
        raise ValueError("connection_request_id is required")

    db = DatabaseClient()
    handshake = HandshakeManager(db=db)

    # Get connection request data
    request_data = await db.get_connection_request(connection_request_id)
    if not request_data:
        raise ValueError(f"Connection request {connection_request_id} not found")

    # Check if this is a multi-match request
    is_multi_match = request_data.get("is_multi_match", False)
    signal_group_id = request_data.get("signal_group_id")

    # Use multi_match_status if provided, otherwise fetch fresh
    if multi_match_status is None and is_multi_match and signal_group_id:
        check_result = await db.check_multi_match_ready_v1(signal_group_id)
        multi_match_status = {
            "is_multi_match": True,
            "signal_group_id": signal_group_id,
            "ready_for_group": check_result.get("ready", False),
            "existing_chat_guid": check_result.get("chat_guid"),
            "accepted_request_ids": check_result.get("accepted_request_ids", []),
        }

    # Handle multi-match group creation
    if is_multi_match and multi_match_status:
        existing_chat = multi_match_status.get("existing_chat_guid")

        if existing_chat:
            # Late joiner - add to existing group
            result = await handshake.add_late_joiner_to_group(
                request_id=connection_request_id,
                existing_chat_guid=existing_chat,
            )
            added_name = result.get("added_user_name", "someone")
            return {
                "chat_guid": existing_chat,
                "added_user_name": added_name,
                "is_late_joiner": True,
                "operation_id": operation.operation_id,
                # User-friendly notification message
                "notification_message": f"🎉 {added_name} has joined the group chat!",
            }

        # Create new multi-person group
        accepted_ids = multi_match_status.get("accepted_request_ids", [])
        if connection_request_id not in accepted_ids:
            accepted_ids.append(connection_request_id)

        result = await handshake.create_multi_person_group(
            signal_group_id=signal_group_id,
            accepted_request_ids=accepted_ids,
        )

        participant_count = len(result.get("participants", []))
        return {
            "chat_guid": result.get("chat_guid"),
            "participant_count": participant_count,
            "is_multi_person": True,
            "operation_id": operation.operation_id,
            # User-friendly notification message
            "notification_message": f"🎉 Your group chat with {participant_count} people is ready! Check your messages.",
        }

    # Standard single-match flow
    initiator_user_id = request_data.get("initiator_user_id")
    target_user_id = request_data.get("target_user_id")
    matching_reasons = request_data.get("matching_reasons", [])

    # Look up both users
    initiator_user = await db.get_user_by_id(initiator_user_id)
    if not initiator_user or not initiator_user.get("phone_number"):
        raise ValueError(f"Could not find phone number for initiator user {initiator_user_id}")
    initiator_phone = initiator_user.get("phone_number")
    initiator_name = initiator_user.get("name", "friend")

    target_user = await db.get_user_by_id(target_user_id)
    if not target_user or not target_user.get("phone_number"):
        raise ValueError(f"Could not find phone number for target user {target_user_id}")
    target_phone = target_user.get("phone_number")
    target_name = target_user.get("name", "friend")

    # Get shared university if any
    university = None
    if initiator_user.get("university") and initiator_user.get("university") == target_user.get("university"):
        university = initiator_user.get("university")

    logger.info(f"[GROUP_CHAT_HANDLER] Creating chat: {initiator_name} <-> {target_name}")

    service = GroupChatService()
    result = await service.create_group(
        user_a_phone=initiator_phone,
        user_b_phone=target_phone,
        user_a_name=initiator_name,
        user_b_name=target_name,
        connection_request_id=connection_request_id,
        user_a_id=initiator_user_id,
        user_b_id=target_user_id,
        university=university,
        matching_reasons=matching_reasons,
    )

    chat_guid = result.get("chat_guid")

    # Mark the connection request as having group created
    await handshake.mark_group_created(connection_request_id, chat_guid)

    return {
        "chat_guid": chat_guid,
        "initiator_name": initiator_name,
        "target_name": target_name,
        "action_type": "group_chat_created",
        "operation_id": operation.operation_id,
        # User-friendly notification message
        "notification_message": f"🎉 Your group chat with {target_name} is ready! Check your messages.",
    }


async def handle_multi_match_invitations(
    operation: QueuedOperation,
    context: Optional[Any] = None,
) -> Dict[str, Any]:
    """Handle sending invitations to multiple targets.

    Used when initiator confirms multi-match - sends invitations
    to all matched targets in parallel.

    Payload should contain:
        - request_ids: List of connection request IDs to send invitations for
        - initiator_name: Name of the initiator

    Returns:
        Dict with sent_count and results
    """
    from app.agents.execution.networking.utils.message_generator import (
        generate_invitation_message,
    )
    from app.integrations.photon_client import PhotonClient
    from app.database.client import DatabaseClient
    import asyncio

    payload = operation.payload
    request_ids = payload.get("request_ids", [])
    initiator_name = payload.get("initiator_name", "someone")

    if not request_ids:
        raise ValueError("request_ids is required")

    db = DatabaseClient()
    photon = PhotonClient()
    results = []
    sent_count = 0

    async def send_invitation(request_id: str) -> Dict[str, Any]:
        """Send a single invitation."""
        try:
            # Get request data
            request_data = await db.get_connection_request(request_id)
            if not request_data:
                return {"request_id": request_id, "success": False, "error": "not_found"}

            target_user_id = request_data.get("target_user_id")
            target_user = await db.get_user_by_id(target_user_id)
            if not target_user or not target_user.get("phone_number"):
                return {"request_id": request_id, "success": False, "error": "no_phone"}

            target_phone = target_user.get("phone_number")
            target_name = target_user.get("name", "there")
            matching_reasons = request_data.get("matching_reasons", [])

            # Generate and send message
            message = await generate_invitation_message(
                initiator_name=initiator_name,
                target_name=target_name,
                matching_reasons=matching_reasons,
            )

            await photon.send_message(to_number=target_phone, content=message)

            # Store in target's conversation history
            await db.store_message(
                user_id=target_user_id,
                content=message,
                message_type="bot",
                metadata={
                    "intent": "networking_invitation",
                    "connection_request_id": request_id,
                    "initiator_name": initiator_name,
                },
            )

            return {
                "request_id": request_id,
                "success": True,
                "target_name": target_name,
            }

        except Exception as e:
            logger.error(f"[MULTI_INVITE_HANDLER] Failed to send invitation {request_id}: {e}")
            return {"request_id": request_id, "success": False, "error": str(e)}

    # Send all invitations in parallel
    tasks = [send_invitation(req_id) for req_id in request_ids]
    results = await asyncio.gather(*tasks)

    sent_count = sum(1 for r in results if r.get("success"))

    logger.info(f"[MULTI_INVITE_HANDLER] Sent {sent_count}/{len(request_ids)} invitations")

    # Build list of target names for notification
    target_names = [r.get("target_name") for r in results if r.get("success") and r.get("target_name")]
    names_str = ", ".join(target_names[:3])  # Show first 3 names
    if len(target_names) > 3:
        names_str += f" and {len(target_names) - 3} more"

    return {
        "sent_count": sent_count,
        "total_count": len(request_ids),
        "results": results,
        "operation_id": operation.operation_id,
        # User-friendly notification message
        "notification_message": (
            f"✅ Sent invitations to {names_str}. I'll let you know when they respond!"
            if target_names
            else f"✅ Sent {sent_count} invitation(s). I'll let you know when they respond!"
        ),
    }


def register_all_handlers(processor: "AsyncOperationProcessor") -> None:
    """Register all operation handlers with the processor.

    Call this on application startup after creating the processor.

    Args:
        processor: The AsyncOperationProcessor instance
    """
    from app.agents.queue.async_processor import AsyncOperationProcessor

    processor.register_handler("group_chat_creation", handle_group_chat_creation)
    processor.register_handler("multi_match_invitations", handle_multi_match_invitations)

    logger.info("[HANDLERS] Registered all operation handlers")
