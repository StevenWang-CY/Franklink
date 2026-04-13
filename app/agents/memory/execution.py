"""Execution memory for the generic execution agent.

Manages the ReAct loop state:
- Scratchpad: Full log of Thought/Action/Observation cycles
- Context: Task-specific data passed in at start
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)


@dataclass
class ScratchpadEntry:
    """A single entry in the execution scratchpad.

    Each entry represents one step in the ReAct loop:
    - "thought": The agent's reasoning
    - "action": The tool call or special action
    - "observation": The result of the action
    """

    type: Literal["thought", "action", "observation"]
    content: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionMemory:
    """Memory for the generic execution agent.

    Attributes:
        scratchpad: Full log of ReAct loop steps
        context: Initial context passed to the task
        iteration: Current iteration number
        task_name: Name of the current task
        interim_results: Results gathered during execution
    """

    scratchpad: List[ScratchpadEntry] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    iteration: int = 0
    task_name: str = ""
    interim_results: Dict[str, Any] = field(default_factory=dict)

    def add_thought(self, thought: str, metadata: Optional[Dict] = None):
        """Record a thought step."""
        entry = ScratchpadEntry(
            type="thought", content=thought, metadata=metadata or {}
        )
        self.scratchpad.append(entry)
        logger.debug(f"[{self.task_name}] Thought: {thought[:100]}...")

    def add_action(
        self,
        action_type: str,
        tool_name: Optional[str] = None,
        params: Optional[Dict] = None,
        metadata: Optional[Dict] = None,
    ):
        """Record an action step."""
        content = f"{action_type}"
        if tool_name:
            content = f"{action_type}: {tool_name}"
            if params:
                content += f" with {params}"

        entry = ScratchpadEntry(
            type="action",
            content=content,
            metadata={
                "action_type": action_type,
                "tool_name": tool_name,
                "params": params,
                **(metadata or {}),
            },
        )
        self.scratchpad.append(entry)
        logger.debug(f"[{self.task_name}] Action: {content[:100]}...")

    def add_observation(self, observation: str, metadata: Optional[Dict] = None):
        """Record an observation (tool result)."""
        entry = ScratchpadEntry(
            type="observation", content=observation, metadata=metadata or {}
        )
        self.scratchpad.append(entry)
        logger.debug(f"[{self.task_name}] Observation: {observation[:100]}...")

    def get_last_thought(self) -> Optional[str]:
        """Get the most recent thought."""
        for entry in reversed(self.scratchpad):
            if entry.type == "thought":
                return entry.content
        return None

    def get_last_observation(self) -> Optional[str]:
        """Get the most recent observation."""
        for entry in reversed(self.scratchpad):
            if entry.type == "observation":
                return entry.content
        return None

    def get_scratchpad_text(self, max_entries: int = 20) -> str:
        """Get scratchpad as formatted text for LLM context.

        Args:
            max_entries: Maximum number of entries to include

        Returns:
            Formatted scratchpad text
        """
        entries = self.scratchpad[-max_entries:]
        lines = []
        for entry in entries:
            prefix = entry.type.capitalize()
            lines.append(f"{prefix}: {entry.content}")
        return "\n".join(lines)

    def get_thoughts_only(self) -> List[str]:
        """Get all thoughts from the scratchpad."""
        return [e.content for e in self.scratchpad if e.type == "thought"]

    def get_actions_only(self) -> List[Dict[str, Any]]:
        """Get all actions with their metadata."""
        return [
            e.metadata for e in self.scratchpad if e.type == "action" and e.metadata
        ]

    def increment_iteration(self):
        """Increment the iteration counter."""
        self.iteration += 1

    def store_result(self, key: str, value: Any):
        """Store an interim result."""
        self.interim_results[key] = value

    def get_result(self, key: str, default: Any = None) -> Any:
        """Get a stored interim result."""
        return self.interim_results.get(key, default)
