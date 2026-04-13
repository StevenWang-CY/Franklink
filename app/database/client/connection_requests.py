"""Internal database client implementation.

This package splits the Supabase DatabaseClient into focused mixins.
"""

import logging
import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from uuid import UUID

from postgrest.exceptions import APIError

from .retry import with_retry

from app.database.models import ConnectionRequestStatus

logger = logging.getLogger(__name__)


# Valid status transitions for connection requests
VALID_STATUS_TRANSITIONS = {
    ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL: [
        ConnectionRequestStatus.PENDING_TARGET_APPROVAL,  # Initiator confirms
        ConnectionRequestStatus.CANCELLED,  # Initiator cancels or requests different
    ],
    ConnectionRequestStatus.PENDING_TARGET_APPROVAL: [
        ConnectionRequestStatus.TARGET_ACCEPTED,  # Target accepts
        ConnectionRequestStatus.TARGET_DECLINED,  # Target declines
        ConnectionRequestStatus.CANCELLED,  # Expired or cancelled
    ],
    ConnectionRequestStatus.TARGET_ACCEPTED: [
        ConnectionRequestStatus.GROUP_CREATED,  # Group chat created
        ConnectionRequestStatus.CANCELLED,  # Something went wrong
    ],
    # Terminal states - no transitions allowed
    ConnectionRequestStatus.GROUP_CREATED: [],
    ConnectionRequestStatus.TARGET_DECLINED: [],
    ConnectionRequestStatus.CANCELLED: [],
    ConnectionRequestStatus.EXPIRED: [],
}


class _ConnectionRequestMethods:
    async def get_networking_state(
        self,
        user_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Get the atomic networking state for a user.

        Args:
            user_id: User ID to get state for

        Returns:
            State dictionary or None if no state exists
        """
        try:
            result = self.client.table("user_networking_states").select("*").eq(
                "user_id", user_id
            ).execute()

            if result.data:
                return result.data[0].get("state_data")
            return None

        except Exception as e:
            logger.error(f"Error getting networking state for {user_id}: {e}")
            return None

    async def upsert_networking_state(
        self,
        user_id: str,
        state: Dict[str, Any],
        expected_version: int,
    ) -> bool:
        """
        Upsert the atomic networking state with optimistic locking.

        Uses version number to prevent race conditions. The update only
        succeeds if the current version matches expected_version.

        Args:
            user_id: User ID to update state for
            state: State dictionary to persist
            expected_version: Expected current version (for optimistic lock)

        Returns:
            True if update succeeded, False if version mismatch

        Raises:
            Exception: On database errors other than version mismatch
        """
        try:
            now = datetime.utcnow().isoformat()

            # Try to update existing record with version check
            if expected_version > 0:
                result = self.client.table("user_networking_states").update({
                    "state_data": state,
                    "updated_at": now,
                }).eq(
                    "user_id", user_id
                ).eq(
                    "state_data->>version", str(expected_version)
                ).execute()

                if result.data:
                    logger.info(f"Updated networking state for {user_id} to version {state.get('version')}")
                    return True

                # Version mismatch - concurrent modification
                logger.warning(f"Version mismatch updating networking state for {user_id}")
                return False

            # First time - insert new record
            result = self.client.table("user_networking_states").upsert({
                "user_id": user_id,
                "state_data": state,
                "created_at": now,
                "updated_at": now,
            }, on_conflict="user_id").execute()

            logger.info(f"Created networking state for {user_id}")
            return True

        except Exception as e:
            logger.error(f"Error upserting networking state for {user_id}: {e}")
            raise

    async def create_connection_request(
        self,
        initiator_user_id: str,
        target_user_id: str,
        match_score: Optional[float] = None,
        matching_reasons: Optional[List[str]] = None,
        llm_introduction: Optional[str] = None,
        llm_concern: Optional[str] = None,
        excluded_candidates: Optional[List[str]] = None,
        signal_group_id: Optional[str] = None,
        is_multi_match: bool = False,
        multi_match_threshold: int = 1,
        connection_purpose: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new connection request for handshake flow.

        Args:
            initiator_user_id: User A who requested the match
            target_user_id: User B who is being matched
            match_score: Similarity/match score
            matching_reasons: List of reasons for the match
            llm_introduction: LLM-generated introduction
            llm_concern: LLM-generated concern about the match
            excluded_candidates: List of previously excluded user IDs
            signal_group_id: UUID linking multiple requests in a multi-match group
            is_multi_match: Whether this is part of a multi-match request
            multi_match_threshold: Number of acceptances needed to create group (default 1)
            connection_purpose: The initiator's purpose/goal for connecting (e.g., "algo trading study group")

        Returns:
            Created connection request data
        """
        try:
            request_data = {
                "initiator_user_id": initiator_user_id,
                "target_user_id": target_user_id,
                "match_score": match_score,
                "matching_reasons": matching_reasons or [],
                "llm_introduction": llm_introduction,
                "llm_concern": llm_concern,
                "status": ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL.value,
                "excluded_candidates": excluded_candidates or [],
                "expires_at": (datetime.utcnow() + timedelta(days=3)).isoformat(),
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }

            # Add connection purpose if provided (used for group naming)
            if connection_purpose:
                request_data["connection_purpose"] = connection_purpose

            # Add multi-match fields if this is a multi-match request
            if is_multi_match and signal_group_id:
                request_data["signal_group_id"] = signal_group_id
                request_data["is_multi_match"] = True
                request_data["multi_match_threshold"] = multi_match_threshold

            result = self.client.table("connection_requests").insert(request_data).execute()
            logger.info(
                f"Created connection request: {initiator_user_id} -> {target_user_id}"
                f"{' (multi-match)' if is_multi_match else ''}"
            )
            return result.data[0]

        except Exception as e:
            logger.error(f"Error creating connection request: {e}", exc_info=True)
            raise

    async def update_connection_request_status(
        self,
        request_id: str,
        status: ConnectionRequestStatus,
        additional_updates: Optional[Dict[str, Any]] = None,
        expected_current_status: Optional[ConnectionRequestStatus] = None,
    ) -> Dict[str, Any]:
        """
        Update the status of a connection request with optimistic locking.

        Uses optimistic locking to prevent race conditions by validating the
        current status before updating. If expected_current_status is provided,
        the update will only succeed if the current status matches.

        Args:
            request_id: Connection request ID
            status: New status to set
            additional_updates: Any additional fields to update
            expected_current_status: If provided, validates current status before update

        Returns:
            Updated connection request data

        Raises:
            ValueError: If request not found or status transition is invalid
        """
        try:
            # Validate UUID format
            try:
                UUID(request_id)
            except (ValueError, TypeError):
                raise ValueError(f"Invalid request_id format: {request_id}")

            # If expected_current_status provided, validate the transition
            if expected_current_status is not None:
                valid_transitions = VALID_STATUS_TRANSITIONS.get(expected_current_status, [])
                if status not in valid_transitions:
                    raise ValueError(
                        f"Invalid status transition: {expected_current_status.value} -> {status.value}. "
                        f"Valid transitions: {[s.value for s in valid_transitions]}"
                    )

            update_data = {
                "status": status.value,
                "updated_at": datetime.utcnow().isoformat()
            }

            if additional_updates:
                update_data.update(additional_updates)

            # Build query with optimistic locking if expected_current_status provided
            query = self.client.table("connection_requests").update(
                update_data
            ).eq("id", request_id)

            if expected_current_status is not None:
                # Optimistic lock: only update if current status matches expected
                query = query.eq("status", expected_current_status.value)

            result = query.execute()

            if result.data:
                logger.info(f"Updated connection request {request_id} to {status.value}")
                return result.data[0]

            # No rows updated - either not found or status changed
            if expected_current_status is not None:
                raise ValueError(
                    f"Connection request {request_id} status transition failed. "
                    f"Expected status {expected_current_status.value} but request may have been modified concurrently."
                )
            raise ValueError(f"Connection request {request_id} not found")

        except Exception as e:
            logger.error(f"Error updating connection request: {e}", exc_info=True)
            raise

    async def get_connection_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a connection request by ID.

        Args:
            request_id: Connection request ID

        Returns:
            Connection request data or None
        """
        try:
            result = self.client.table("connection_requests").select("*").eq(
                "id", request_id
            ).execute()

            return result.data[0] if result.data else None

        except Exception as e:
            logger.error(f"Error getting connection request: {e}", exc_info=True)
            return None

    async def get_pending_request_for_user(
        self,
        user_id: str,
        as_initiator: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Get pending connection request for a user awaiting their confirmation.

        Args:
            user_id: User ID to check
            as_initiator: If True, check as initiator (awaiting their confirmation);
                         if False, check as target (awaiting their response)

        Returns:
            Pending connection request or None
        """
        try:
            column = "initiator_user_id" if as_initiator else "target_user_id"

            # Use status appropriate to the role:
            # - Initiator: only PENDING_INITIATOR_APPROVAL (waiting for them to confirm match)
            # - Target: only PENDING_TARGET_APPROVAL (waiting for them to accept/decline)
            if as_initiator:
                status = ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL.value
            else:
                status = ConnectionRequestStatus.PENDING_TARGET_APPROVAL.value

            result = self.client.table("connection_requests").select("*").eq(
                column, user_id
            ).eq(
                "status", status
            ).order(
                "created_at", desc=True
            ).gt(
                "expires_at", datetime.utcnow().isoformat()
            ).limit(1).execute()

            return result.data[0] if result.data else None

        except Exception as e:
            logger.error(f"Error getting pending request for user: {e}", exc_info=True)
            return None

    async def get_pending_request_for_target(
        self,
        target_user_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get any pending connection request awaiting target user's response.

        Args:
            target_user_id: Target user's ID

        Returns:
            Pending connection request or None
        """
        try:
            result = self.client.table("connection_requests").select("*").eq(
                "target_user_id", target_user_id
            ).eq(
                "status", ConnectionRequestStatus.PENDING_TARGET_APPROVAL.value
            ).gt(
                "expires_at", datetime.utcnow().isoformat()
            ).order("created_at", desc=True).limit(1).execute()

            return result.data[0] if result.data else None

        except Exception as e:
            logger.error(f"Error getting pending request for target: {e}", exc_info=True)
            return None

    async def list_pending_requests_for_target(
        self,
        target_user_id: str,
        *,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        List pending connection requests awaiting target user's response (newest first).

        This is used to disambiguate when a user has multiple inbound invites.
        """
        try:
            n = max(1, min(int(limit or 5), 25))
            result = (
                self.client.table("connection_requests")
                .select("*")
                .eq("target_user_id", target_user_id)
                .eq("status", ConnectionRequestStatus.PENDING_TARGET_APPROVAL.value)
                .gt("expires_at", datetime.utcnow().isoformat())
                .order("created_at", desc=True)
                .limit(n)
                .execute()
            )
            return list(result.data or [])
        except Exception as e:
            logger.error(f"Error listing pending requests for target: {e}", exc_info=True)
            return []

    async def list_pending_requests_for_initiator(
        self,
        initiator_user_id: str,
        *,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        List pending connection requests awaiting initiator confirmation (newest first).

        This is used to disambiguate when a user has multiple Frank suggestions pending.
        """
        try:
            n = max(1, min(int(limit or 5), 25))
            result = (
                self.client.table("connection_requests")
                .select("*")
                .eq("initiator_user_id", initiator_user_id)
                .eq("status", ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL.value)
                .gt("expires_at", datetime.utcnow().isoformat())
                .order("created_at", desc=True)
                .limit(n)
                .execute()
            )
            return list(result.data or [])
        except Exception as e:
            logger.error(f"Error listing pending requests for initiator: {e}", exc_info=True)
            return []

    async def check_existing_connection(
        self,
        user_a_id: str,
        user_b_id: str
    ) -> bool:
        """
        Check if two users already have an active connection or group chat.

        Args:
            user_a_id: First user ID
            user_b_id: Second user ID

        Returns:
            True if connection exists, False otherwise
        """
        try:
            # SECURITY: Validate UUIDs before string interpolation to prevent injection
            try:
                UUID(user_a_id)
                UUID(user_b_id)
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid UUID in check_existing_connection: {e}")
                return False

            # Check for existing group chat
            result = self.client.table("group_chats").select("id").or_(
                f"and(user_a_id.eq.{user_a_id},user_b_id.eq.{user_b_id}),"
                f"and(user_a_id.eq.{user_b_id},user_b_id.eq.{user_a_id})"
            ).execute()

            if result.data:
                logger.info(f"Existing group chat found between {user_a_id} and {user_b_id}")
                return True

            # Check for pending or accepted connection request
            result = self.client.table("connection_requests").select("id").or_(
                f"and(initiator_user_id.eq.{user_a_id},target_user_id.eq.{user_b_id}),"
                f"and(initiator_user_id.eq.{user_b_id},target_user_id.eq.{user_a_id})"
            ).in_(
                "status",
                [
                    ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL.value,
                    ConnectionRequestStatus.PENDING_TARGET_APPROVAL.value,
                    ConnectionRequestStatus.TARGET_ACCEPTED.value,
                    ConnectionRequestStatus.GROUP_CREATED.value
                ]
            ).gt("expires_at", datetime.utcnow().isoformat()).execute()

            if result.data:
                logger.info(f"Existing connection request found between {user_a_id} and {user_b_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"Error checking existing connection: {e}", exc_info=True)
            return False

    async def add_excluded_candidate(
        self,
        request_id: str,
        candidate_id: str
    ) -> Dict[str, Any]:
        """
        Add a candidate to the excluded list for a connection request.

        Args:
            request_id: Connection request ID
            candidate_id: User ID to exclude

        Returns:
            Updated connection request
        """
        try:
            # Get current excluded list
            request = await self.get_connection_request(request_id)
            if not request:
                raise ValueError(f"Connection request {request_id} not found")

            excluded = request.get("excluded_candidates", []) or []
            if candidate_id not in excluded:
                excluded.append(candidate_id)

            result = self.client.table("connection_requests").update({
                "excluded_candidates": excluded,
                "updated_at": datetime.utcnow().isoformat()
            }).eq("id", request_id).execute()

            logger.info(f"Added {candidate_id} to excluded candidates for request {request_id}")
            return result.data[0]

        except Exception as e:
            logger.error(f"Error adding excluded candidate: {e}", exc_info=True)
            raise

    async def get_recent_connection_purposes(
        self,
        user_id: str,
        days: int = 7,
    ) -> List[str]:
        """
        Get connection purposes from recent connection requests.

        Used for deduplication when suggesting new connection purposes.
        Returns purposes from requests in the last N days, regardless of status.

        Args:
            user_id: User ID to get purposes for
            days: Number of days to look back (default 7)

        Returns:
            List of connection purpose strings
        """
        try:
            # SECURITY: Validate UUID
            try:
                UUID(user_id)
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid UUID in get_recent_connection_purposes: {e}")
                return []

            # Calculate cutoff date
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

            # Get connection purposes from recent requests
            result = self.client.table("connection_requests").select(
                "connection_purpose"
            ).eq(
                "initiator_user_id", user_id
            ).gte(
                "created_at", cutoff
            ).not_.is_(
                "connection_purpose", "null"
            ).execute()

            if not result.data:
                return []

            # Extract unique purposes
            purposes = list(set(
                r["connection_purpose"]
                for r in result.data
                if r.get("connection_purpose")
            ))

            logger.info(
                f"Found {len(purposes)} recent connection purposes for user {user_id[:8]}"
            )
            return purposes

        except Exception as e:
            logger.error(f"Error getting recent connection purposes: {e}", exc_info=True)
            return []

    async def get_user_connections(
        self,
        user_id: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Get list of completed connections for a user.

        Returns connections where a group chat was created (GROUP_CREATED status),
        including details about who they connected with.

        Args:
            user_id: User ID to get connections for
            limit: Maximum number of connections to return

        Returns:
            List of connection records with user details
        """
        try:
            # SECURITY: Validate UUID before string interpolation
            try:
                UUID(user_id)
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid UUID in get_user_connections: {e}")
                return []

            # Get connections where user was either initiator or target
            # and the connection was completed (group created)
            result = self.client.table("connection_requests").select(
                "id, initiator_user_id, target_user_id, match_score, "
                "matching_reasons, group_chat_guid, created_at, updated_at"
            ).eq(
                "status", ConnectionRequestStatus.GROUP_CREATED.value
            ).or_(
                f"initiator_user_id.eq.{user_id},target_user_id.eq.{user_id}"
            ).order(
                "updated_at", desc=True
            ).limit(limit).execute()

            if not result.data:
                return []

            # Enrich with user names
            connections = []
            for conn in result.data:
                # Determine the other user
                is_initiator = conn["initiator_user_id"] == user_id
                other_user_id = conn["target_user_id"] if is_initiator else conn["initiator_user_id"]

                # Get other user's name
                other_user = await self.get_user_by_id(other_user_id)
                other_name = other_user.get("name", "Unknown") if other_user else "Unknown"

                connections.append({
                    "connection_id": conn["id"],
                    "connected_with_id": other_user_id,
                    "connected_with_name": other_name,
                    "user_role": "initiator" if is_initiator else "target",
                    "match_score": conn.get("match_score"),
                    "matching_reasons": conn.get("matching_reasons", []),
                    "group_chat_guid": conn.get("group_chat_guid"),
                    "connected_at": conn.get("updated_at"),
                })

            logger.info(f"Found {len(connections)} connections for user {user_id}")
            return connections

        except Exception as e:
            logger.error(f"Error getting user connections: {e}", exc_info=True)
            return []

    async def update_connection_request(
        self,
        request_id: str,
        updates: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Update a connection request with arbitrary fields.

        Args:
            request_id: Connection request ID
            updates: Fields to update

        Returns:
            Updated connection request or None
        """
        try:
            updates["updated_at"] = datetime.utcnow().isoformat()

            result = self.client.table("connection_requests").update(
                updates
            ).eq("id", request_id).execute()

            if result.data:
                logger.info(f"Updated connection request {request_id}: {list(updates.keys())}")
                return result.data[0]

            return None

        except Exception as e:
            logger.error(f"Error updating connection request: {e}", exc_info=True)
            return None

    async def check_multi_match_ready_v1(
        self,
        signal_group_id: str,
    ) -> Dict[str, Any]:
        """
        Check if multi-match threshold is met for a signal group.

        Args:
            signal_group_id: Signal group ID linking related requests

        Returns:
            Dict with ready, accepted_count, threshold, accepted_request_ids, chat_guid
        """
        try:
            result = self.client.rpc(
                "check_multi_match_ready_v1",
                {"p_signal_group_id": signal_group_id},
            ).execute()

            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]

            # Return defaults if no data
            return {
                "ready": False,
                "accepted_count": 0,
                "threshold": 2,
                "accepted_request_ids": [],
                "chat_guid": None,
            }

        except Exception as e:
            logger.error(f"Error checking multi-match ready: {e}", exc_info=True)
            return {
                "ready": False,
                "accepted_count": 0,
                "threshold": 2,
                "accepted_request_ids": [],
                "chat_guid": None,
            }
