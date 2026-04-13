"""Interaction module - Conductor agent that orchestrates tasks."""

from app.agents.interaction.agent import InteractionAgentNew

# Alias for backwards compatibility
InteractionAgent = InteractionAgentNew

__all__ = ["InteractionAgent", "InteractionAgentNew"]


def get_interaction_agent(db, photon, openai):
    """Factory function to get the interaction agent.

    Args:
        db: DatabaseClient instance
        photon: PhotonClient instance
        openai: AzureOpenAIClient instance

    Returns:
        InteractionAgentNew instance
    """
    return InteractionAgentNew(db=db, photon=photon, openai=openai)
