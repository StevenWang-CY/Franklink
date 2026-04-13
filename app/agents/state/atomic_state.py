"""Atomic state management for networking flows.

Provides immutable state updates, valid transition enforcement,
and optimistic locking for complex multi-step operations like
connection handshakes. Learned from poke-backend's simple status
tracking pattern but adapted for Franklink's more complex flows.

Key concepts:
- Frozen dataclasses for immutability (can't accidentally mutate state)
- Explicit valid transitions (prevents invalid state changes)
- Optimistic locking with version numbers (prevents race conditions)
- In-memory cache with DB persistence (fast reads, durable writes)
"""

from dataclasses import dataclass, field, replace
from typing import Dict, Any, Optional, List, FrozenSet, Tuple
from datetime import datetime
from enum import Enum
import logging

from app.database.models import ConnectionRequestStatus

logger = logging.getLogger(__name__)


class InvalidTransitionError(ValueError):
    """Raised when attempting an invalid state transition."""

    def __init__(
        self,
        current_state: "NetworkingFlowState",
        target_state: "NetworkingFlowState",
        valid_transitions: FrozenSet["NetworkingFlowState"],
    ):
        self.current_state = current_state
        self.target_state = target_state
        self.valid_transitions = valid_transitions
        super().__init__(
            f"Invalid transition: {current_state.value} -> {target_state.value}. "
            f"Valid transitions: {[s.value for s in valid_transitions]}"
        )


class NetworkingFlowState(str, Enum):
    """Valid states for networking flow transitions.

    Maps to ConnectionRequestStatus but adds flow-level states
    for tracking the full user journey.
    """

    IDLE = "idle"
    MATCHING = "matching"
    PENDING_INITIATOR_APPROVAL = "pending_initiator_approval"
    PENDING_TARGET_RESPONSE = "pending_target_response"
    READY_FOR_GROUP = "ready_for_group"
    GROUP_CREATED = "group_created"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @classmethod
    def from_connection_status(
        cls, status: ConnectionRequestStatus
    ) -> "NetworkingFlowState":
        """Convert ConnectionRequestStatus to NetworkingFlowState."""
        mapping = {
            ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL: cls.PENDING_INITIATOR_APPROVAL,
            ConnectionRequestStatus.PENDING_TARGET_APPROVAL: cls.PENDING_TARGET_RESPONSE,
            ConnectionRequestStatus.TARGET_ACCEPTED: cls.READY_FOR_GROUP,
            ConnectionRequestStatus.TARGET_DECLINED: cls.CANCELLED,
            ConnectionRequestStatus.GROUP_CREATED: cls.GROUP_CREATED,
            ConnectionRequestStatus.CANCELLED: cls.CANCELLED,
            ConnectionRequestStatus.EXPIRED: cls.CANCELLED,
        }
        return mapping.get(status, cls.IDLE)

    def to_connection_status(self) -> Optional[ConnectionRequestStatus]:
        """Convert NetworkingFlowState to ConnectionRequestStatus."""
        mapping = {
            self.PENDING_INITIATOR_APPROVAL: ConnectionRequestStatus.PENDING_INITIATOR_APPROVAL,
            self.PENDING_TARGET_RESPONSE: ConnectionRequestStatus.PENDING_TARGET_APPROVAL,
            self.READY_FOR_GROUP: ConnectionRequestStatus.TARGET_ACCEPTED,
            self.GROUP_CREATED: ConnectionRequestStatus.GROUP_CREATED,
            self.CANCELLED: ConnectionRequestStatus.CANCELLED,
        }
        return mapping.get(self)


# Valid state transitions - enforced by AtomicNetworkingState.transition_to()
VALID_TRANSITIONS: Dict[NetworkingFlowState, FrozenSet[NetworkingFlowState]] = {
    NetworkingFlowState.IDLE: frozenset(
        [
            NetworkingFlowState.MATCHING,
        ]
    ),
    NetworkingFlowState.MATCHING: frozenset(
        [
            NetworkingFlowState.PENDING_INITIATOR_APPROVAL,
            NetworkingFlowState.FAILED,
            NetworkingFlowState.IDLE,  # No matches found
        ]
    ),
    NetworkingFlowState.PENDING_INITIATOR_APPROVAL: frozenset(
        [
            NetworkingFlowState.PENDING_TARGET_RESPONSE,  # Initiator confirms
            NetworkingFlowState.CANCELLED,  # Initiator cancels
            NetworkingFlowState.MATCHING,  # Request different match
        ]
    ),
    NetworkingFlowState.PENDING_TARGET_RESPONSE: frozenset(
        [
            NetworkingFlowState.READY_FOR_GROUP,  # Target accepts
            NetworkingFlowState.CANCELLED,  # Target declines or expires
            NetworkingFlowState.FAILED,  # System error
        ]
    ),
    NetworkingFlowState.READY_FOR_GROUP: frozenset(
        [
            NetworkingFlowState.GROUP_CREATED,  # Group created successfully
            NetworkingFlowState.FAILED,  # Group creation failed
        ]
    ),
    # Terminal states - allow restart to IDLE for new networking flows
    NetworkingFlowState.GROUP_CREATED: frozenset(
        [
            NetworkingFlowState.IDLE,  # Allow starting a new networking request after completion
        ]
    ),
    NetworkingFlowState.CANCELLED: frozenset(
        [
            NetworkingFlowState.IDLE,  # Allow restart after cancellation
        ]
    ),
    NetworkingFlowState.FAILED: frozenset(
        [
            NetworkingFlowState.IDLE,  # Allow restart after failure
        ]
    ),
}


@dataclass(frozen=True)
class AtomicNetworkingState:
    """Immutable networking state that can only be updated atomically.

    Uses frozen=True to prevent accidental mutation. All updates must go
    through transition methods that return NEW instances (original unchanged).

    Example:
        state = AtomicNetworkingState()
        state = state.start_matching(user_id="...")
        state = state.with_match(request_id="...", match_details={...})

    Attributes:
        flow_state: Current state in the networking flow
        user_id: The user this state belongs to
        request_id: Active connection request ID (single match)
        request_ids: Active connection request IDs (multi-match)
        initiator_id: User who initiated the networking request
        target_ids: Target user IDs for the connection
        match_details: Details about the match(es)
        group_chat_guid: Created group chat GUID (when complete)
        connection_purpose: User's stated purpose for connecting
        updated_at: Last update timestamp
        version: Optimistic locking version number
    """

    flow_state: NetworkingFlowState = NetworkingFlowState.IDLE
    user_id: Optional[str] = None
    request_id: Optional[str] = None
    request_ids: Optional[Tuple[str, ...]] = None
    initiator_id: Optional[str] = None
    target_ids: Optional[Tuple[str, ...]] = None
    match_details: Optional[Tuple[Dict[str, Any], ...]] = None
    group_chat_guid: Optional[str] = None
    connection_purpose: Optional[str] = None
    error_message: Optional[str] = None
    updated_at: datetime = field(default_factory=datetime.utcnow)
    version: int = 0

    def transition_to(
        self, new_state: NetworkingFlowState, **updates: Any
    ) -> "AtomicNetworkingState":
        """Atomically transition to a new state.

        Args:
            new_state: Target state
            **updates: Additional field updates

        Returns:
            New state instance (old instance unchanged)

        Raises:
            InvalidTransitionError: If transition is invalid
        """
        valid_next = VALID_TRANSITIONS.get(self.flow_state, frozenset())
        if new_state not in valid_next:
            raise InvalidTransitionError(self.flow_state, new_state, valid_next)

        return replace(
            self,
            flow_state=new_state,
            updated_at=datetime.utcnow(),
            version=self.version + 1,
            **updates,
        )

    def start_matching(self, user_id: str) -> "AtomicNetworkingState":
        """Start matching process for a user."""
        return self.transition_to(
            NetworkingFlowState.MATCHING,
            user_id=user_id,
            initiator_id=user_id,
        )

    def with_match(
        self,
        request_id: str,
        target_id: str,
        match_details: Dict[str, Any],
        connection_purpose: Optional[str] = None,
    ) -> "AtomicNetworkingState":
        """Create new state with single match found."""
        return self.transition_to(
            NetworkingFlowState.PENDING_INITIATOR_APPROVAL,
            request_id=request_id,
            target_ids=(target_id,),
            match_details=(match_details,),
            connection_purpose=connection_purpose,
        )

    def with_multi_match(
        self,
        request_ids: List[str],
        target_ids: List[str],
        match_details: List[Dict[str, Any]],
        connection_purpose: Optional[str] = None,
    ) -> "AtomicNetworkingState":
        """Create new state with multiple matches found."""
        return self.transition_to(
            NetworkingFlowState.PENDING_INITIATOR_APPROVAL,
            request_ids=tuple(request_ids),
            target_ids=tuple(target_ids),
            match_details=tuple(match_details),
            connection_purpose=connection_purpose,
        )

    def initiator_confirmed(self) -> "AtomicNetworkingState":
        """Initiator has confirmed the match."""
        return self.transition_to(NetworkingFlowState.PENDING_TARGET_RESPONSE)

    def target_accepted(self) -> "AtomicNetworkingState":
        """Target has accepted the connection."""
        return self.transition_to(NetworkingFlowState.READY_FOR_GROUP)

    def group_created(self, group_chat_guid: str) -> "AtomicNetworkingState":
        """Group chat was successfully created."""
        return self.transition_to(
            NetworkingFlowState.GROUP_CREATED,
            group_chat_guid=group_chat_guid,
        )

    def cancel(self, reason: Optional[str] = None) -> "AtomicNetworkingState":
        """Cancel the current flow."""
        return self.transition_to(
            NetworkingFlowState.CANCELLED,
            error_message=reason,
        )

    def fail(self, error: str) -> "AtomicNetworkingState":
        """Mark the flow as failed."""
        return self.transition_to(
            NetworkingFlowState.FAILED,
            error_message=error,
        )

    def reset(self) -> "AtomicNetworkingState":
        """Reset to idle state (after cancel or failure)."""
        return self.transition_to(
            NetworkingFlowState.IDLE,
            request_id=None,
            request_ids=None,
            target_ids=None,
            match_details=None,
            group_chat_guid=None,
            connection_purpose=None,
            error_message=None,
        )

    def is_terminal(self) -> bool:
        """Check if state is terminal (no further transitions possible)."""
        return self.flow_state in (
            NetworkingFlowState.GROUP_CREATED,
            NetworkingFlowState.CANCELLED,
            NetworkingFlowState.FAILED,
        )

    def is_active(self) -> bool:
        """Check if there's an active networking flow."""
        return self.flow_state not in (
            NetworkingFlowState.IDLE,
            NetworkingFlowState.GROUP_CREATED,
            NetworkingFlowState.CANCELLED,
            NetworkingFlowState.FAILED,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize state to dictionary for persistence."""
        return {
            "flow_state": self.flow_state.value,
            "user_id": self.user_id,
            "request_id": self.request_id,
            "request_ids": list(self.request_ids) if self.request_ids else None,
            "initiator_id": self.initiator_id,
            "target_ids": list(self.target_ids) if self.target_ids else None,
            "match_details": list(self.match_details) if self.match_details else None,
            "group_chat_guid": self.group_chat_guid,
            "connection_purpose": self.connection_purpose,
            "error_message": self.error_message,
            "updated_at": self.updated_at.isoformat(),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AtomicNetworkingState":
        """Deserialize state from dictionary."""
        return cls(
            flow_state=NetworkingFlowState(data.get("flow_state", "idle")),
            user_id=data.get("user_id"),
            request_id=data.get("request_id"),
            request_ids=(
                tuple(data["request_ids"]) if data.get("request_ids") else None
            ),
            initiator_id=data.get("initiator_id"),
            target_ids=tuple(data["target_ids"]) if data.get("target_ids") else None,
            match_details=(
                tuple(data["match_details"]) if data.get("match_details") else None
            ),
            group_chat_guid=data.get("group_chat_guid"),
            connection_purpose=data.get("connection_purpose"),
            error_message=data.get("error_message"),
            updated_at=(
                datetime.fromisoformat(data["updated_at"])
                if data.get("updated_at")
                else datetime.utcnow()
            ),
            version=data.get("version", 0),
        )


class AtomicStateManager:
    """Manages atomic state with database persistence and optimistic locking.

    Provides:
    - In-memory cache for fast reads
    - Database persistence for durability
    - Optimistic locking to prevent race conditions

    Example:
        manager = AtomicStateManager(db)
        state = await manager.get_state(user_id)
        new_state = state.start_matching(user_id)
        success = await manager.update_state(user_id, new_state)
    """

    def __init__(self, db: Any):
        self.db = db
        self._cache: Dict[str, AtomicNetworkingState] = {}

    async def get_state(self, user_id: str) -> AtomicNetworkingState:
        """Get current atomic state for user.

        First checks in-memory cache, then falls back to database.
        Returns default IDLE state if no state exists.
        """
        if user_id in self._cache:
            return self._cache[user_id]

        stored = await self.db.get_networking_state(user_id)
        if stored:
            state = AtomicNetworkingState.from_dict(stored)
        else:
            state = AtomicNetworkingState(user_id=user_id)

        self._cache[user_id] = state
        return state

    async def update_state(
        self,
        user_id: str,
        new_state: AtomicNetworkingState,
    ) -> bool:
        """Atomically update state with optimistic locking.

        The update only succeeds if the version number matches
        (no concurrent modifications occurred).

        Note: Invalidates cache before reading to prevent stale cache reads
        that could allow invalid updates through.

        Args:
            user_id: User ID
            new_state: New state to persist

        Returns:
            True if update succeeded, False if concurrent modification
        """
        # Invalidate cache first to prevent TOCTOU race condition
        # This ensures we read fresh data from DB before validating version
        self.invalidate_cache(user_id)
        current = await self.get_state(user_id)

        if new_state.version != current.version + 1:
            logger.warning(
                f"[ATOMIC_STATE] Version mismatch for {user_id}: "
                f"expected {current.version + 1}, got {new_state.version}"
            )
            return False

        try:
            await self.db.upsert_networking_state(
                user_id=user_id,
                state=new_state.to_dict(),
                expected_version=current.version,
            )
            self._cache[user_id] = new_state
            logger.info(
                f"[ATOMIC_STATE] Updated state for {user_id}: "
                f"{current.flow_state.value} -> {new_state.flow_state.value}"
            )
            return True

        except Exception as e:
            logger.error(f"[ATOMIC_STATE] Failed to update state for {user_id}: {e}")
            # Invalidate cache on failure to ensure fresh read next time
            self.invalidate_cache(user_id)
            return False

    def invalidate_cache(self, user_id: str) -> None:
        """Remove user from cache (forces reload from DB on next access)."""
        self._cache.pop(user_id, None)

    def clear_cache(self) -> None:
        """Clear entire cache."""
        self._cache.clear()

    async def force_reset_state(self, user_id: str) -> AtomicNetworkingState:
        """Force reset state to IDLE, bypassing version checks.

        This is intended for test cleanup and recovery scenarios where
        we need to reset state regardless of the current version.

        Args:
            user_id: User ID to reset state for

        Returns:
            The new IDLE state
        """
        # Clear cache first
        self.invalidate_cache(user_id)

        # Create fresh IDLE state with version 0
        fresh_state = AtomicNetworkingState(user_id=user_id, version=0)

        try:
            # Delete existing state if any
            self.db.client.table("user_networking_states").delete().eq(
                "user_id", user_id
            ).execute()

            logger.info(f"[ATOMIC_STATE] Force reset state for {user_id} to IDLE")
            self._cache[user_id] = fresh_state
            return fresh_state

        except Exception as e:
            logger.error(f"[ATOMIC_STATE] Failed to force reset state for {user_id}: {e}")
            # Return fresh state anyway - next update will create it
            return fresh_state
