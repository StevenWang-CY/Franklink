"""Zep Graph Client for knowledge graph operations.

Extends ZepMemoryClient with graph.add, graph.search, and user context capabilities.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config import settings
from app.integrations.zep_client_simple import ZepMemoryClient

logger = logging.getLogger(__name__)


@dataclass
class GraphSearchResult:
    """Result from a graph search operation."""

    fact: str
    score: float
    source_node: Optional[str] = None
    target_node: Optional[str] = None
    created_at: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class GraphAddResult:
    """Result from adding data to the graph."""

    success: bool
    episode_id: Optional[str] = None
    error: Optional[str] = None


class ZepGraphClient(ZepMemoryClient):
    """
    Extended Zep client with knowledge graph capabilities.

    Adds graph.add, graph.search, and user context retrieval
    on top of the base ZepMemoryClient.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        """Initialize Zep graph client."""
        super().__init__(api_key=api_key, base_url=base_url)

    def is_graph_enabled(self) -> bool:
        """Check if graph features are enabled and client is available."""
        return (
            self.is_available()
            and getattr(settings, "zep_graph_enabled", True)
        )

    async def ensure_user_exists(self, user_id: str) -> bool:
        """
        Ensure a user exists in Zep before adding to their graph.

        Args:
            user_id: Unique user identifier

        Returns:
            True if user exists or was created, False on error
        """
        if not self.client:
            return False

        payload = {
            "user_id": user_id,
        }

        last_status = None
        last_text = ""
        for attempt in range(1, 4):
            try:
                response = await self.client.post("/api/v2/users", json=payload)
                last_status = response.status_code
                last_text = response.text or ""

                if response.status_code in [200, 201, 409]:
                    logger.debug(f"User ready in Zep: {user_id[:8]}...")
                    return True

                if response.status_code == 400:
                    try:
                        data = response.json()
                        msg = str(data.get("message") or "")
                        if "already exists" in msg.lower():
                            return True
                    except Exception:
                        pass

                if self._should_retry_status(response.status_code) and attempt < 3:
                    await self._sleep_backoff(attempt)
                    continue

                logger.warning(
                    f"Failed to ensure user exists: {response.status_code} - {response.text[:200]}"
                )
                return False

            except Exception as e:
                if attempt < 3:
                    await self._sleep_backoff(attempt)
                    continue
                logger.error(f"Error ensuring user exists in Zep: {str(e)}")
                return False

        logger.warning(f"Failed to ensure user after retries: {last_status} - {last_text[:200]}")
        return False

    async def add_to_graph(
        self,
        user_id: str,
        data: str,
        data_type: str = "text",
    ) -> GraphAddResult:
        """
        Add data to a user's knowledge graph.

        Args:
            user_id: User identifier
            data: The data to add (text, JSON string, or message)
            data_type: One of "text", "json", or "message"

        Returns:
            GraphAddResult with success status and episode_id if successful
        """
        if not self.client:
            return GraphAddResult(success=False, error="Zep client not available")

        if not self.is_graph_enabled():
            return GraphAddResult(success=False, error="Zep graph not enabled")

        if data_type not in ("text", "json", "message"):
            return GraphAddResult(success=False, error=f"Invalid data_type: {data_type}")

        if len(data) > 10000:
            return GraphAddResult(
                success=False,
                error=f"Data exceeds 10,000 character limit ({len(data)} chars)"
            )

        await self.ensure_user_exists(user_id)

        payload = {
            "user_id": user_id,
            "type": data_type,
            "data": data,
        }

        last_status = None
        last_text = ""
        for attempt in range(1, 4):
            try:
                response = await self.client.post("/api/v2/graph", json=payload)
                last_status = response.status_code
                last_text = response.text or ""

                if response.status_code in [200, 201, 202]:
                    result_data = response.json() if response.text else {}
                    episode_id = result_data.get("episode_id") or result_data.get("uuid")
                    logger.info(
                        f"Added data to Zep graph user={user_id[:8]}... "
                        f"type={data_type} chars={len(data)}"
                    )
                    return GraphAddResult(success=True, episode_id=episode_id)

                if self._should_retry_status(response.status_code) and attempt < 3:
                    await self._sleep_backoff(attempt)
                    continue

                logger.warning(
                    f"Failed to add to graph: {response.status_code} - {response.text[:200]}"
                )
                return GraphAddResult(
                    success=False,
                    error=f"API error: {response.status_code}"
                )

            except Exception as e:
                if attempt < 3:
                    await self._sleep_backoff(attempt)
                    continue
                logger.error(f"Error adding to Zep graph: {str(e)}")
                return GraphAddResult(success=False, error=str(e))

        return GraphAddResult(
            success=False,
            error=f"Failed after retries: {last_status} - {last_text[:100]}"
        )

    async def search_graph(
        self,
        user_id: str,
        query: str,
        scope: str = "edges",
        limit: int = 10,
        min_score: Optional[float] = None,
    ) -> List[GraphSearchResult]:
        """
        Search a user's knowledge graph for relevant facts.

        Args:
            user_id: User identifier
            query: Search query (semantic search)
            scope: "edges" for facts, "nodes" for entities
            limit: Maximum results to return
            min_score: Optional minimum relevance score filter

        Returns:
            List of GraphSearchResult objects
        """
        if not self.client:
            return []

        if not self.is_graph_enabled():
            return []

        if scope not in ("edges", "nodes"):
            scope = "edges"

        payload = {
            "user_id": user_id,
            "query": query,
            "scope": scope,
            "limit": min(limit, 50),
        }

        try:
            response = await self.client.post("/api/v2/graph/search", json=payload)

            if response.status_code == 200:
                data = response.json()
                edges = data.get("edges", []) if scope == "edges" else []
                nodes = data.get("nodes", []) if scope == "nodes" else []

                results = []
                for edge in edges:
                    score = edge.get("score") or edge.get("relevance_score") or 0.0
                    if min_score and score < min_score:
                        continue
                    results.append(GraphSearchResult(
                        fact=edge.get("fact", ""),
                        score=score,
                        source_node=edge.get("source_node_name"),
                        target_node=edge.get("target_node_name"),
                        created_at=edge.get("created_at"),
                        valid_from=edge.get("valid_at"),
                        valid_to=edge.get("invalid_at"),
                        metadata=edge.get("metadata"),
                    ))

                for node in nodes:
                    score = node.get("score") or node.get("relevance_score") or 0.0
                    if min_score and score < min_score:
                        continue
                    results.append(GraphSearchResult(
                        fact=node.get("name", ""),
                        score=score,
                        metadata=node.get("metadata"),
                    ))

                logger.debug(
                    f"Graph search user={user_id[:8]}... query='{query[:30]}...' "
                    f"results={len(results)}"
                )
                return results

            if response.status_code == 404:
                logger.debug(f"No graph found for user {user_id[:8]}...")
                return []

            logger.warning(
                f"Graph search failed: {response.status_code} - {response.text[:200]}"
            )
            return []

        except Exception as e:
            logger.error(f"Error searching Zep graph: {str(e)}")
            return []

    async def get_user_context(
        self,
        user_id: str,
        thread_id: Optional[str] = None,
        min_rating: Optional[float] = None,
    ) -> Optional[str]:
        """
        Get holistic user context from their knowledge graph.

        Returns a context string containing user summary and relevant facts,
        suitable for including in LLM prompts.

        Args:
            user_id: User identifier
            thread_id: Optional thread ID for context-aware retrieval
            min_rating: Optional minimum fact rating filter

        Returns:
            Context string or None if unavailable
        """
        if not self.client:
            return None

        if not self.is_graph_enabled():
            return None

        try:
            params = {}
            if min_rating is not None:
                params["min_rating"] = min_rating

            if thread_id:
                response = await self.client.get(
                    f"/api/v2/threads/{thread_id}/context",
                    params=params,
                )
            else:
                response = await self.client.get(
                    f"/api/v2/users/{user_id}/context",
                    params=params,
                )

            if response.status_code == 200:
                data = response.json()
                context = data.get("context")
                if context and isinstance(context, str) and len(context.strip()) > 10:
                    logger.debug(f"Got user context for {user_id[:8]}... ({len(context)} chars)")
                    return context.strip()
                return None

            if response.status_code == 404:
                logger.debug(f"No context found for user {user_id[:8]}...")
                return None

            logger.warning(
                f"Failed to get user context: {response.status_code} - {response.text[:200]}"
            )
            return None

        except Exception as e:
            logger.error(f"Error getting user context from Zep: {str(e)}")
            return None

    async def add_fact_triple(
        self,
        user_id: str,
        fact: str,
        fact_name: str,
        source_node_name: str,
        target_node_name: str,
    ) -> GraphAddResult:
        """
        Add a specific fact triple to the user's graph.

        This creates an explicit relationship between two nodes.

        Args:
            user_id: User identifier
            fact: The fact statement (e.g., "Paul met Eric")
            fact_name: Relationship type (e.g., "MET", "WORKS_AT")
            source_node_name: Source entity name
            target_node_name: Target entity name

        Returns:
            GraphAddResult with success status
        """
        if not self.client:
            return GraphAddResult(success=False, error="Zep client not available")

        if not self.is_graph_enabled():
            return GraphAddResult(success=False, error="Zep graph not enabled")

        await self.ensure_user_exists(user_id)

        payload = {
            "user_id": user_id,
            "fact": fact,
            "fact_name": fact_name,
            "source_node_name": source_node_name,
            "target_node_name": target_node_name,
        }

        try:
            response = await self.client.post("/api/v2/graph/facts", json=payload)

            if response.status_code in [200, 201, 202]:
                result_data = response.json() if response.text else {}
                task_id = result_data.get("task_id")
                logger.info(
                    f"Added fact triple to Zep graph user={user_id[:8]}... "
                    f"fact='{fact[:50]}...'"
                )
                return GraphAddResult(success=True, episode_id=task_id)

            logger.warning(
                f"Failed to add fact triple: {response.status_code} - {response.text[:200]}"
            )
            return GraphAddResult(
                success=False,
                error=f"API error: {response.status_code}"
            )

        except Exception as e:
            logger.error(f"Error adding fact triple to Zep graph: {str(e)}")
            return GraphAddResult(success=False, error=str(e))

    async def get_user_facts(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get all facts for a user from their graph.

        Args:
            user_id: User identifier
            limit: Maximum facts to return

        Returns:
            List of fact dictionaries
        """
        if not self.client:
            return []

        if not self.is_graph_enabled():
            return []

        try:
            response = await self.client.get(
                f"/api/v2/users/{user_id}/facts",
                params={"limit": min(limit, 100)},
            )

            if response.status_code == 200:
                data = response.json()
                facts = data.get("facts", [])
                logger.debug(f"Got {len(facts)} facts for user {user_id[:8]}...")
                return facts if isinstance(facts, list) else []

            if response.status_code == 404:
                return []

            logger.warning(
                f"Failed to get user facts: {response.status_code} - {response.text[:200]}"
            )
            return []

        except Exception as e:
            logger.error(f"Error getting user facts from Zep: {str(e)}")
            return []


_graph_client: Optional[ZepGraphClient] = None


def get_zep_graph_client() -> ZepGraphClient:
    """Get or create the singleton Zep graph client."""
    global _graph_client
    if _graph_client is None:
        _graph_client = ZepGraphClient()
    return _graph_client
