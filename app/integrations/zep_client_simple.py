"""Simplified Zep Memory Client using direct HTTP API."""

import asyncio
import httpx
import logging
import random
from typing import List, Dict, Any, Optional
from datetime import datetime
import json

from app.config import settings

logger = logging.getLogger(__name__)


class ZepMemoryClient:
    """
    Simplified client for Zep Cloud API using direct HTTP requests.

    This implementation uses the Zep REST API directly for better control
    and compatibility with Zep Cloud.
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        """
        Initialize Zep client.

        Args:
            api_key: Zep API key (uses settings if not provided)
            base_url: Zep base URL (uses settings if not provided)
        """
        self.api_key = api_key or settings.zep_api_key
        self.base_url = base_url or settings.zep_base_url
        self.client = None

        if not self.api_key:
            logger.warning("Zep API key not configured. Memory features will be limited.")
            return

        # Initialize HTTP client with auth headers
        # Zep uses API key directly in header
        self.headers = {
            "Authorization": f"Api-Key {self.api_key}",
            "Content-Type": "application/json"
        }

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=30.0
        )

        logger.info(f"Zep client initialized with base URL: {self.base_url}")

    def is_available(self) -> bool:
        """Helper to mirror interface used elsewhere."""
        return self.client is not None

    @staticmethod
    def _should_retry_status(status_code: int) -> bool:
        return status_code in {408, 409, 425, 429, 500, 502, 503, 504}

    @staticmethod
    async def _sleep_backoff(attempt: int, *, base: float = 0.25, cap: float = 2.5) -> None:
        # Exponential backoff with jitter.
        delay = min(cap, base * (2 ** max(0, attempt - 1)))
        jitter = random.uniform(0.0, delay * 0.25)
        await asyncio.sleep(delay + jitter)

    async def create_or_get_thread(self, user_id: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Create or retrieve a Zep thread for a user.

        Args:
            user_id: Unique user identifier
            metadata: Optional metadata

        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            return False

        payload = {
            "user_id": user_id,
            "session_id": user_id,  # Use same ID for both (API still uses session_id in payload)
            "metadata": metadata or {},
        }

        last_status = None
        last_text = ""
        for attempt in range(1, 6):
            try:
                response = await self.client.post("/api/v2/sessions", json=payload)
                last_status = response.status_code
                last_text = response.text or ""

                if response.status_code in [200, 201, 409]:  # 409 means session already exists
                    logger.info(f"Thread ready for user: {user_id}")
                    return True

                if response.status_code == 400:
                    # Some Zep Cloud deployments return 400 for "already exists".
                    try:
                        data = response.json()
                        msg = str(data.get("message") or "")
                        if "already exists" in msg.lower() and "session" in msg.lower():
                            logger.info(f"Thread already exists for user: {user_id}")
                            return True
                    except Exception:
                        pass

                if self._should_retry_status(response.status_code) and attempt < 5:
                    await self._sleep_backoff(attempt)
                    continue

                logger.error(f"Failed to create thread: {response.status_code} - {response.text}")
                return False
            except Exception as e:
                if attempt < 5:
                    await self._sleep_backoff(attempt)
                    continue
                logger.error(f"Error creating/getting Zep thread: {str(e)}")
                return False

        logger.error(f"Failed to create thread after retries: {last_status} - {last_text[:200]}")
        return False

    async def add_message(
        self,
        thread_id: str,
        content: str,
        role: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Add a message to a Zep thread.

        Args:
            thread_id: Thread identifier
            content: Message content
            role: "user" or "assistant"
            metadata: Optional metadata

        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            return False

        payload = {
            "messages": [
                {
                    "content": content,
                    "role": role,
                    "metadata": metadata or {},
                }
            ]
        }

        last_status = None
        last_text = ""
        for attempt in range(1, 6):
            try:
                # Zep Cloud uses /memory endpoint for adding messages (API path still uses sessions)
                response = await self.client.post(f"/api/v2/sessions/{thread_id}/memory", json=payload)
                last_status = response.status_code
                last_text = response.text or ""

                if response.status_code in [200, 201]:
                    logger.debug(f"Added message to Zep thread {thread_id}")
                    return True

                if self._should_retry_status(response.status_code) and attempt < 5:
                    await self._sleep_backoff(attempt)
                    continue

                logger.error(f"Failed to add message: {response.status_code} - {response.text}")
                return False
            except Exception as e:
                if attempt < 5:
                    await self._sleep_backoff(attempt)
                    continue
                logger.error(f"Error adding message to Zep thread: {str(e)}")
                return False

        logger.error(f"Failed to add message after retries: {last_status} - {last_text[:200]}")
        return False

    async def get_messages(self, thread_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Get raw message history for a thread (more complete than /memory).

        Args:
            thread_id: Thread identifier
            limit: Maximum messages to return (server may cap)

        Returns:
            List of message dicts (newest-first or oldest-first depending on API)
        """
        if not self.client:
            return []

        limit = max(1, min(int(limit or 50), 200))

        try:
            response = await self.client.get(
                f"/api/v2/sessions/{thread_id}/messages",  # API path still uses sessions
                params={"limit": limit},
            )

            if response.status_code == 200:
                data = response.json()
                messages = data.get("messages", []) if isinstance(data, dict) else []
                return messages if isinstance(messages, list) else []
            if response.status_code == 404:
                return []

            logger.error(f"Failed to get messages: {response.status_code} - {response.text[:200]}")
            return []
        except Exception as e:
            logger.error(f"Error getting messages from Zep thread: {str(e)}")
            return []

    async def get_memory(self, thread_id: str, limit: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Get memory for a thread.

        Args:
            thread_id: Thread identifier
            limit: Optional message limit

        Returns:
            Memory data or None
        """
        if not self.client:
            return None

        try:
            params = {}
            if limit:
                params["limit"] = limit

            response = await self.client.get(
                f"/api/v2/sessions/{thread_id}/memory",  # API path still uses sessions
                params=params
            )

            if response.status_code == 200:
                data = response.json()
                # Ensure we return a dictionary with expected structure
                if data is None:
                    return {"messages": [], "facts": [], "summary": None}
                return data
            elif response.status_code == 404:
                # Thread exists but no memory yet
                logger.debug(f"No memory found for thread {thread_id} (404)")
                return {"messages": [], "facts": [], "summary": None}
            else:
                logger.error(f"Failed to get memory: {response.status_code} - {response.text[:200]}")
            return None

        except Exception as e:
            logger.error(f"Error getting memory from Zep thread: {str(e)}")
            return None

    async def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """
        Get thread details (including metadata).

        Args:
            thread_id: Thread identifier

        Returns:
            Thread dict or None
        """
        if not self.client:
            return None

        try:
            response = await self.client.get(f"/api/v2/sessions/{thread_id}")  # API path still uses sessions

            if response.status_code == 200:
                return response.json()
            if response.status_code == 404:
                return None

            logger.error(f"Failed to get thread: {response.status_code} - {response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"Error getting thread from Zep: {str(e)}")
            return None

    async def delete_thread(self, thread_id: str) -> bool:
        """
        Delete a Zep thread and its memory.
        """
        if not self.client:
            return False
        try:
            response = await self.client.delete(f"/api/v2/sessions/{thread_id}")  # API path still uses sessions
            if response.status_code in [200, 204, 404]:
                logger.info(f"Deleted Zep thread {thread_id} (status {response.status_code})")
                return True
            logger.error(f"Failed to delete thread {thread_id}: {response.status_code} - {response.text}")
            return False
        except Exception as e:
            logger.error(f"Error deleting Zep thread {thread_id}: {e}")
            return False

    async def search_memory(
        self,
        thread_id: str,
        query: str,
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Search memory for relevant context.

        Args:
            thread_id: Thread identifier
            query: Search query
            limit: Result limit

        Returns:
            List of search results
        """
        if not self.client:
            return []

        try:
            response = await self.client.post(
                f"/api/v2/sessions/{thread_id}/search",  # API path still uses sessions
                json={
                    "query": query,
                    "limit": limit
                }
            )

            if response.status_code == 200:
                data = response.json()
                return data.get("results", [])
            elif response.status_code == 410:
                # 410 Gone - This endpoint may be deprecated or not available in the API plan
                logger.warning(f"Memory search endpoint returned 410 (Gone) - feature may not be available")
                return []
            elif response.status_code == 404:
                # Thread doesn't exist yet or has no searchable memory
                logger.debug(f"Thread {thread_id} not found for search (404)")
                return []
            else:
                logger.error(f"Failed to search memory: {response.status_code} - {response.text[:200]}")
                return []

        except Exception as e:
            logger.error(f"Error searching Zep thread memory: {str(e)}")
            return []

    async def get_facts(self, thread_id: str) -> List[Dict[str, Any]]:
        """
        Get extracted facts for a thread.

        Args:
            thread_id: Thread identifier

        Returns:
            List of facts
        """
        if not self.client:
            return []

        try:
            response = await self.client.get(f"/api/v2/sessions/{thread_id}/facts")  # API path still uses sessions

            if response.status_code == 200:
                data = response.json()
                return data.get("facts", [])
            else:
                return []

        except Exception as e:
            logger.error(f"Error getting facts: {str(e)}")
            return []

    # NOTE: add_user_fact() method removed - Zep Cloud API deprecated /facts endpoint
    # Zep now recommends using graph.add_fact_triple instead (requires graph feature)
    #
    # Memory strategy without explicit facts:
    # 1. Thread metadata stores structured profile (name, university, major, etc)
    # 2. Zep automatically extracts facts from conversation messages
    # 3. Supabase profile serves as fallback in _format_zep_context()
    #
    # This three-tier approach ensures user information is always available

    async def update_thread_metadata(
        self,
        thread_id: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """
        Update metadata for an existing Zep thread.

        Args:
            thread_id: Thread identifier
            metadata: New metadata to merge with existing

        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            return False

        payload = {"metadata": metadata}

        last_status = None
        last_text = ""
        for attempt in range(1, 7):
            try:
                response = await self.client.patch(f"/api/v2/sessions/{thread_id}", json=payload)  # API path still uses sessions
                last_status = response.status_code
                last_text = response.text or ""

                if response.status_code in [200, 204]:
                    logger.info(f"Updated Zep thread metadata for {thread_id}")
                    return True

                if response.status_code == 400:
                    # Some deployments use 400 with an informative message for write contention.
                    try:
                        data = response.json()
                        msg = str(data.get("message") or "")
                        if "too many concurrent writes" in msg.lower() and attempt < 6:
                            await self._sleep_backoff(attempt)
                            continue
                    except Exception:
                        pass

                if self._should_retry_status(response.status_code) and attempt < 6:
                    await self._sleep_backoff(attempt)
                    continue

                logger.warning(f"Failed to update thread metadata: {response.status_code} - {response.text[:200]}")
                return False
            except Exception as e:
                if attempt < 6:
                    await self._sleep_backoff(attempt)
                    continue
                logger.error(f"Error updating Zep thread metadata: {str(e)}")
                return False

        logger.warning(f"Failed to update thread metadata after retries: {last_status} - {last_text[:200]}")
        return False

    async def get_thread_summary(self, thread_id: str) -> Optional[str]:
        """
        Get conversation summary for a thread.

        Extracts summary from the memory response.

        Args:
            thread_id: Thread identifier

        Returns:
            Summary text or None if not available
        """
        if not self.client:
            return None

        try:
            # Get memory which includes summary
            memory = await self.get_memory(thread_id=thread_id)

            if not memory:
                return None

            # Extract summary from memory response
            summary = memory.get('summary')
            if summary:
                # Summary can be a dict with 'content' or a string
                if isinstance(summary, dict):
                    content = summary.get('content', '')
                    # Clean up empty or placeholder summaries
                    if content and content.strip() and len(content.strip()) > 10:
                        return content.strip()
                elif isinstance(summary, str) and len(summary.strip()) > 10:
                    return summary.strip()

            return None

        except Exception as e:
            logger.error(f"Error getting thread summary: {str(e)}")
            return None

    def is_available(self) -> bool:
        """Check if Zep client is available."""
        return self.client is not None

    async def close(self):
        """Close the HTTP client."""
        if self.client:
            await self.client.aclose()
