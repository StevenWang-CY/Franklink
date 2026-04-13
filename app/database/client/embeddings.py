"""Internal database client implementation.

This package splits the Supabase DatabaseClient into focused mixins.
"""

import logging
import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from uuid import UUID

from postgrest.exceptions import APIError

from .retry import with_retry

logger = logging.getLogger(__name__)


class _EmbeddingMethods:
    async def update_career_interest_embedding(
        self,
        user_id: str,
        embedding: List[float]
    ) -> Dict[str, Any]:
        """
        Update a user's career interest embedding.

        Args:
            user_id: User ID
            embedding: 1536-dimension embedding vector

        Returns:
            Updated user data
        """
        try:
            result = self.client.table("users").update({
                "career_interest_embedding": embedding,
                "updated_at": datetime.utcnow().isoformat()
            }).eq("id", user_id).execute()

            if result.data:
                logger.info(f"Updated career interest embedding for user {user_id}")
                return result.data[0]

            raise ValueError(f"User {user_id} not found")

        except Exception as e:
            logger.error(f"Error updating career interest embedding: {e}", exc_info=True)
            raise

    async def update_demand_embedding(
        self,
        user_id: str,
        embedding: List[float]
    ) -> Dict[str, Any]:
        """
        Update a user's demand embedding.

        Args:
            user_id: User ID
            embedding: 1536-dimension embedding vector

        Returns:
            Updated user data
        """
        try:
            result = self.client.table("users").update({
                "demand_embedding": embedding,
                "updated_at": datetime.utcnow().isoformat()
            }).eq("id", user_id).execute()

            if result.data:
                logger.info(f"Updated demand embedding for user {user_id}")
                return result.data[0]

            raise ValueError(f"User {user_id} not found")

        except Exception as e:
            logger.error(f"Error updating demand embedding: {e}", exc_info=True)
            raise

    async def update_latest_demand_embedding(
        self,
        user_id: str,
        embedding: List[float]
    ) -> Dict[str, Any]:
        """
        Update a user's latest-demand embedding.

        Args:
            user_id: User ID
            embedding: 1536-dimension embedding vector

        Returns:
            Updated user data
        """
        try:
            result = self.client.table("users").update({
                "latest_demand_embedding": embedding,
                "updated_at": datetime.utcnow().isoformat()
            }).eq("id", user_id).execute()

            if result.data:
                logger.info(f"Updated latest demand embedding for user {user_id}")
                return result.data[0]

            raise ValueError(f"User {user_id} not found")

        except Exception as e:
            logger.error(f"Error updating latest demand embedding: {e}", exc_info=True)
            raise

    async def update_value_embedding(
        self,
        user_id: str,
        embedding: Optional[List[float]]
    ) -> Dict[str, Any]:
        """
        Update or clear a user's value embedding.

        Args:
            user_id: User ID
            embedding: 1536-dimension embedding vector (or None to clear)

        Returns:
            Updated user data
        """
        try:
            result = self.client.table("users").update({
                "value_embedding": embedding,
                "updated_at": datetime.utcnow().isoformat()
            }).eq("id", user_id).execute()

            if result.data:
                action = "Cleared" if embedding is None else "Updated"
                logger.info(f"{action} value embedding for user {user_id}")
                return result.data[0]

            raise ValueError(f"User {user_id} not found")

        except Exception as e:
            logger.error(f"Error updating value embedding: {e}", exc_info=True)
            raise

    async def update_context_embedding(
        self,
        user_id: str,
        embedding: Optional[List[float]],
        context_summary: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Update or clear a user's context embedding (synthesized background data).

        Args:
            user_id: User ID
            embedding: 1536-dimension embedding vector (or None to clear)
            context_summary: The text used to generate the embedding (stored for debugging/display)

        Returns:
            Updated user data
        """
        try:
            update_data = {
                "context_embedding": embedding,
                "context_summary": context_summary,
                "updated_at": datetime.utcnow().isoformat()
            }
            result = self.client.table("users").update(update_data).eq("id", user_id).execute()

            if result.data:
                action = "Cleared" if embedding is None else "Updated"
                logger.info(f"{action} context embedding for user {user_id}")
                return result.data[0]

            raise ValueError(f"User {user_id} not found")

        except Exception as e:
            logger.error(f"Error updating context embedding: {e}", exc_info=True)
            raise

    async def match_users_by_career_interest(
        self,
        query_embedding: List[float],
        university_filter: str,
        exclude_user_id: str,
        exclude_user_ids: Optional[List[str]] = None,
        match_threshold: float = 0.4,
        match_count: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Find users with similar career interests using semantic search.

        Args:
            query_embedding: The initiator's career interest embedding
            university_filter: University to filter by (exact match)
            exclude_user_id: User ID to exclude (the initiator)
            exclude_user_ids: Additional user IDs to exclude
            match_threshold: Minimum similarity score (0-1)
            match_count: Maximum number of matches to return

        Returns:
            List of matching users with similarity scores
        """
        try:
            # Call the RPC function
            result = self.client.rpc(
                "match_users_by_career_interest",
                {
                    "query_embedding": query_embedding,
                    "university_filter": university_filter,
                    "exclude_user_id": exclude_user_id,
                    "match_threshold": match_threshold,
                    "match_count": match_count
                }
            ).execute()

            matches = result.data or []

            # Filter out additional excluded users
            if exclude_user_ids:
                exclude_set = set(exclude_user_ids)
                matches = [m for m in matches if str(m.get("id")) not in exclude_set]

            logger.info(
                f"Found {len(matches)} career interest matches for university "
                f"{university_filter} (excluding {exclude_user_id})"
            )
            return matches

        except Exception as e:
            logger.error(f"Error matching users by career interest: {e}", exc_info=True)
            return []

    async def match_users_by_value(
        self,
        query_embedding: List[float],
        exclude_user_id: str,
        exclude_user_ids: Optional[List[str]] = None,
        match_threshold: float = 0.4,
        match_count: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Find users whose value matches the given demand embedding.

        Args:
            query_embedding: Demand embedding to match against user value embeddings
            exclude_user_id: User ID to exclude (the initiator)
            exclude_user_ids: Additional user IDs to exclude
            match_threshold: Minimum similarity score (0-1)
            match_count: Maximum number of matches to return

        Returns:
            List of matching users with similarity scores
        """
        try:
            result = self.client.rpc(
                "match_users_by_value",
                {
                    "query_embedding": query_embedding,
                    "exclude_user_id": exclude_user_id,
                    "match_threshold": match_threshold,
                    "match_count": match_count
                }
            ).execute()

            matches = result.data or []

            if exclude_user_ids:
                exclude_set = set(exclude_user_ids)
                matches = [m for m in matches if str(m.get("id")) not in exclude_set]

            logger.info(
                f"Found {len(matches)} value matches (excluding {exclude_user_id})"
            )
            return matches

        except Exception as e:
            logger.error(f"Error matching users by value: {e}", exc_info=True)
            return []

    async def match_users_by_demand(
        self,
        query_embedding: List[float],
        exclude_user_id: str,
        exclude_user_ids: Optional[List[str]] = None,
        match_threshold: float = 0.4,
        match_count: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Find users whose demand matches the given value embedding.

        Args:
            query_embedding: Value embedding to match against user demand embeddings
            exclude_user_id: User ID to exclude (the initiator)
            exclude_user_ids: Additional user IDs to exclude
            match_threshold: Minimum similarity score (0-1)
            match_count: Maximum number of matches to return

        Returns:
            List of matching users with similarity scores
        """
        try:
            result = self.client.rpc(
                "match_users_by_demand",
                {
                    "query_embedding": query_embedding,
                    "exclude_user_id": exclude_user_id,
                    "match_threshold": match_threshold,
                    "match_count": match_count
                }
            ).execute()

            matches = result.data or []

            if exclude_user_ids:
                exclude_set = set(exclude_user_ids)
                matches = [m for m in matches if str(m.get("id")) not in exclude_set]

            logger.info(
                f"Found {len(matches)} demand matches (excluding {exclude_user_id})"
            )
            return matches

        except Exception as e:
            logger.error(f"Error matching users by demand: {e}", exc_info=True)
            return []

    def get_users_without_embeddings(self) -> List[Dict[str, Any]]:
        """
        Get all onboarded users who don't have career_interest_embedding set.

        Returns:
            List of users needing embedding backfill
        """
        try:
            result = self.client.table("users").select(
                "id, name, university, career_interests"
            ).eq(
                "is_onboarded", True
            ).is_(
                "career_interest_embedding", "null"
            ).not_.is_(
                "career_interests", "null"
            ).execute()

            users = result.data or []
            # Filter to only users with non-empty career_interests
            users = [u for u in users if u.get("career_interests") and len(u["career_interests"]) > 0]
            logger.info(f"Found {len(users)} users without career interest embeddings")
            return users

        except Exception as e:
            logger.error(f"Error getting users without embeddings: {e}", exc_info=True)
            return []

    def get_users_without_context_embeddings(self) -> List[Dict[str, Any]]:
        """
        Get all onboarded users who don't have context_embedding set.

        Returns:
            List of users needing context embedding backfill
        """
        try:
            result = self.client.table("users").select(
                "id, name, university, major, location, year, career_interests"
            ).eq(
                "is_onboarded", True
            ).is_(
                "context_embedding", "null"
            ).execute()

            users = result.data or []
            # Filter to only users with some context data
            users = [
                u for u in users
                if (u.get("university") or u.get("major") or
                    u.get("location") or u.get("year") or
                    (u.get("career_interests") and len(u["career_interests"]) > 0))
            ]
            logger.info(f"Found {len(users)} users without context embeddings")
            return users

        except Exception as e:
            logger.error(f"Error getting users without context embeddings: {e}", exc_info=True)
            return []

    async def match_users_comprehensive(
        self,
        query_embedding: List[float],
        embedding_type: str,
        exclude_user_id: str,
        exclude_user_ids: Optional[List[str]] = None,
        match_threshold: float = 0.35,
        match_count: int = 15
    ) -> List[Dict[str, Any]]:
        """
        Find users using comprehensive matching with rich candidate data.

        Supports matching by value, demand, context, or career_interest embeddings.
        Returns comprehensive user data for LLM-based match selection.

        Args:
            query_embedding: Query embedding vector (1536 dimensions)
            embedding_type: Type of embedding to match against
                ('value', 'demand', 'context', or 'career_interest')
            exclude_user_id: User ID to exclude (the initiator)
            exclude_user_ids: Additional user IDs to exclude
            match_threshold: Minimum similarity score (0-1)
            match_count: Maximum number of matches to return

        Returns:
            List of matching users with comprehensive profile data and similarity scores
        """
        if embedding_type not in ('value', 'demand', 'context', 'career_interest'):
            logger.error(f"Invalid embedding_type: {embedding_type}")
            return []

        try:
            result = self.client.rpc(
                "match_users_comprehensive",
                {
                    "query_embedding": query_embedding,
                    "embedding_type": embedding_type,
                    "exclude_user_id": exclude_user_id,
                    "match_threshold": match_threshold,
                    "match_count": match_count
                }
            ).execute()

            matches = result.data or []

            # Filter out additional excluded users
            if exclude_user_ids:
                exclude_set = set(exclude_user_ids)
                matches = [m for m in matches if str(m.get("id")) not in exclude_set]

            logger.info(
                f"Found {len(matches)} comprehensive {embedding_type} matches "
                f"(excluding {exclude_user_id})"
            )
            return matches

        except Exception as e:
            logger.error(
                f"Error in match_users_comprehensive ({embedding_type}): {e}",
                exc_info=True
            )
            return []
