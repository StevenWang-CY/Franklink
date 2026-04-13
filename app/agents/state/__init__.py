"""Atomic state management for complex multi-step flows.

Provides immutable state updates, valid transition enforcement,
and optimistic locking for networking and other flows.
"""

from .atomic_state import (
    NetworkingFlowState,
    VALID_TRANSITIONS,
    AtomicNetworkingState,
    AtomicStateManager,
    InvalidTransitionError,
)

__all__ = [
    "NetworkingFlowState",
    "VALID_TRANSITIONS",
    "AtomicNetworkingState",
    "AtomicStateManager",
    "InvalidTransitionError",
]
