"""Base Agent interface for all agents (Interaction and Execution)."""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """
    Base class for all agents in the multi-agent system.

    This provides a common interface for both:
    - InteractionAgent (conductor)
    - ExecutionAgents (domain workers: onboarding, networking, etc.)
    """

    def __init__(self, agent_type: str, db: Any, openai: Any):
        """
        Initialize the base agent.

        Args:
            agent_type: Type of agent (e.g., "interaction", "onboarding", "networking")
            db: DatabaseClient instance
            openai: AzureOpenAIClient instance
        """
        self.agent_type = agent_type
        self.db = db
        self.openai = openai

        logger.info(f"[{self.agent_type.upper()}] Agent initialized")

    @abstractmethod
    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the agent's logic.

        This is the main entry point for all agents. For Execution Agents,
        this runs their task logic. For the Interaction Agent, this orchestrates
        multiple Execution Agents.

        Args:
            state: The current state dictionary

        Returns:
            Updated state dictionary after agent execution
        """
        pass

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} type={self.agent_type}>"
