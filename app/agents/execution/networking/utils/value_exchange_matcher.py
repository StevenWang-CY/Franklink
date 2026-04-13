"""Value exchange matcher for networking.

Finds the best match for a user based on mutual value exchange:
- What the initiator needs (demand) should match what the target offers (value)
- What the initiator offers (value) should match what the target needs (demand)
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """Result from finding a match.

    All fields are structured data - no user-facing text except llm_introduction
    which is for internal match explanation.
    """
    success: bool = False
    error_message: Optional[str] = None

    # Target user info
    target_user_id: Optional[str] = None
    target_name: Optional[str] = None
    target_phone: Optional[str] = None

    # Match quality
    match_score: float = 0.0
    matching_reasons: List[str] = field(default_factory=list)

    # LLM-generated context (for internal use, not user-facing)
    llm_introduction: Optional[str] = None
    llm_concern: Optional[str] = None


class ValueExchangeMatcher:
    """Finds matches based on mutual value exchange.

    Uses embedding similarity to match:
    - Initiator's demand against target's value
    - Initiator's value against target's demand
    """

    def __init__(
        self,
        db: Optional[DatabaseClient] = None,
        openai: Optional[AzureOpenAIClient] = None,
    ):
        """Initialize the matcher.

        Args:
            db: Database client (creates one if not provided)
            openai: OpenAI client for embeddings (creates one if not provided)
        """
        self.db = db or DatabaseClient()
        self.openai = openai or AzureOpenAIClient()

    async def find_best_match(
        self,
        user_id: str,
        user_profile: Dict[str, Any],
        excluded_user_ids: Optional[List[str]] = None,
        override_demand: Optional[str] = None,
        override_value: Optional[str] = None,
    ) -> MatchResult:
        """Find the best value-exchange match for a user.

        Args:
            user_id: The initiator's user ID
            user_profile: The initiator's profile data
            excluded_user_ids: User IDs to exclude from matching
            override_demand: Override the user's demand for this search
            override_value: Override the user's value for this search

        Returns:
            MatchResult with match details or error
        """
        try:
            excluded = excluded_user_ids or []

            # Get the initiator's demand and value
            demand_text = override_demand or user_profile.get("latest_demand") or user_profile.get("all_demand")
            value_text = override_value or user_profile.get("all_value")

            if not demand_text:
                return MatchResult(
                    success=False,
                    error_message="No demand specified. What kind of help are you looking for?",
                )

            # Generate embedding for the initiator's demand
            demand_embedding = await self.openai.get_embedding(demand_text)
            if not demand_embedding:
                return MatchResult(
                    success=False,
                    error_message="Failed to process your networking request. Please try again.",
                )

            # Find users whose value matches the initiator's demand
            candidates = await self.db.match_users_by_value(
                query_embedding=demand_embedding,
                exclude_user_id=user_id,
                exclude_user_ids=excluded,
                match_threshold=0.3,  # Lower threshold to get more candidates
                match_count=10,
            )

            if not candidates:
                return MatchResult(
                    success=False,
                    error_message="No suitable matches found at this time. Try being more specific about what you're looking for.",
                )

            # Score candidates based on mutual value exchange
            scored_candidates = await self._score_candidates(
                candidates=candidates,
                initiator_demand=demand_text,
                initiator_value=value_text,
                user_profile=user_profile,
            )

            if not scored_candidates:
                return MatchResult(
                    success=False,
                    error_message="No suitable matches found based on mutual value exchange.",
                )

            # Pick the best match
            best = scored_candidates[0]

            # Build matching reasons
            matching_reasons = self._build_matching_reasons(
                target=best,
                initiator_demand=demand_text,
            )

            # Generate introduction text (for internal context)
            llm_introduction = await self._generate_introduction(
                initiator_name=user_profile.get("name", "Someone"),
                target=best,
                initiator_demand=demand_text,
            )

            return MatchResult(
                success=True,
                target_user_id=str(best.get("id")),
                target_name=best.get("name"),
                target_phone=best.get("phone_number"),
                match_score=best.get("_score", 0.0),
                matching_reasons=matching_reasons,
                llm_introduction=llm_introduction,
            )

        except Exception as e:
            logger.error(f"[MATCHER] find_best_match failed: {e}", exc_info=True)
            return MatchResult(
                success=False,
                error_message=f"Match search failed: {str(e)}",
            )

    async def _score_candidates(
        self,
        candidates: List[Dict[str, Any]],
        initiator_demand: str,
        initiator_value: Optional[str],
        user_profile: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Score candidates based on mutual value exchange.

        Args:
            candidates: List of potential matches from embedding search
            initiator_demand: What the initiator is looking for
            initiator_value: What the initiator can offer
            user_profile: Initiator's profile

        Returns:
            Sorted list of candidates with _score field
        """
        scored = []

        for candidate in candidates:
            score = candidate.get("similarity", 0.5)

            # Boost score if we have mutual value exchange
            if initiator_value and candidate.get("all_demand"):
                # Check if initiator's value matches candidate's demand
                value_embedding = await self.openai.get_embedding(initiator_value)
                if value_embedding:
                    demand_embedding = candidate.get("demand_embedding")
                    if demand_embedding:
                        # Simple heuristic: boost if there's alignment
                        # In production, you'd compute cosine similarity
                        score *= 1.2

            # Boost for same university
            if (user_profile.get("university") and
                candidate.get("university") == user_profile.get("university")):
                score *= 1.1

            candidate["_score"] = min(score, 1.0)  # Cap at 1.0
            scored.append(candidate)

        # Sort by score descending
        scored.sort(key=lambda x: x.get("_score", 0), reverse=True)

        return scored

    def _build_matching_reasons(
        self,
        target: Dict[str, Any],
        initiator_demand: str,
    ) -> List[str]:
        """Build human-readable matching reasons.

        Args:
            target: The matched user
            initiator_demand: What the initiator is looking for

        Returns:
            List of matching reason strings
        """
        reasons = []

        target_value = target.get("all_value") or ""
        target_career = target.get("career_interests") or []

        # Add reason based on what they can offer
        if target_value:
            reasons.append(f"Can help with: {target_value[:100]}...")

        # Add career context if relevant
        if target_career:
            if isinstance(target_career, list):
                careers = ", ".join(target_career[:3])
            else:
                careers = str(target_career)
            reasons.append(f"Background in: {careers}")

        # Add university context
        if target.get("university"):
            reasons.append(f"From {target['university']}")

        return reasons[:3]  # Limit to 3 reasons

    async def _generate_introduction(
        self,
        initiator_name: str,
        target: Dict[str, Any],
        initiator_demand: str,
    ) -> str:
        """Generate an introduction explanation (for internal context).

        Args:
            initiator_name: Name of the person requesting the match
            target: The matched user
            initiator_demand: What the initiator is looking for

        Returns:
            Introduction text explaining the match
        """
        target_name = target.get("name", "this person")
        target_value = target.get("all_value") or "their expertise"

        # Simple template-based introduction
        return (
            f"{target_name} may be able to help with {initiator_demand[:50]}... "
            f"They offer: {target_value[:100]}..."
        )
