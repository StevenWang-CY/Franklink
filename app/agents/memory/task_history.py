"""Task history persistence for context building.

Stores completed task states to provide context for future interactions.
This is NOT for task resumption - just for understanding conversation context.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TaskStateRecord:
    """A compact record of a completed task execution."""

    task_name: str
    instruction: str  # What the user wanted
    outcome: str  # Brief outcome summary
    status: str  # "complete" or "failed"
    key_data: Dict[str, Any]  # Important results
    created_at: Optional[str] = None

    def to_context_string(self) -> str:
        """Format as concise context string for LLM prompts."""
        data_str = ""
        if self.key_data:
            # Only include non-empty, relevant data
            relevant = {k: v for k, v in self.key_data.items() if v}
            if relevant:
                data_str = f" | Data: {json.dumps(relevant)}"

        return f"[{self.task_name}] {self.instruction} -> {self.outcome}{data_str}"


class TaskHistorySaver:
    """Saves and loads task execution history for context.

    Designed for efficiency:
    - Stores only essential information
    - Retrieves only recent tasks
    - Formats compactly for LLM context
    """

    def __init__(self, db):
        """Initialize the task history saver.

        Args:
            db: DatabaseClient instance
        """
        self.db = db

    async def save_task(
        self,
        user_id: str,
        task_name: str,
        instruction: str,
        outcome: str,
        status: str,
        key_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Save a completed task state.

        Args:
            user_id: User's UUID
            task_name: Name of the task (e.g., "networking", "update")
            instruction: The refined instruction that was executed
            outcome: Brief summary of what happened
            status: "complete" or "failed"
            key_data: Optional dict of important results
        """
        try:
            data = {
                "user_id": user_id,
                "task_name": task_name,
                "instruction": instruction[:500] if instruction else "",  # Limit size
                "outcome": outcome[:500] if outcome else "",  # Limit size
                "status": status,
                "key_data": key_data or {},
            }

            self.db.client.table("task_state").insert(data).execute()
            logger.debug(f"[TASK_HISTORY] Saved {task_name} task for user {user_id}")

        except Exception as e:
            # Don't fail the main flow if history saving fails
            logger.warning(f"[TASK_HISTORY] Failed to save task state: {e}")

    async def get_recent_tasks(
        self,
        user_id: str,
        limit: int = 3,
    ) -> List[TaskStateRecord]:
        """Get recent task states for context.

        Args:
            user_id: User's UUID
            limit: Maximum number of tasks to return

        Returns:
            List of TaskStateRecord, most recent first
        """
        try:
            result = (
                self.db.client.table("task_state")
                .select("task_name, instruction, outcome, status, key_data, created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )

            records = []
            for row in result.data or []:
                records.append(TaskStateRecord(
                    task_name=row.get("task_name", ""),
                    instruction=row.get("instruction", ""),
                    outcome=row.get("outcome", ""),
                    status=row.get("status", ""),
                    key_data=row.get("key_data", {}),
                    created_at=row.get("created_at"),
                ))

            logger.debug(f"[TASK_HISTORY] Loaded {len(records)} recent tasks for user {user_id}")
            return records

        except Exception as e:
            logger.warning(f"[TASK_HISTORY] Failed to load task history: {e}")
            return []

    def format_for_context(
        self,
        records: List[TaskStateRecord],
        max_chars: int = 500,
    ) -> str:
        """Format task records as concise context string.

        Args:
            records: List of task records
            max_chars: Maximum total characters

        Returns:
            Formatted context string
        """
        if not records:
            return ""

        lines = []
        total_chars = 0

        for record in records:
            line = record.to_context_string()
            if total_chars + len(line) > max_chars:
                break
            lines.append(line)
            total_chars += len(line) + 1  # +1 for newline

        return "\n".join(lines)
