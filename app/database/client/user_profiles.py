"""Database client methods for user_profiles table."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _UserProfileMethods:
    """Mixin for user profile database operations."""

    async def get_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Get holistic profile for a user.

        Args:
            user_id: User UUID

        Returns:
            Profile dict or None if not found
        """
        try:
            result = (
                self.client.table("user_profiles")
                .select("*")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )

            if result.data:
                return result.data[0]
            return None

        except Exception as e:
            logger.error(f"Error getting user profile: {e}", exc_info=True)
            return None

    async def upsert_user_profile(
        self,
        user_id: str,
        profile_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Create or update a user's holistic profile.

        Args:
            user_id: User UUID
            profile_data: Profile fields to upsert

        Returns:
            Updated profile dict or None on error
        """
        try:
            ALLOWED_FIELDS = {
                "personality_summary",
                "communication_style",
                "work_patterns",
                "latent_needs",
                "unspoken_gaps",
                "ideal_relationship_types",
                "relationship_strengths",
                "relationship_risks",
                "trajectory_summary",
                "core_motivations",
                "career_stage",
                "holistic_summary",
                "holistic_embedding",
                "computed_at",
                "zep_facts_count",
                "confidence_score",
            }

            sanitized = {
                k: v for k, v in profile_data.items()
                if k in ALLOWED_FIELDS
            }
            sanitized["user_id"] = user_id
            sanitized["updated_at"] = datetime.utcnow().isoformat()

            result = (
                self.client.table("user_profiles")
                .upsert(sanitized, on_conflict="user_id")
                .execute()
            )

            if result.data:
                logger.info(f"Upserted profile for user {user_id[:8]}...")
                return result.data[0]

            return None

        except Exception as e:
            logger.error(f"Error upserting user profile: {e}", exc_info=True)
            return None

    async def get_users_needing_profile_synthesis(
        self,
        stale_days: int = 7,
        batch_limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Get users that need profile synthesis.

        Returns users with:
        - No profile exists
        - Profile older than stale_days

        Args:
            stale_days: Consider profiles stale after this many days
            batch_limit: Max users to return

        Returns:
            List of {user_id, reason} dicts
        """
        try:
            result = self.client.rpc(
                "get_users_needing_profile_synthesis",
                {"stale_days": stale_days, "batch_limit": batch_limit},
            ).execute()

            return result.data if result.data else []

        except Exception as e:
            logger.error(f"Error getting users needing profile synthesis: {e}", exc_info=True)
            return []

    async def match_users_by_profile(
        self,
        query_embedding: List[float],
        exclude_user_id: str,
        exclude_user_ids: Optional[List[str]] = None,
        match_threshold: float = 0.35,
        match_count: int = 15,
    ) -> List[Dict[str, Any]]:
        """
        Find users with similar holistic profiles.

        Args:
            query_embedding: 1536-dimension embedding vector
            exclude_user_id: User to exclude from results
            exclude_user_ids: Additional users to exclude
            match_threshold: Minimum similarity score (0-1)
            match_count: Maximum results to return

        Returns:
            List of matched user dicts with similarity scores
        """
        try:
            result = self.client.rpc(
                "match_users_by_profile",
                {
                    "query_embedding": query_embedding,
                    "exclude_user_id": exclude_user_id,
                    "match_threshold": match_threshold,
                    "match_count": match_count,
                },
            ).execute()

            matches = result.data if result.data else []

            if exclude_user_ids:
                exclude_set = set(exclude_user_ids)
                matches = [m for m in matches if m.get("id") not in exclude_set]

            return matches

        except Exception as e:
            logger.error(f"Error matching users by profile: {e}", exc_info=True)
            return []

    async def delete_user_profile(self, user_id: str) -> bool:
        """
        Delete a user's holistic profile.

        Args:
            user_id: User UUID

        Returns:
            True if deleted, False otherwise
        """
        try:
            result = (
                self.client.table("user_profiles")
                .delete()
                .eq("user_id", user_id)
                .execute()
            )

            if result.data:
                logger.info(f"Deleted profile for user {user_id[:8]}...")
                return True
            return False

        except Exception as e:
            logger.error(f"Error deleting user profile: {e}", exc_info=True)
            return False

    async def get_profile_stats(self) -> Dict[str, Any]:
        """
        Get statistics about user profiles.

        Returns:
            Dict with profile statistics
        """
        try:
            total_result = (
                self.client.table("user_profiles")
                .select("id", count="exact")
                .execute()
            )
            total = total_result.count or 0

            high_confidence_result = (
                self.client.table("user_profiles")
                .select("id", count="exact")
                .gte("confidence_score", 0.7)
                .execute()
            )
            high_confidence = high_confidence_result.count or 0

            return {
                "total_profiles": total,
                "high_confidence_profiles": high_confidence,
                "coverage_ratio": high_confidence / total if total > 0 else 0,
            }

        except Exception as e:
            logger.error(f"Error getting profile stats: {e}", exc_info=True)
            return {"total_profiles": 0, "high_confidence_profiles": 0, "coverage_ratio": 0}
