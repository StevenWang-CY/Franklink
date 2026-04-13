"""Handshake manager for connection request lifecycle.

Manages the handshake flow for networking:
1. PENDING_INITIATOR_APPROVAL - Match found, awaiting initiator confirmation
2. PENDING_TARGET_APPROVAL - Initiator confirmed, awaiting target response
3. TARGET_ACCEPTED - Target accepted, ready for group chat
4. GROUP_CREATED - Group chat created, connection complete
5. TARGET_DECLINED / CANCELLED / EXPIRED - Terminal failure states

Now integrates with AtomicStateManager to track user-level flow state
and prevent race conditions across the networking flow.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.database.client import DatabaseClient
from app.database.models import ConnectionRequestStatus
from app.agents.state import (
    AtomicStateManager,
    AtomicNetworkingState,
    NetworkingFlowState,
    InvalidTransitionError,
)

logger = logging.getLogger(__name__)


class HandshakeManager:
    """Manages connection request handshake lifecycle.

    Wraps database operations for connection requests with
    proper status transitions and validation.

    Now integrates AtomicStateManager to track user-level flow state,
    preventing race conditions from double-taps or webhook retries.
    """

    def __init__(self, db: Optional[DatabaseClient] = None):
        """Initialize the handshake manager.

        Args:
            db: Database client (creates one if not provided)
        """
        self.db = db or DatabaseClient()
        self._state_manager = AtomicStateManager(self.db)

    async def _update_user_state(
        self,
        user_id: str,
        transition_fn: str,
        **kwargs: Any,
    ) -> bool:
        """Safely update user's atomic networking state.

        This is a helper that handles the read-modify-write cycle with
        proper error handling. If the transition fails (invalid or race
        condition), the operation should be retried or aborted.

        Args:
            user_id: User ID to update
            transition_fn: Name of the transition method on AtomicNetworkingState
            **kwargs: Additional arguments to pass to the transition method

        Returns:
            True if update succeeded, False otherwise
        """
        try:
            state = await self._state_manager.get_state(user_id)
            method = getattr(state, transition_fn, None)
            if not method:
                logger.error(f"[HANDSHAKE] Unknown transition: {transition_fn}")
                return False

            new_state = method(**kwargs)
            return await self._state_manager.update_state(user_id, new_state)
        except InvalidTransitionError as e:
            logger.warning(f"[HANDSHAKE] Invalid state transition for {user_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"[HANDSHAKE] Failed to update user state: {e}")
            return False

    async def get_user_flow_state(self, user_id: str) -> AtomicNetworkingState:
        """Get the current atomic networking state for a user.

        Args:
            user_id: User ID to check

        Returns:
            Current AtomicNetworkingState
        """
        return await self._state_manager.get_state(user_id)

    async def create_request(
        self,
        initiator_id: str,
        match_result: Any,  # MatchResult from value_exchange_matcher
        excluded_candidates: Optional[List[str]] = None,
        signal_group_id: Optional[str] = None,
        is_multi_match: bool = False,
        multi_match_threshold: int = 1,
        connection_purpose: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new connection request.

        Also updates the initiator's atomic networking state to track the flow.

        Args:
            initiator_id: The initiator's user ID
            match_result: MatchResult with target info and match details
            excluded_candidates: Previously rejected user IDs
            signal_group_id: UUID linking multiple requests in a multi-match group
            is_multi_match: Whether this is part of a multi-match request
            multi_match_threshold: Number of acceptances needed to create group (default 1)
            connection_purpose: The initiator's purpose/goal for connecting (e.g., "algo trading study group")

        Returns:
            Created connection request data
        """
        try:
            request = await self.db.create_connection_request(
                initiator_user_id=initiator_id,
                target_user_id=match_result.target_user_id,
                match_score=match_result.match_score,
                matching_reasons=match_result.matching_reasons,
                llm_introduction=match_result.llm_introduction,
                llm_concern=match_result.llm_concern,
                excluded_candidates=excluded_candidates,
                signal_group_id=signal_group_id,
                is_multi_match=is_multi_match,
                multi_match_threshold=multi_match_threshold,
                connection_purpose=connection_purpose,
            )

            request_id = request.get("id")

            # Update initiator's atomic state to PENDING_INITIATOR_APPROVAL
            # This tracks their flow state at the user level (not just request level)
            try:
                state = await self._state_manager.get_state(initiator_id)

                # If user is in a terminal state, reset to IDLE first to allow new flow
                if state.is_terminal():
                    logger.info(
                        f"[HANDSHAKE] Resetting terminal state {state.flow_state.value} "
                        f"to IDLE for new networking request"
                    )
                    state = state.reset()
                    await self._state_manager.update_state(initiator_id, state)
                    # Reload fresh state after reset
                    state = await self._state_manager.get_state(initiator_id)

                # If user is IDLE, transition to MATCHING first, then to PENDING_INITIATOR_APPROVAL
                if state.flow_state == NetworkingFlowState.IDLE:
                    state = state.start_matching(initiator_id)

                # Now transition to PENDING_INITIATOR_APPROVAL with match details
                match_details = {
                    "target_user_id": match_result.target_user_id,
                    "target_name": match_result.target_name,
                    "match_score": match_result.match_score,
                    "matching_reasons": match_result.matching_reasons,
                }

                if is_multi_match:
                    # For multi-match, we might have multiple requests
                    existing_ids = list(state.request_ids) if state.request_ids else []
                    existing_targets = list(state.target_ids) if state.target_ids else []
                    existing_details = list(state.match_details) if state.match_details else []

                    new_state = state.with_multi_match(
                        request_ids=existing_ids + [request_id],
                        target_ids=existing_targets + [match_result.target_user_id],
                        match_details=existing_details + [match_details],
                        connection_purpose=connection_purpose,
                    )
                else:
                    new_state = state.with_match(
                        request_id=request_id,
                        target_id=match_result.target_user_id,
                        match_details=match_details,
                        connection_purpose=connection_purpose,
                    )

                await self._state_manager.update_state(initiator_id, new_state)
            except InvalidTransitionError as e:
                # Log but don't fail - the DB request was created successfully
                logger.warning(f"[HANDSHAKE] Could not update atomic state: {e}")
            except Exception as e:
                logger.warning(f"[HANDSHAKE] Atomic state update failed: {e}")

            logger.info(
                f"[HANDSHAKE] Created request {request_id}: "
                f"{initiator_id} -> {match_result.target_user_id}"
                f"{' (multi-match, group=' + signal_group_id[:8] + ')' if is_multi_match else ''}"
            )

            return request

        except Exception as e:
            logger.error(f"[HANDSHAKE] create_request failed: {e}", exc_info=True)
            raise

    async def get_pending_for_initiator(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get pending connection request for a user as initiator.

        Args:
            user_id: User ID to check

        Returns:
            Pending request data or None
        """
        try:
            return await self.db.get_pending_request_for_user(
                user_id=user_id,
                as_initiator=True,
            )
        except Exception as e:
            logger.error(f"[HANDSHAKE] get_pending_for_initiator failed: {e}", exc_info=True)
            return None

    async def get_pending_for_target(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get pending connection request for a user as target.

        Args:
            user_id: User ID to check

        Returns:
            Pending request data or None
        """
        try:
            return await self.db.get_pending_request_for_target(
                target_user_id=user_id,
            )
        except Exception as e:
            logger.error(f"[HANDSHAKE] get_pending_for_target failed: {e}", exc_info=True)
            return None

    async def initiator_confirms(self, request_id: str) -> Dict[str, Any]:
        """Initiator confirms the match.

        Transitions: PENDING_INITIATOR_APPROVAL -> PENDING_TARGET_APPROVAL

        Uses optimistic locking to prevent race conditions.
        Also updates the initiator's atomic networking state.

        Args:
            request_id: Connection request ID

        Returns:
            Updated request data

        Raises:
            ValueError: If status transition fails (e.g., already confirmed)
        """
        try:
            request = await self.db.update_connection_request_status(
                request_id=request_id,
                status=ConnectionRequestStatus.PENDING_TARGET_APPROVAL,
                additional_updates={
                    "target_notified_at": datetime.utcnow().isoformat(),
                },
                expected_current_status=ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL,
            )

            # Update initiator's atomic state to PENDING_TARGET_RESPONSE
            initiator_id = request.get("initiator_user_id")
            if initiator_id:
                try:
                    state = await self._state_manager.get_state(initiator_id)
                    if state.flow_state == NetworkingFlowState.PENDING_INITIATOR_APPROVAL:
                        new_state = state.initiator_confirmed()
                        await self._state_manager.update_state(initiator_id, new_state)
                except InvalidTransitionError as e:
                    logger.warning(f"[HANDSHAKE] Could not update atomic state on confirm: {e}")
                except Exception as e:
                    logger.warning(f"[HANDSHAKE] Atomic state update failed on confirm: {e}")

            logger.info(f"[HANDSHAKE] Initiator confirmed request {request_id}")

            return request

        except Exception as e:
            logger.error(f"[HANDSHAKE] initiator_confirms failed: {e}", exc_info=True)
            raise

    async def initiator_requests_different(
        self,
        request_id: str,
        current_target_id: str,
    ) -> Dict[str, Any]:
        """Initiator requests a different match.

        Cancels the current request and adds target to exclusion list.
        Uses optimistic locking to prevent race conditions.

        Args:
            request_id: Connection request ID
            current_target_id: Target to exclude

        Returns:
            Updated request data

        Raises:
            ValueError: If request is not in PENDING_INITIATOR_APPROVAL status
        """
        try:
            # Add to exclusion list
            await self.db.add_excluded_candidate(
                request_id=request_id,
                candidate_id=current_target_id,
            )

            # Cancel the request with optimistic lock
            request = await self.db.update_connection_request_status(
                request_id=request_id,
                status=ConnectionRequestStatus.CANCELLED,
                expected_current_status=ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL,
            )

            logger.info(
                f"[HANDSHAKE] Initiator requested different match for {request_id}, "
                f"excluded {current_target_id}"
            )

            return request

        except Exception as e:
            logger.error(f"[HANDSHAKE] initiator_requests_different failed: {e}", exc_info=True)
            raise

    async def initiator_cancels(self, request_id: str) -> Dict[str, Any]:
        """Initiator cancels the networking request.

        Uses optimistic locking - only cancels if still in PENDING_INITIATOR_APPROVAL.
        Also updates the initiator's atomic state to CANCELLED.

        Args:
            request_id: Connection request ID

        Returns:
            Updated request data

        Raises:
            ValueError: If request is not in PENDING_INITIATOR_APPROVAL status
        """
        try:
            request = await self.db.update_connection_request_status(
                request_id=request_id,
                status=ConnectionRequestStatus.CANCELLED,
                expected_current_status=ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL,
            )

            # Update initiator's atomic state to CANCELLED
            initiator_id = request.get("initiator_user_id")
            if initiator_id:
                try:
                    state = await self._state_manager.get_state(initiator_id)
                    if state.is_active():
                        new_state = state.cancel("User cancelled")
                        await self._state_manager.update_state(initiator_id, new_state)
                except InvalidTransitionError as e:
                    logger.warning(f"[HANDSHAKE] Could not update atomic state on cancel: {e}")
                except Exception as e:
                    logger.warning(f"[HANDSHAKE] Atomic state update failed on cancel: {e}")

            logger.info(f"[HANDSHAKE] Initiator cancelled request {request_id}")

            return request

        except Exception as e:
            logger.error(f"[HANDSHAKE] initiator_cancels failed: {e}", exc_info=True)
            raise

    async def target_accepts(self, request_id: str) -> Dict[str, Any]:
        """Target accepts the connection request.

        Transitions: PENDING_TARGET_APPROVAL -> TARGET_ACCEPTED
        Uses optimistic locking to prevent race conditions.
        Also updates the initiator's atomic state to READY_FOR_GROUP.

        Args:
            request_id: Connection request ID

        Returns:
            Updated request data

        Raises:
            ValueError: If request is not in PENDING_TARGET_APPROVAL status
        """
        try:
            request = await self.db.update_connection_request_status(
                request_id=request_id,
                status=ConnectionRequestStatus.TARGET_ACCEPTED,
                additional_updates={
                    "target_responded_at": datetime.utcnow().isoformat(),
                },
                expected_current_status=ConnectionRequestStatus.PENDING_TARGET_APPROVAL,
            )

            # Update initiator's atomic state to READY_FOR_GROUP
            initiator_id = request.get("initiator_user_id")
            if initiator_id:
                try:
                    state = await self._state_manager.get_state(initiator_id)
                    if state.flow_state == NetworkingFlowState.PENDING_TARGET_RESPONSE:
                        new_state = state.target_accepted()
                        await self._state_manager.update_state(initiator_id, new_state)
                except InvalidTransitionError as e:
                    logger.warning(f"[HANDSHAKE] Could not update atomic state on accept: {e}")
                except Exception as e:
                    logger.warning(f"[HANDSHAKE] Atomic state update failed on accept: {e}")

            logger.info(f"[HANDSHAKE] Target accepted request {request_id}")

            return request

        except Exception as e:
            logger.error(f"[HANDSHAKE] target_accepts failed: {e}", exc_info=True)
            raise

    async def target_declines(
        self,
        request_id: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Target declines the connection request.

        Transitions: PENDING_TARGET_APPROVAL -> TARGET_DECLINED
        Uses optimistic locking to prevent race conditions.
        Also updates the initiator's atomic state to CANCELLED.

        Args:
            request_id: Connection request ID
            reason: Optional decline reason

        Returns:
            Updated request data

        Raises:
            ValueError: If request is not in PENDING_TARGET_APPROVAL status
        """
        try:
            additional_updates = {
                "target_responded_at": datetime.utcnow().isoformat(),
            }

            # Store decline reason if provided
            if reason:
                additional_updates["decline_reason"] = reason
                logger.info(f"[HANDSHAKE] Decline reason for {request_id}: {reason}")

            request = await self.db.update_connection_request_status(
                request_id=request_id,
                status=ConnectionRequestStatus.TARGET_DECLINED,
                additional_updates=additional_updates,
                expected_current_status=ConnectionRequestStatus.PENDING_TARGET_APPROVAL,
            )

            # Update initiator's atomic state to CANCELLED (target declined)
            initiator_id = request.get("initiator_user_id")
            if initiator_id:
                try:
                    state = await self._state_manager.get_state(initiator_id)
                    if state.flow_state == NetworkingFlowState.PENDING_TARGET_RESPONSE:
                        new_state = state.cancel(f"Target declined: {reason}" if reason else "Target declined")
                        await self._state_manager.update_state(initiator_id, new_state)
                except InvalidTransitionError as e:
                    logger.warning(f"[HANDSHAKE] Could not update atomic state on decline: {e}")
                except Exception as e:
                    logger.warning(f"[HANDSHAKE] Atomic state update failed on decline: {e}")

            logger.info(f"[HANDSHAKE] Target declined request {request_id}")

            return request

        except Exception as e:
            logger.error(f"[HANDSHAKE] target_declines failed: {e}", exc_info=True)
            raise

    async def mark_group_created(
        self,
        request_id: str,
        chat_guid: str,
    ) -> Dict[str, Any]:
        """Mark that a group chat was created for this connection.

        Transitions: TARGET_ACCEPTED -> GROUP_CREATED
        Uses optimistic locking to prevent duplicate group creation.
        Also updates the initiator's atomic state to GROUP_CREATED.

        Args:
            request_id: Connection request ID
            chat_guid: The created group chat GUID

        Returns:
            Updated request data

        Raises:
            ValueError: If request is not in TARGET_ACCEPTED status
        """
        try:
            request = await self.db.update_connection_request_status(
                request_id=request_id,
                status=ConnectionRequestStatus.GROUP_CREATED,
                additional_updates={
                    "group_chat_guid": chat_guid,
                },
                expected_current_status=ConnectionRequestStatus.TARGET_ACCEPTED,
            )

            # Update initiator's atomic state to GROUP_CREATED (terminal state)
            initiator_id = request.get("initiator_user_id")
            if initiator_id:
                try:
                    state = await self._state_manager.get_state(initiator_id)
                    if state.flow_state == NetworkingFlowState.READY_FOR_GROUP:
                        new_state = state.group_created(chat_guid)
                        await self._state_manager.update_state(initiator_id, new_state)
                except InvalidTransitionError as e:
                    logger.warning(f"[HANDSHAKE] Could not update atomic state on group created: {e}")
                except Exception as e:
                    logger.warning(f"[HANDSHAKE] Atomic state update failed on group created: {e}")

            logger.info(
                f"[HANDSHAKE] Group created for request {request_id}: {chat_guid}"
            )

            return request

        except Exception as e:
            logger.error(f"[HANDSHAKE] mark_group_created failed: {e}", exc_info=True)
            raise

    # ========================================================================
    # Multi-match support methods
    # ========================================================================

    async def target_accepts_multi_match(self, request_id: str) -> Dict[str, Any]:
        """Target accepts a multi-match connection request.

        This checks if the multi-match threshold is met and returns
        information about whether a group should be created.

        Transitions: PENDING_TARGET_APPROVAL -> TARGET_ACCEPTED
        Uses optimistic locking to prevent race conditions.

        Args:
            request_id: Connection request ID

        Returns:
            Updated request data with multi_match_status

        Raises:
            ValueError: If request is not in PENDING_TARGET_APPROVAL status
        """
        try:
            # First, mark this request as accepted with optimistic lock
            request = await self.db.update_connection_request_status(
                request_id=request_id,
                status=ConnectionRequestStatus.TARGET_ACCEPTED,
                additional_updates={
                    "target_responded_at": datetime.utcnow().isoformat(),
                },
                expected_current_status=ConnectionRequestStatus.PENDING_TARGET_APPROVAL,
            )

            # Check if this is a multi-match request
            is_multi_match = request.get("is_multi_match", False)
            signal_group_id = request.get("signal_group_id")

            if not is_multi_match or not signal_group_id:
                # Regular single-match acceptance
                logger.info(f"[HANDSHAKE] Target accepted request {request_id} (single match)")
                request["multi_match_status"] = {
                    "is_multi_match": False,
                    "ready_for_group": True,  # Single match is always ready
                }
                return request

            # Check multi-match threshold
            check_result = await self.db.check_multi_match_ready_v1(signal_group_id)

            ready = check_result.get("ready", False)
            accepted_count = check_result.get("accepted_count", 0)
            threshold = check_result.get("threshold", 2)
            existing_chat = check_result.get("chat_guid")

            logger.info(
                f"[HANDSHAKE] Target accepted multi-match request {request_id}: "
                f"{accepted_count}/{threshold} accepted, ready={ready}, existing_chat={existing_chat}"
            )

            request["multi_match_status"] = {
                "is_multi_match": True,
                "signal_group_id": signal_group_id,
                "ready_for_group": ready,
                "accepted_count": accepted_count,
                "threshold": threshold,
                "existing_chat_guid": existing_chat,
                "accepted_request_ids": check_result.get("accepted_request_ids", []),
            }

            return request

        except Exception as e:
            logger.error(f"[HANDSHAKE] target_accepts_multi_match failed: {e}", exc_info=True)
            raise

    async def create_multi_person_group(
        self,
        signal_group_id: str,
        accepted_request_ids: List[str],
    ) -> Dict[str, Any]:
        """Create a multi-person group chat for accepted multi-match requests.

        This is called when the multi-match threshold is met.

        Args:
            signal_group_id: The signal group ID linking the requests
            accepted_request_ids: List of accepted request IDs

        Returns:
            Dict with chat_guid and participant info
        """
        try:
            from app.groupchat.features.provisioning import GroupChatService

            # Get all accepted requests
            participants = []
            initiator_id = None
            matching_reasons = []

            connection_purpose = None
            for request_id in accepted_request_ids:
                request = await self.db.get_connection_request(request_id)
                if not request:
                    continue

                if initiator_id is None:
                    initiator_id = request.get("initiator_user_id")
                    # Get connection purpose from the first request (all should have same)
                    connection_purpose = request.get("connection_purpose")

                target_id = request.get("target_user_id")
                target_user = await self.db.get_user_by_id(target_id)

                if target_user:
                    participants.append({
                        "user_id": target_id,
                        "phone": target_user.get("phone_number"),
                        "name": target_user.get("name", "friend"),
                        "is_initiator": False,
                        "request_id": request_id,
                    })

                # Collect matching reasons
                reasons = request.get("matching_reasons", [])
                for reason in reasons:
                    if reason not in matching_reasons:
                        matching_reasons.append(reason)

            # Add initiator to participants
            if initiator_id:
                initiator_user = await self.db.get_user_by_id(initiator_id)
                if initiator_user:
                    participants.insert(0, {
                        "user_id": initiator_id,
                        "phone": initiator_user.get("phone_number"),
                        "name": initiator_user.get("name", "friend"),
                        "is_initiator": True,
                        "request_id": None,
                    })

            if len(participants) < 2:
                raise ValueError("Not enough participants for multi-person group")

            # Create the group chat with connection purpose for naming
            service = GroupChatService()
            result = await service.create_multi_person_group(
                participants=participants,
                signal_group_id=signal_group_id,
                matching_reasons=matching_reasons,
                connection_purpose=connection_purpose,
            )

            chat_guid = result.get("chat_guid")

            # Update all requests with the chat_guid (with optimistic locking)
            for request_id in accepted_request_ids:
                await self.db.update_connection_request_status(
                    request_id=request_id,
                    status=ConnectionRequestStatus.GROUP_CREATED,
                    additional_updates={
                        "group_chat_guid": chat_guid,
                        "multi_match_chat_guid": chat_guid,
                    },
                    expected_current_status=ConnectionRequestStatus.TARGET_ACCEPTED,
                )

            logger.info(
                f"[HANDSHAKE] Created multi-person group {chat_guid} "
                f"with {len(participants)} participants"
            )

            return {
                "chat_guid": chat_guid,
                "participants": participants,
                "signal_group_id": signal_group_id,
            }

        except Exception as e:
            logger.error(f"[HANDSHAKE] create_multi_person_group failed: {e}", exc_info=True)
            raise

    async def add_late_joiner_to_group(
        self,
        request_id: str,
        existing_chat_guid: str,
    ) -> Dict[str, Any]:
        """Add a late-accepting target to an existing multi-match group.

        This is called when someone accepts after the group was already created.

        Args:
            request_id: The connection request ID
            existing_chat_guid: The existing group chat GUID

        Returns:
            Dict with result info
        """
        try:
            from app.groupchat.features.provisioning import GroupChatService

            # Get the request and target info
            request = await self.db.get_connection_request(request_id)
            if not request:
                raise ValueError(f"Request {request_id} not found")

            target_id = request.get("target_user_id")
            target_user = await self.db.get_user_by_id(target_id)

            if not target_user:
                raise ValueError(f"Target user {target_id} not found")

            target_phone = target_user.get("phone_number")
            target_name = target_user.get("name", "friend")

            # Get additional context for the detailed warm intro
            connection_purpose = request.get("connection_purpose")
            matching_reasons = request.get("matching_reasons", [])
            llm_introduction = request.get("llm_introduction")

            # Get existing group members' names for personalized intro
            existing_member_names = []
            signal_group_id = request.get("signal_group_id")
            if signal_group_id:
                # Get other accepted requests in this group to find existing members
                group_requests = await self.db.client.table("connection_requests").select(
                    "target_user_id, initiator_user_id"
                ).eq(
                    "signal_group_id", signal_group_id
                ).eq(
                    "status", ConnectionRequestStatus.GROUP_CREATED.value
                ).execute()

                seen_ids = set()
                for req in (group_requests.data or []):
                    for uid in [req.get("initiator_user_id"), req.get("target_user_id")]:
                        if uid and uid != target_id and uid not in seen_ids:
                            seen_ids.add(uid)
                            member = await self.db.get_user_by_id(uid)
                            if member:
                                existing_member_names.append(member.get("name", "friend"))

            # Add to the existing group with detailed intro
            service = GroupChatService()
            result = await service.add_participant_to_group(
                chat_guid=existing_chat_guid,
                user_id=target_id,
                phone=target_phone,
                name=target_name,
                connection_request_id=request_id,
                existing_member_names=existing_member_names,
                connection_purpose=connection_purpose,
                matching_reasons=matching_reasons,
                llm_introduction=llm_introduction,
            )

            # Update the request (with optimistic locking)
            await self.db.update_connection_request_status(
                request_id=request_id,
                status=ConnectionRequestStatus.GROUP_CREATED,
                additional_updates={
                    "group_chat_guid": existing_chat_guid,
                    "multi_match_chat_guid": existing_chat_guid,
                },
                expected_current_status=ConnectionRequestStatus.TARGET_ACCEPTED,
            )

            logger.info(
                f"[HANDSHAKE] Added late joiner {target_name} to group {existing_chat_guid}"
            )

            return {
                "chat_guid": existing_chat_guid,
                "added_user_id": target_id,
                "added_user_name": target_name,
            }

        except Exception as e:
            logger.error(f"[HANDSHAKE] add_late_joiner_to_group failed: {e}", exc_info=True)
            raise
