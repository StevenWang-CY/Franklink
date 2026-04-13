"""
Group chat provisioning (create Frank-managed group chats).

Owned by app/groupchat. Networking graphs may call this to create the chat.
"""

import logging
from datetime import datetime
from typing import Dict, Any, Optional, List

from app.integrations.photon_client import PhotonClient, PhotonClientError
from app.database.client import DatabaseClient
from app.agents.execution.networking.prompts.invitation import get_welcome_prompt
from app.agents.execution.networking.utils.message_generator import generate_groupchat_welcome_message
from app.groupchat.io import GroupChatRecorder, GroupChatSender

logger = logging.getLogger(__name__)


class GroupChatServiceError(Exception):
    """Error in group chat service."""


class GroupChatService:
    """Service for creating and managing group chats."""

    def __init__(self):
        self.photon = PhotonClient()
        self.db = DatabaseClient()
        self.recorder = GroupChatRecorder(db=self.db)
        self.sender = GroupChatSender(photon=self.photon, recorder=self.recorder)

    @staticmethod
    def _build_group_display_name(user_a_name: str, user_b_name: str) -> Optional[str]:
        def first_token(value: str) -> str:
            parts = str(value or "").strip().split()
            return parts[0] if parts else ""

        a = first_token(user_a_name)
        b = first_token(user_b_name)

        if a and b:
            if a.lower() == b.lower():
                # Avoid "Alex & Alex" — fall back to full names if available.
                a_full = str(user_a_name or "").strip()
                b_full = str(user_b_name or "").strip()
                if a_full and b_full and a_full.lower() != b_full.lower():
                    return f"{a_full} & {b_full}"
            return f"{a} & {b}"
        if a:
            return a
        if b:
            return b
        return None

    async def create_group(
        self,
        user_a_phone: str,
        user_b_phone: str,
        user_a_name: str,
        user_b_name: str,
        connection_request_id: Optional[str] = None,
        user_a_id: Optional[str] = None,
        user_b_id: Optional[str] = None,
        university: Optional[str] = None,
        matching_reasons: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Create a group chat with two users and Frank.

        Args:
            user_a_phone: User A's phone number
            user_b_phone: User B's phone number
            user_a_name: User A's name for welcome message
            user_b_name: User B's name for welcome message
            connection_request_id: Associated connection request ID
            user_a_id: User A's ID for database record
            user_b_id: User B's ID for database record
            university: Shared university for personalized welcome
            matching_reasons: Why these two people are a good match

        Returns:
            Dict with chat_guid and created record

        Raises:
            GroupChatServiceError: If group creation fails
        """
        welcome_message = await generate_groupchat_welcome_message(
            user_a_name=user_a_name,
            user_b_name=user_b_name,
            matching_reasons=matching_reasons,
        )
        if not welcome_message:
            logger.warning(
                "[GROUPCHAT] LLM welcome message failed, using fallback template. "
                "matching_reasons=%s", matching_reasons
            )
            welcome_message = get_welcome_prompt(
                user_a_name,
                user_b_name,
                university=university,
                matching_reasons=matching_reasons,
            )

        try:
            # Create group chat via Photon
            # The addresses list includes both users; Frank is implicitly included
            # as the sender (the Photon server is Frank's Mac)
            result = await self.photon.create_group_chat(
                addresses=[user_a_phone, user_b_phone],
                message=welcome_message
            )

            chat_guid = result.get("chat_guid")
            if not chat_guid:
                raise GroupChatServiceError(
                    "Photon did not return a chat GUID"
                )

            # Best-effort: rename the group chat to "UserA & UserB".
            try:
                display_name = self._build_group_display_name(user_a_name, user_b_name)
                if display_name:
                    await self.photon.update_chat(chat_guid, display_name=display_name)
                    logger.info("[GROUPCHAT] Renamed chat=%s to %s", str(chat_guid)[:40], display_name)
            except Exception as e:
                logger.warning("[GROUPCHAT] Failed to rename group chat: %s", e, exc_info=True)

            logger.info(f"Created group chat: {chat_guid}")

            # Initialize Zep thread immediately for this chat (best-effort).
            # This ensures long-term memory is ready even if later writes fail.
            try:
                if hasattr(self, 'memory') and self.memory is not None:
                    base_meta = {
                        "kind": "groupchat",
                        "chat_guid": chat_guid,
                        "participants": {
                            "user_a": {"id": str(user_a_id or ""), "name": user_a_name},
                            "user_b": {"id": str(user_b_id or ""), "name": user_b_name},
                        },
                        "connection_request_id": str(connection_request_id or ""),
                        "created_at": datetime.utcnow().isoformat(),
                    }
                    thread_id = await self.memory.ensure_thread(chat_guid=chat_guid, metadata=base_meta)
                    if not thread_id:
                        logger.warning("[GROUPCHAT][ZEP] Failed to initialize thread for chat=%s", str(chat_guid)[:40])
            except Exception as e:
                logger.warning("[GROUPCHAT][ZEP] Failed to initialize thread: %s", e)

            # Store in database if we have user IDs
            db_record = None
            if user_a_id and user_b_id:
                try:
                    db_record = await self.db.create_group_chat_record(
                        chat_guid=chat_guid,
                        user_a_id=user_a_id,
                        user_b_id=user_b_id,
                        connection_request_id=connection_request_id
                    )
                    logger.info(f"Stored group chat record in database")
                except Exception as e:
                    # Log but don't fail - the chat was created successfully
                    logger.error(
                        f"Failed to store group chat record: {e}",
                        exc_info=True
                    )

            try:
                await self.recorder.record_outbound(
                    chat_guid=chat_guid,
                    content=welcome_message,
                    metadata={"type": "warm_intro"},
                )
            except Exception as e:
                logger.warning("[GROUPCHAT] Failed to record warm intro: %s", e)

            return {
                "chat_guid": chat_guid,
                "welcome_message": welcome_message,
                "db_record": db_record,
                "photon_response": result.get("data")
            }

        except PhotonClientError as e:
            logger.error(f"Photon error creating group chat: {e}", exc_info=True)
            raise GroupChatServiceError(f"Failed to create group chat: {e}") from e
        except Exception as e:
            logger.error(f"Error creating group chat: {e}", exc_info=True)
            raise GroupChatServiceError(f"Unexpected error: {e}") from e

    async def get_group_by_guid(self, chat_guid: str) -> Optional[Dict[str, Any]]:
        """
        Get group chat info by GUID.

        Args:
            chat_guid: The chat GUID

        Returns:
            Group chat record or None
        """
        return await self.db.get_group_chat_by_guid(chat_guid)

    async def get_group_for_users(self, user_a_id: str, user_b_id: str) -> Optional[Dict[str, Any]]:
        """
        Get existing group chat between two users.

        Args:
            user_a_id: First user ID
            user_b_id: Second user ID

        Returns:
            Group chat record or None
        """
        return await self.db.get_group_chat_for_users(user_a_id, user_b_id)

    # ========================================================================
    # Multi-person group chat support
    # ========================================================================

    @staticmethod
    def _build_multi_person_display_name(
        names: List[str],
        connection_purpose: Optional[str] = None,
    ) -> Optional[str]:
        """Build display name for multi-person group.

        Args:
            names: List of participant first names
            connection_purpose: The initiator's goal (e.g., "algo trading study group")

        Returns:
            Display name based on purpose or names like "Alex, Ben & Chris"
        """
        # If we have a connection purpose, use it for the group name
        if connection_purpose:
            # Capitalize first letter, keep rest lowercase for casual feel
            # Truncate if too long for iMessage (max ~50 chars for display)
            purpose = connection_purpose.strip()
            if len(purpose) > 50:
                purpose = purpose[:47] + "..."
            # Capitalize first letter
            if purpose:
                purpose = purpose[0].upper() + purpose[1:]
            return purpose

        if not names:
            return None

        # Get first names only
        first_names = []
        for name in names:
            parts = str(name or "").strip().split()
            if parts:
                first_names.append(parts[0])

        if not first_names:
            return None

        if len(first_names) == 1:
            return first_names[0]
        elif len(first_names) == 2:
            return f"{first_names[0]} & {first_names[1]}"
        else:
            # "Alex, Ben, Chris & Dan"
            return ", ".join(first_names[:-1]) + f" & {first_names[-1]}"

    async def create_multi_person_group(
        self,
        participants: List[Dict[str, Any]],
        signal_group_id: str,
        matching_reasons: Optional[List[str]] = None,
        connection_purpose: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a multi-person group chat with N users and Frank.

        Used for multi-match signals like study groups, cofounder search, etc.

        Args:
            participants: List of participant dicts with keys:
                - user_id: User ID
                - phone: Phone number
                - name: Display name
                - is_initiator: Whether this is the initiator
                - request_id: Optional connection request ID
            signal_group_id: The signal group ID linking these matches
            matching_reasons: Why these people were matched
            connection_purpose: The initiator's goal (used for group naming)

        Returns:
            Dict with chat_guid and participant info

        Raises:
            GroupChatServiceError: If group creation fails
        """
        from app.proactive.outreach.message_generator import (
            generate_multi_person_welcome_message,
        )

        if len(participants) < 2:
            raise GroupChatServiceError("Need at least 2 participants for group chat")

        # Get all phone numbers and names
        phones = [p["phone"] for p in participants if p.get("phone")]
        names = [p["name"] for p in participants if p.get("name")]

        if len(phones) < 2:
            raise GroupChatServiceError("Not enough valid phone numbers")

        # Generate welcome message
        welcome_message = await generate_multi_person_welcome_message(
            participant_names=names,
            signal_text=connection_purpose or (matching_reasons[0] if matching_reasons else "similar interests"),
            matching_reasons=matching_reasons or [],
        )

        if not welcome_message:
            # Fallback message
            names_text = ", ".join(names[:-1]) + f" and {names[-1]}" if len(names) > 1 else names[0]
            welcome_message = (
                f"hey everyone! connected you all because you have similar interests. "
                f"{names_text}, thought you'd benefit from knowing each other"
            )

        try:
            # Create group chat via Photon
            result = await self.photon.create_group_chat(
                addresses=phones,
                message=welcome_message
            )

            chat_guid = result.get("chat_guid")
            if not chat_guid:
                raise GroupChatServiceError("Photon did not return a chat GUID")

            # Rename the group chat using connection purpose if available
            try:
                display_name = self._build_multi_person_display_name(names, connection_purpose=connection_purpose)
                if display_name:
                    await self.photon.update_chat(chat_guid, display_name=display_name)
                    logger.info("[GROUPCHAT] Multi-person chat=%s renamed to %s", str(chat_guid)[:40], display_name)
            except Exception as e:
                logger.warning("[GROUPCHAT] Failed to rename multi-person chat: %s", e)

            logger.info(f"Created multi-person group chat: {chat_guid} with {len(participants)} participants")

            # Store participant records
            for p in participants:
                try:
                    await self.db.add_group_chat_participant(
                        chat_guid=chat_guid,
                        user_id=p["user_id"],
                        role="initiator" if p.get("is_initiator") else "member",
                        connection_request_id=p.get("request_id"),
                    )
                except Exception as e:
                    logger.warning(f"[GROUPCHAT] Failed to store participant {p.get('user_id')}: {e}")

            # Record the welcome message
            try:
                await self.recorder.record_outbound(
                    chat_guid=chat_guid,
                    content=welcome_message,
                    metadata={"type": "multi_person_intro", "signal_group_id": signal_group_id},
                )
            except Exception as e:
                logger.warning("[GROUPCHAT] Failed to record multi-person intro: %s", e)

            return {
                "chat_guid": chat_guid,
                "welcome_message": welcome_message,
                "participants": participants,
                "signal_group_id": signal_group_id,
            }

        except PhotonClientError as e:
            logger.error(f"Photon error creating multi-person group chat: {e}", exc_info=True)
            raise GroupChatServiceError(f"Failed to create multi-person group chat: {e}") from e
        except Exception as e:
            logger.error(f"Error creating multi-person group chat: {e}", exc_info=True)
            raise GroupChatServiceError(f"Unexpected error: {e}") from e

    async def add_participant_to_group(
        self,
        chat_guid: str,
        user_id: str,
        phone: str,
        name: str,
        connection_request_id: Optional[str] = None,
        existing_member_names: Optional[List[str]] = None,
        connection_purpose: Optional[str] = None,
        matching_reasons: Optional[List[str]] = None,
        llm_introduction: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Add a late-joining participant to an existing multi-person group.

        Args:
            chat_guid: The existing group chat GUID
            user_id: User ID to add
            phone: User's phone number
            name: User's display name
            connection_request_id: Optional associated connection request ID
            existing_member_names: Names of current group members (for personalized intro)
            connection_purpose: The initiator's goal for the group
            matching_reasons: Why this person was matched
            llm_introduction: LLM-generated intro about the new joiner

        Returns:
            Dict with result info

        Raises:
            GroupChatServiceError: If adding participant fails
        """
        from app.agents.execution.networking.utils.message_generator import (
            generate_late_joiner_intro,
        )

        try:
            # Add user to iMessage group via Photon
            await self.photon.add_participant(chat_guid=chat_guid, address=phone)

            # Generate detailed warm intro for the new joiner
            announcement = await generate_late_joiner_intro(
                new_joiner_name=name,
                existing_members=existing_member_names or [],
                connection_purpose=connection_purpose,
                matching_reasons=matching_reasons,
                llm_introduction=llm_introduction,
            )

            await self.sender.send_and_record(
                chat_guid=chat_guid,
                content=announcement,
                metadata={"type": "late_joiner_announcement", "user_id": user_id},
            )

            # Store participant record
            try:
                await self.db.add_group_chat_participant(
                    chat_guid=chat_guid,
                    user_id=user_id,
                    role="member",
                    connection_request_id=connection_request_id,
                )
            except Exception as e:
                logger.warning(f"[GROUPCHAT] Failed to store late joiner record: {e}")

            logger.info(f"[GROUPCHAT] Added late joiner {name} to chat {chat_guid}")

            return {
                "chat_guid": chat_guid,
                "added_user_id": user_id,
                "added_user_name": name,
            }

        except PhotonClientError as e:
            logger.error(f"Photon error adding participant: {e}", exc_info=True)
            raise GroupChatServiceError(f"Failed to add participant: {e}") from e
        except Exception as e:
            logger.error(f"Error adding participant to group: {e}", exc_info=True)
            raise GroupChatServiceError(f"Unexpected error: {e}") from e


#
# NOTE: Native iMessage polls are intentionally disabled for group chats for now.
#
