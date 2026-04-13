"""Base tool infrastructure for the agent system.

Tools are atomic, reusable functions that agents can invoke during execution.
Each tool has a name, description, parameters schema, and an async function.
"""

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union, get_type_hints

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """Result returned from a tool invocation."""

    success: bool
    data: Any = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_observation(self, max_length: int = 2000) -> str:
        """Convert result to observation string for the ReAct loop.

        Args:
            max_length: Maximum characters for the observation string.
                       Default 2000 chars (~500 tokens) to keep context manageable.
        """
        if self.success:
            if isinstance(self.data, str):
                result = self.data
            elif isinstance(self.data, dict):
                result = str(self.data)
            elif self.data is None:
                return "Action completed successfully."
            else:
                result = str(self.data)

            # Truncate if too long to prevent LLM context overflow
            if len(result) > max_length:
                return result[:max_length] + f"... [truncated, {len(result)} chars total]"
            return result
        else:
            return f"Error: {self.error}"


@dataclass
class Tool:
    """A tool that can be invoked by an agent.

    Attributes:
        name: Unique identifier for the tool
        description: Human-readable description for LLM context
        func: The async function to execute
        parameters: JSON Schema describing the function parameters
    """

    name: str
    description: str
    func: Callable
    parameters: Dict[str, Any] = field(default_factory=dict)

    def to_llm_schema(self) -> Dict[str, Any]:
        """Convert tool to LLM function-calling schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _extract_params_from_signature(func: Callable) -> Dict[str, Any]:
    """Extract JSON Schema parameters from a function signature.

    Args:
        func: The function to extract parameters from

    Returns:
        JSON Schema object describing the parameters
    """
    sig = inspect.signature(func)
    hints = get_type_hints(func) if hasattr(func, "__annotations__") else {}

    properties = {}
    required = []

    # Skip 'self' and common injected dependencies
    skip_params = {"self", "db", "openai", "state", "context"}

    for param_name, param in sig.parameters.items():
        if param_name in skip_params:
            continue

        # Get type hint
        type_hint = hints.get(param_name, Any)
        json_type = _python_type_to_json_type(type_hint)

        # Build property schema
        prop_schema: Dict[str, Any] = {"type": json_type}

        # Check for Optional
        if hasattr(type_hint, "__origin__"):
            if type_hint.__origin__ is Union:
                # Handle Optional[X] which is Union[X, None]
                args = [a for a in type_hint.__args__ if a is not type(None)]
                if args:
                    prop_schema["type"] = _python_type_to_json_type(args[0])

        properties[param_name] = prop_schema

        # Required if no default value
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _python_type_to_json_type(py_type: Any) -> str:
    """Convert Python type annotation to JSON Schema type."""
    type_mapping = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        List: "array",
        dict: "object",
        Dict: "object",
    }

    # Handle generic types
    origin = getattr(py_type, "__origin__", None)
    if origin is not None:
        return type_mapping.get(origin, "string")

    return type_mapping.get(py_type, "string")


def tool(name: str, description: str):
    """Decorator to register a function as a Tool.

    Usage:
        @tool("find_match", "Find a networking match for the user")
        async def find_match(user_id: str, demand: str) -> ToolResult:
            ...

    Args:
        name: Unique tool identifier
        description: Description for the LLM

    Returns:
        Decorated function with _tool_meta attribute
    """

    def decorator(func: Callable) -> Callable:
        parameters = _extract_params_from_signature(func)
        func._tool_meta = Tool(
            name=name,
            description=description,
            func=func,
            parameters=parameters,
        )
        return func

    return decorator


def get_tool_from_func(func: Callable) -> Optional[Tool]:
    """Extract Tool metadata from a decorated function."""
    return getattr(func, "_tool_meta", None)


class ToolRegistry:
    """Registry for managing available tools."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool_or_func: Union[Tool, Callable]) -> None:
        """Register a tool or decorated function."""
        if isinstance(tool_or_func, Tool):
            self._tools[tool_or_func.name] = tool_or_func
        elif hasattr(tool_or_func, "_tool_meta"):
            tool_obj = tool_or_func._tool_meta
            self._tools[tool_obj.name] = tool_obj
        else:
            raise ValueError(
                f"Cannot register {tool_or_func}: not a Tool or decorated function"
            )

    def get(self, name: str) -> Optional[Tool]:
        """Get a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        """List all registered tools."""
        return list(self._tools.values())

    def to_llm_schema(self) -> List[Dict[str, Any]]:
        """Convert all tools to LLM function-calling schema."""
        return [tool.to_llm_schema() for tool in self._tools.values()]


# Global registry for all tools
global_registry = ToolRegistry()
