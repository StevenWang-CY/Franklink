"""Interaction memory for the conductor agent.

Manages conversation context:
- Short-term: Recent messages in current conversation window
- Long-term: User summary, preferences, history from Zep threads
- Active tasks: Currently running task roster
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Message:
    """A message in conversation history."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InteractionMemory:
    """Memory for the interaction agent (conductor).

    Attributes:
        short_term: Recent N messages from current session (sliding window)
        long_term: User summary and facts from Zep
        active_tasks: List of task names currently being executed
        user_profile_summary: Condensed view of user profile for context
        session_facts: Facts extracted during this session
    """

    short_term: List[Message] = field(default_factory=list)
    long_term: Dict[str, Any] = field(default_factory=dict)
    active_tasks: List[str] = field(default_factory=list)
    user_profile_summary: Optional[str] = None
    session_facts: List[Dict[str, Any]] = field(default_factory=list)

    # Configuration - increased from 10 to 50 based on poke-backend pattern
    # Larger window provides better context for complex multi-turn conversations
    max_short_term_messages: int = 50

    def add_message(self, role: str, content: str, metadata: Optional[Dict] = None):
        """Add a message to short-term memory with auto-pruning.

        Uses sliding window pattern from poke-backend to maintain bounded memory.
        When messages exceed max_short_term_messages, oldest messages are dropped.
        """
        msg = Message(role=role, content=content, metadata=metadata or {})
        self.short_term.append(msg)

        # Auto-prune: maintain sliding window (pattern from poke-backend)
        if len(self.short_term) > self.max_short_term_messages:
            self.short_term = self.short_term[-self.max_short_term_messages :]

    def add_user_message(self, content: str, metadata: Optional[Dict] = None):
        """Convenience method to add a user message."""
        self.add_message("user", content, metadata)

    def add_assistant_message(self, content: str, metadata: Optional[Dict] = None):
        """Convenience method to add an assistant message."""
        self.add_message("assistant", content, metadata)

    def get_recent_messages(self, n: int = 5) -> List[Message]:
        """Get the N most recent messages."""
        return self.short_term[-n:]

    def get_conversation_text(self, n: int = 5) -> str:
        """Get recent conversation as formatted text."""
        messages = self.get_recent_messages(n)
        return "\n".join(f"{m.role}: {m.content}" for m in messages)

    def add_task(self, task_name: str):
        """Add a task to the active roster."""
        if task_name not in self.active_tasks:
            self.active_tasks.append(task_name)

    def remove_task(self, task_name: str):
        """Remove a task from the active roster."""
        if task_name in self.active_tasks:
            self.active_tasks.remove(task_name)

    def has_active_task(self) -> bool:
        """Check if there are any active tasks."""
        return len(self.active_tasks) > 0

    def add_fact(self, fact: str, confidence: float = 1.0, source: str = "extraction"):
        """Add a fact extracted during the session."""
        self.session_facts.append(
            {
                "fact": fact,
                "confidence": confidence,
                "source": source,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )

    def update_from_zep(self, zep_memory: Dict[str, Any]):
        """Update long-term memory from Zep response.

        Args:
            zep_memory: Response from Zep containing facts, summary, etc.
        """
        self.long_term = zep_memory
        if "summary" in zep_memory:
            self.user_profile_summary = zep_memory["summary"]

    def to_context_dict(self) -> Dict[str, Any]:
        """Convert memory to context dictionary for LLM."""
        return {
            "recent_conversation": self.get_conversation_text(),
            "user_summary": self.user_profile_summary or "No summary available",
            "active_tasks": self.active_tasks,
            "session_facts": [f["fact"] for f in self.session_facts],
            "long_term_facts": self.long_term.get("facts", []),
        }

    def clear_session(self):
        """Clear session-specific data (keep long-term)."""
        self.short_term = []
        self.active_tasks = []
        self.session_facts = []
