"""State tracking for the Interaction Agent's loop.

This module provides dataclasses for tracking task execution states
across iterations of the interaction loop.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from app.agents.execution.state import ExecutionResult

TaskStatus = Literal["pending", "running", "complete", "failed", "waiting"]


@dataclass
class TaskExecutionState:
    """Track a single task's execution state across iterations."""

    task_name: str
    status: TaskStatus = "pending"
    result: Optional[ExecutionResult] = None
    waiting_for: Optional[str] = None  # What we're waiting for (e.g., "match_confirmation")

    def is_terminal(self) -> bool:
        """Check if task is in a terminal state (no more work needed this iteration)."""
        return self.status in ("complete", "failed", "waiting")

    def needs_more_work(self) -> bool:
        """Check if task needs continued execution."""
        return self.status in ("pending", "running")


@dataclass
class IterationContext:
    """Aggregated state for a single iteration of the interaction loop."""

    iteration: int
    task_states: Dict[str, TaskExecutionState] = field(default_factory=dict)

    @property
    def all_complete(self) -> bool:
        """Check if all tasks completed successfully or are waiting for user."""
        if not self.task_states:
            return False
        return all(ts.status in ("complete", "waiting") for ts in self.task_states.values())

    @property
    def any_failed(self) -> bool:
        """Check if any task failed."""
        return any(ts.status == "failed" for ts in self.task_states.values())

    @property
    def any_waiting(self) -> bool:
        """Check if any task is waiting for user input."""
        return any(ts.status == "waiting" for ts in self.task_states.values())

    @property
    def completed_tasks(self) -> List[str]:
        """Get list of completed task names."""
        return [name for name, ts in self.task_states.items() if ts.status == "complete"]

    @property
    def waiting_tasks(self) -> List[TaskExecutionState]:
        """Get list of tasks waiting for user input."""
        return [ts for ts in self.task_states.values() if ts.status == "waiting"]

    @property
    def failed_tasks(self) -> List[TaskExecutionState]:
        """Get list of failed tasks."""
        return [ts for ts in self.task_states.values() if ts.status == "failed"]

    @property
    def incomplete_tasks(self) -> List[str]:
        """Get list of task names that need more work."""
        return [name for name, ts in self.task_states.items() if ts.needs_more_work()]
