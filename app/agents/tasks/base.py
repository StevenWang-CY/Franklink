"""Base task infrastructure for the agent system.

Tasks define goals that agents work toward, including:
- System prompt with instructions
- Available tools
- Completion criteria
- Configuration (max iterations, etc.)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from app.agents.tools.base import Tool, get_tool_from_func

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """Status of a task execution."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"  # Waiting for user input
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass
class Task:
    """A task that defines a goal for the execution agent.

    Attributes:
        name: Unique identifier for the task type
        system_prompt: Instructions for the LLM on how to complete the task
        tools: List of tools available for this task
        completion_criteria: Description of when the task is complete
        max_iterations: Maximum number of ReAct loops before failure
        allow_early_exit: Whether the agent can complete before exhausting iterations
        requires_user_input: Whether this task typically needs user interaction
    """

    name: str
    system_prompt: str
    tools: List[Tool] = field(default_factory=list)
    completion_criteria: str = ""
    max_iterations: int = 10
    allow_early_exit: bool = True
    requires_user_input: bool = False

    def __post_init__(self):
        """Convert decorated functions to Tool objects if needed."""
        converted_tools = []
        for tool_item in self.tools:
            if isinstance(tool_item, Tool):
                converted_tools.append(tool_item)
            elif callable(tool_item) and hasattr(tool_item, "_tool_meta"):
                converted_tools.append(tool_item._tool_meta)
            else:
                logger.warning(f"Invalid tool in task {self.name}: {tool_item}")
        self.tools = converted_tools

    def get_tool(self, name: str) -> Optional[Tool]:
        """Get a tool by name from this task's available tools."""
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def get_tool_names(self) -> List[str]:
        """Get list of available tool names."""
        return [tool.name for tool in self.tools]

    def to_llm_tools_schema(self) -> List[Dict[str, Any]]:
        """Convert tools to LLM function-calling schema."""
        return [tool.to_llm_schema() for tool in self.tools]

    def build_system_prompt(self, context: Optional[Dict[str, Any]] = None) -> str:
        """Build the full system prompt with tool information.

        Args:
            context: Optional context to inject into the prompt
                - task_instruction: Dict with "case" and "instruction" from InteractionAgent
                - user_profile: User profile data
                - Other task-specific context

        Returns:
            Complete system prompt for the LLM
        """
        import json as _json

        tool_descriptions = "\n".join(
            f"- {tool.name}: {tool.description}" for tool in self.tools
        )

        # Extract task_instruction prominently if present
        task_instruction_section = ""
        if context and "task_instruction" in context:
            task_instruction = context.get("task_instruction", {})
            if task_instruction:
                task_instruction_section = f"""
## YOUR TASK INSTRUCTION (from InteractionAgent)
```json
{_json.dumps(task_instruction, indent=2)}
```
IMPORTANT: Follow the case and instruction above. Use task_instruction["instruction"] as input for tools that need the user's request.
"""

        # Build remaining context (excluding task_instruction which is shown separately)
        context_section = ""
        if context:
            other_context = {k: v for k, v in context.items() if k != "task_instruction"}
            if other_context:
                # Format user_profile more cleanly if present
                if "user_profile" in other_context:
                    profile = other_context["user_profile"]
                    profile_items = []
                    for key in ["user_id", "name", "phone_number", "university", "latest_demand", "all_value"]:
                        if profile.get(key):
                            profile_items.append(f"  - {key}: {profile[key]}")
                    if profile_items:
                        context_section += f"\n## User Profile\n" + "\n".join(profile_items)
                    other_context = {k: v for k, v in other_context.items() if k != "user_profile"}

                # Add any remaining context
                if other_context:
                    remaining = "\n".join(f"- {k}: {v}" for k, v in other_context.items())
                    context_section += f"\n## Additional Context\n{remaining}"

        prompt = f"""{self.system_prompt}
{task_instruction_section}
## Available Tools
{tool_descriptions}

## Completion Criteria
{self.completion_criteria}

## Instructions
1. Think step by step about what action to take next
2. Choose one tool to call with appropriate parameters
3. Wait for the observation before deciding the next action
4. When the task is complete, use the "complete" action
5. If you need user input, use the "wait_for_user" action

Always respond with a JSON object containing:
- "thought": Your reasoning about what to do next
- "action": Either a tool call or special action
  - Tool call: {{"type": "tool", "name": "<tool_name>", "params": {{...}}}}
  - Complete: {{"type": "complete", "result": {{"response_text": "<message to send to user>"}}}}
  - Wait for user: {{"type": "wait_for_user", "message": "<message>", "waiting_for": "<state>"}}
{context_section}"""

        return prompt


def create_task(
    name: str,
    system_prompt: str,
    tools: List[Callable],
    completion_criteria: str = "",
    max_iterations: int = 10,
) -> Task:
    """Factory function to create a Task from decorated functions.

    Args:
        name: Task identifier
        system_prompt: Instructions for the task
        tools: List of @tool decorated functions
        completion_criteria: When the task is done
        max_iterations: Max ReAct loops

    Returns:
        Configured Task object
    """
    tool_objects = []
    for func in tools:
        tool_obj = get_tool_from_func(func)
        if tool_obj:
            tool_objects.append(tool_obj)
        else:
            logger.warning(f"Function {func.__name__} is not a decorated tool")

    return Task(
        name=name,
        system_prompt=system_prompt,
        tools=tool_objects,
        completion_criteria=completion_criteria,
        max_iterations=max_iterations,
    )
