"""Execution state definitions for the generic execution agent."""

import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from app.agents.memory.execution import ExecutionMemory


ExecutionStatus = Literal["running", "complete", "failed", "waiting"]


@dataclass
class ExecutionAction:
    """An action to take in the execution loop.

    For type="tool":
        - tool_name: Name of the tool to execute
        - params: Parameters for the tool

    For type="complete":
        - summary: What was accomplished (internal note)
        - data: Structured data collected (NOT user-facing text)

    For type="wait_for_user":
        - waiting_for: What we're waiting for (e.g., "match_confirmation")
        - data: Context data about what's pending
    """

    type: Literal["tool", "complete", "wait_for_user"]
    tool_name: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)

    # For complete/wait_for_user action - structured data only
    summary: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    waiting_for: Optional[str] = None  # For wait_for_user type

    # DEPRECATED - keeping for backward compatibility during migration
    result: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


@dataclass
class ExecutionResult:
    """Result from executing a task.

    The execution agent returns STRUCTURED DATA only, never user-facing text.
    The Interaction Agent is responsible for synthesizing user-facing responses.

    Attributes:
        status: Current status (complete, failed)
        actions_taken: List of tools called and their results
        data_collected: Information extracted during execution
        state_changes: Database modifications made
        error: Error message (if failed)
        memory: Execution memory (for debugging/logging)
        iterations_used: Number of ReAct loop iterations
    """

    status: ExecutionStatus

    # Structured output (NOT user-facing)
    actions_taken: List[Dict[str, Any]] = field(default_factory=list)
    data_collected: Dict[str, Any] = field(default_factory=dict)
    state_changes: Dict[str, Any] = field(default_factory=dict)

    # Error handling
    error: Optional[str] = None

    # Memory for debugging/logging
    memory: Optional[ExecutionMemory] = None
    iterations_used: int = 0

    # DEPRECATED fields - keeping for backward compatibility
    result: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    waiting_for: Optional[str] = None  # Kept for backward compatibility
    waiting_context: Optional[str] = None  # Kept for backward compatibility

    def __post_init__(self):
        """Handle backward compatibility for deprecated fields."""
        # If old-style result with response_text is passed, issue deprecation warning
        if self.result and "response_text" in self.result:
            warnings.warn(
                "ExecutionResult.result['response_text'] is deprecated. "
                "Response synthesis now happens in InteractionAgent.",
                DeprecationWarning,
                stacklevel=2,
            )

    def summarize_actions(self) -> str:
        """Generate human-readable summary of actions taken.

        Returns:
            Formatted string summarizing what tools were called and their outcomes
        """
        if not self.actions_taken:
            return "No actions taken."

        summaries = []
        for action in self.actions_taken:
            tool_name = action.get("tool_name", "unknown")
            success = action.get("success", False)
            status_str = "succeeded" if success else "failed"

            # Include brief result info if available
            result_info = action.get("result_summary", "")
            if result_info:
                summaries.append(f"- {tool_name}: {status_str} ({result_info})")
            else:
                summaries.append(f"- {tool_name}: {status_str}")

        return "\n".join(summaries)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "status": self.status,
            "actions_taken": self.actions_taken,
            "data_collected": self.data_collected,
            "state_changes": self.state_changes,
            "error": self.error,
            "iterations_used": self.iterations_used,
            # Include deprecated fields for backward compatibility
            "result": self.result,
            "message": self.message,
        }
