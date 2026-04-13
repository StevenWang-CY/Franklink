"""Adaptive matcher for networking.

Uses multi-signal candidate generation and LLM-based selection to find
the best networking match that satisfies the user's demand while ensuring
mutual benefit for both parties.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.utils.context_embedding import build_context_text

logger = logging.getLogger(__name__)


# LLM Prompts for match selection
MATCH_SELECTION_SYSTEM_PROMPT = """You are a networking match analyzer for Franklink, a professional networking platform for students and early-career professionals.

Your task: Select the SINGLE BEST match from a list of candidates for the initiator's networking request.

## CRITICAL: Be SPECIFIC and CONCRETE

Franklink's core differentiation is that every intro ACTUALLY MATTERS. Generic explanations like "they share similar interests" or "both interested in startups" are USELESS. Users need to understand the SPECIFIC, CONCRETE reasons why this connection makes sense.

WRONG (too generic - users won't feel the value):
- "Both are interested in technology and startups"
- "They have complementary skills"
- "Similar career interests in finance"

RIGHT (specific and compelling - users understand the unique fit):
- "Beatrice is actively building a fintech startup and needs someone with Eric's ML experience for their recommendation engine"
- "Jimmy just finished a trading systems project at D.E. Shaw - exactly the HFT background Eric wants to learn from"
- "Steven is recruiting for his hackathon team and specifically needs frontend skills, which matches Eric's React portfolio"

## Evaluation Criteria

1. **Demand Satisfaction (35%)**: How SPECIFICALLY can this candidate help?
   - What CONCRETE skill, experience, project, or knowledge do they have?
   - Reference SPECIFIC details: company names, project names, course names, technologies
   - A "hackathon teammate" match should mention their specific tech stack or past project
   - A "mentor" match should mention their specific role, company, or experience

2. **Mutual Benefit (35%)**: What SPECIFIC motivation would the candidate have?
   - What does the INITIATOR offer that the CANDIDATE specifically needs?
   - Reference the candidate's stated demand or latent needs with specifics
   - "Eric's Franklink app experience could help with Beatrice's user engagement problem"
   - NOT "they could help each other" (too vague)

3. **Holistic Compatibility (20%)**: What SPECIFIC personality or style alignment exists?
   - Reference specific traits, working styles, or relationship preferences
   - "Both prefer hands-on collaboration over formal mentorship"
   - NOT "good personality fit" (meaningless)

4. **Context Compatibility (10%)**: What PRACTICAL factors make this work?
   - Same campus, same class year, overlapping schedules, shared events
   - "Both attending the Penn Blockchain hackathon this weekend"

## Red Flags (Reduce confidence significantly)
- Candidate has no clear value to offer AND initiator has nothing the candidate wants
- Candidate's demand directly conflicts with being available for this type of connection
- Obvious mismatch in career stage that makes exchange difficult
- Candidate's ideal relationship types don't include what initiator is looking for

## Output Format
Return ONLY valid JSON (no markdown, no explanation):
{
    "selected_user_id": "<uuid of best match>",
    "confidence": <0.0-1.0>,
    "rationale": {
        "demand_satisfaction": "<1-2 sentences with SPECIFIC details: names, companies, projects, skills>",
        "mutual_benefit": "<1-2 sentences explaining what SPECIFICALLY motivates the candidate>",
        "context_fit": "<1 sentence: specific practical factors>"
    },
    "match_summary": "<1 sentence with the SINGLE most compelling reason for this match>",
    "concern": "<any concern about this match, or null if none>"
}"""


@dataclass
class CandidateProfile:
    """Rich candidate profile for LLM evaluation."""
    user_id: str
    name: str
    phone_number: str
    university: Optional[str] = None
    major: Optional[str] = None
    year: Optional[str] = None
    location: Optional[str] = None
    career_interests: List[str] = field(default_factory=list)
    all_demand: Optional[str] = None
    all_value: Optional[str] = None
    context_summary: Optional[str] = None
    needs: List[Any] = field(default_factory=list)
    linkedin_data: Optional[Dict[str, Any]] = None

    # Match metadata
    match_sources: Set[str] = field(default_factory=set)
    similarity_scores: Dict[str, float] = field(default_factory=dict)

    # Zep knowledge graph enrichment
    zep_facts: List[str] = field(default_factory=list)

    # Holistic profile data (from user_profiles table)
    holistic_summary: Optional[str] = None
    latent_needs: List[str] = field(default_factory=list)
    ideal_relationship_types: List[str] = field(default_factory=list)
    career_stage: Optional[str] = None
    relationship_strengths: Optional[str] = None

    def get_background_context(self) -> str:
        """Extract relevant background context from personal_facts and linkedin."""
        context_parts = []

        # From linkedin_data
        if self.linkedin_data:
            if headline := self.linkedin_data.get("headline"):
                context_parts.append(f"LinkedIn: {headline}")
            if experiences := self.linkedin_data.get("experiences", []):
                recent = experiences[0] if experiences else {}
                if title := recent.get("title"):
                    company = recent.get("company", "")
                    context_parts.append(f"Current/Recent: {title} at {company}")
            if skills := self.linkedin_data.get("skills", []):
                context_parts.append(f"Skills: {', '.join(skills[:5])}")

        # From needs
        if self.needs:
            needs_str = ", ".join(str(n) for n in self.needs[:3])
            context_parts.append(f"Career needs: {needs_str}")

        return "\n".join(context_parts) if context_parts else "No additional context"

    def to_llm_format(self, index: int) -> str:
        """Format candidate for LLM prompt."""
        careers = ", ".join(self.career_interests) if self.career_interests else "Not specified"
        sources = ", ".join(self.match_sources) if self.match_sources else "unknown"
        best_similarity = max(self.similarity_scores.values()) if self.similarity_scores else 0.0

        # Build Zep insights section if available
        zep_insights = ""
        if self.zep_facts:
            zep_insights = "\n\n**Email/Context Insights:**\n" + "\n".join(
                f"- {fact}" for fact in self.zep_facts[:3]
            )

        # Build holistic profile section if available
        holistic_section = ""
        if self.holistic_summary:
            latent_needs_str = ", ".join(self.latent_needs) if self.latent_needs else "Not analyzed"
            relationship_types_str = ", ".join(self.ideal_relationship_types) if self.ideal_relationship_types else "Not analyzed"
            holistic_section = f"""

**Holistic Profile (AI-synthesized understanding):**
{self.holistic_summary}

- Latent Needs: {latent_needs_str}
- Ideal Relationship Types: {relationship_types_str}
- Career Stage: {self.career_stage or 'Unknown'}
- Relationship Strengths: {self.relationship_strengths or 'Not analyzed'}"""

        return f"""
### Candidate {index}: {self.name or 'Unknown'}
- User ID: {self.user_id}
- Match Sources: {sources} (best similarity: {best_similarity:.2f})
- University: {self.university or 'Not specified'} | Major: {self.major or 'Not specified'} | Year: {self.year or 'N/A'}
- Location: {self.location or 'Not specified'}
- Career Interests: {careers}
- Context Summary: {self.context_summary or 'Not available'}

**What they're looking for (DEMAND):**
{self.all_demand or 'Not specified'}

**What they can offer (VALUE):**
{self.all_value or 'Not specified'}

**Background/Context:**
{self.get_background_context()}{zep_insights}{holistic_section}
"""


@dataclass
class AdaptiveMatchResult:
    """Result from adaptive matching."""
    success: bool = False
    error_message: Optional[str] = None

    # Target user info
    target_user_id: Optional[str] = None
    target_name: Optional[str] = None
    target_phone: Optional[str] = None

    # Match quality
    match_confidence: float = 0.0
    match_score: float = 0.0  # Alias for match_confidence
    demand_satisfaction: Optional[str] = None
    mutual_benefit: Optional[str] = None
    match_summary: Optional[str] = None
    concern: Optional[str] = None
    matching_reasons: List[str] = field(default_factory=list)
    llm_introduction: Optional[str] = None
    llm_concern: Optional[str] = None


class AdaptiveMatcher:
    """
    Adaptive matcher for networking connections.

    Uses:
    1. Multi-signal embedding queries for broad candidate generation
    2. LLM-based intelligent selection for finding the best match

    This replaces the simpler ValueExchangeMatcher with a more flexible
    approach that can handle diverse networking demands (mentor, teammate,
    study partner, etc.) while ensuring mutual benefit.
    """

    CANDIDATE_POOL_SIZE = 8  # Number of candidates to pass to LLM
    VALUE_MATCH_COUNT = 10
    DEMAND_MATCH_COUNT = 8
    CONTEXT_MATCH_COUNT = 8
    REVERSE_MATCH_COUNT = 8
    HOLISTIC_MATCH_COUNT = 8  # Holistic profile similarity matches

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
    ) -> AdaptiveMatchResult:
        """Find the best match using adaptive multi-signal approach.

        Args:
            user_id: The initiator's user ID
            user_profile: The initiator's profile data
            excluded_user_ids: User IDs to exclude from matching
            override_demand: Override the user's demand for this search
            override_value: Override the user's value for this search

        Returns:
            AdaptiveMatchResult with match details or error
        """
        try:
            excluded = excluded_user_ids or []

            # Get the initiator's demand and value
            demand_text = (
                override_demand or
                user_profile.get("latest_demand") or
                user_profile.get("all_demand")
            )
            value_text = override_value or user_profile.get("all_value")

            if not demand_text:
                return AdaptiveMatchResult(
                    success=False,
                    error_message="No demand specified. What kind of help are you looking for?",
                )

            logger.info(f"[ADAPTIVE_MATCHER] Finding match for user {user_id}")
            logger.info(f"[ADAPTIVE_MATCHER] Demand: {demand_text[:100]}...")

            # Phase 1: Generate broad candidate pool
            candidates = await self._generate_candidate_pool(
                user_id=user_id,
                user_profile=user_profile,
                demand_text=demand_text,
                value_text=value_text,
                excluded_user_ids=excluded,
            )

            if not candidates:
                return AdaptiveMatchResult(
                    success=False,
                    error_message=(
                        "No suitable matches found at this time. "
                        "Try being more specific about what you're looking for."
                    ),
                )

            logger.info(
                f"[ADAPTIVE_MATCHER] Generated {len(candidates)} candidates, "
                f"passing top {min(len(candidates), self.CANDIDATE_POOL_SIZE)} to LLM"
            )

            # Phase 1.5a: Enrich candidates with Zep knowledge graph facts
            candidates = await self._enrich_candidates_with_zep(
                candidates=candidates,
                initiator_demand=demand_text,
            )

            # Phase 1.5b: Enrich candidates with holistic profile data
            candidates = await self._enrich_candidates_with_profiles(
                candidates=candidates,
            )

            # Phase 2: LLM selection
            selection = await self._llm_select_best_match(
                initiator_profile=user_profile,
                demand_text=demand_text,
                value_text=value_text,
                candidates=candidates[:self.CANDIDATE_POOL_SIZE],
            )

            if not selection:
                return AdaptiveMatchResult(
                    success=False,
                    error_message="Could not determine best match from candidates.",
                )

            # Find the selected candidate
            selected_id = selection.get("selected_user_id")
            selected = next(
                (c for c in candidates if c.user_id == selected_id),
                None
            )

            if not selected:
                logger.error(f"[ADAPTIVE_MATCHER] LLM selected unknown user: {selected_id}")
                return AdaptiveMatchResult(
                    success=False,
                    error_message="Match selection error. Please try again.",
                )

            # Build result
            rationale = selection.get("rationale", {})
            matching_reasons = self._build_matching_reasons(selected, rationale)

            return AdaptiveMatchResult(
                success=True,
                target_user_id=selected.user_id,
                target_name=selected.name,
                target_phone=selected.phone_number,
                match_confidence=selection.get("confidence", 0.0),
                match_score=selection.get("confidence", 0.0),
                demand_satisfaction=rationale.get("demand_satisfaction"),
                mutual_benefit=rationale.get("mutual_benefit"),
                match_summary=selection.get("match_summary"),
                concern=selection.get("concern"),
                matching_reasons=matching_reasons,
                llm_introduction=selection.get("match_summary"),
                llm_concern=selection.get("concern"),
            )

        except Exception as e:
            logger.error(f"[ADAPTIVE_MATCHER] find_best_match failed: {e}", exc_info=True)
            return AdaptiveMatchResult(
                success=False,
                error_message=f"Match search failed: {str(e)}",
            )

    async def _generate_candidate_pool(
        self,
        user_id: str,
        user_profile: Dict[str, Any],
        demand_text: str,
        value_text: Optional[str],
        excluded_user_ids: List[str],
    ) -> List[CandidateProfile]:
        """Generate candidates using multiple embedding queries.

        Runs 4 parallel queries:
        1. Value Match - candidates whose value matches initiator's demand
        2. Demand Match - candidates with similar demands (peer learning)
        3. Context Match - candidates with similar background
        4. Reverse Match - candidates whose demand matches initiator's value

        Args:
            user_id: Initiator's user ID
            user_profile: Initiator's profile
            demand_text: What the initiator is looking for
            value_text: What the initiator can offer
            excluded_user_ids: Users to exclude

        Returns:
            Deduplicated list of CandidateProfile objects
        """
        # Generate embeddings in parallel
        async def noop():
            return None

        demand_embedding_task = self.openai.get_embedding(demand_text)
        value_embedding_task = (
            self.openai.get_embedding(value_text)
            if value_text else noop()
        )
        context_text = build_context_text(user_profile)
        context_embedding_task = (
            self.openai.get_embedding(context_text)
            if context_text else noop()
        )

        demand_embedding, value_embedding, context_embedding = await asyncio.gather(
            demand_embedding_task,
            value_embedding_task,
            context_embedding_task,
        )

        if not demand_embedding:
            logger.error("[ADAPTIVE_MATCHER] Failed to generate demand embedding")
            return []

        # Run all matching queries in parallel
        tasks = []

        # Query 1: Value Match (traditional - their value matches my demand)
        tasks.append(self._query_with_source(
            "value",
            self.db.match_users_comprehensive(
                query_embedding=demand_embedding,
                embedding_type="value",
                exclude_user_id=user_id,
                exclude_user_ids=excluded_user_ids,
                match_threshold=0.30,
                match_count=self.VALUE_MATCH_COUNT,
            )
        ))

        # Query 2: Demand Match (their demand similar to my demand - peer learning)
        tasks.append(self._query_with_source(
            "demand",
            self.db.match_users_comprehensive(
                query_embedding=demand_embedding,
                embedding_type="demand",
                exclude_user_id=user_id,
                exclude_user_ids=excluded_user_ids,
                match_threshold=0.40,
                match_count=self.DEMAND_MATCH_COUNT,
            )
        ))

        # Query 3: Context Match (similar background)
        if context_embedding:
            tasks.append(self._query_with_source(
                "context",
                self.db.match_users_comprehensive(
                    query_embedding=context_embedding,
                    embedding_type="context",
                    exclude_user_id=user_id,
                    exclude_user_ids=excluded_user_ids,
                    match_threshold=0.40,
                    match_count=self.CONTEXT_MATCH_COUNT,
                )
            ))

        # Query 4: Reverse Match (their demand matches my value - they want someone like me)
        if value_embedding:
            tasks.append(self._query_with_source(
                "reverse",
                self.db.match_users_comprehensive(
                    query_embedding=value_embedding,
                    embedding_type="demand",
                    exclude_user_id=user_id,
                    exclude_user_ids=excluded_user_ids,
                    match_threshold=0.40,
                    match_count=self.REVERSE_MATCH_COUNT,
                )
            ))

        # Query 5: Holistic Match (similar holistic profiles - deeper compatibility)
        from app.config import settings
        if getattr(settings, "profile_synthesis_use_in_matching", True):
            initiator_profile_data = await self.db.get_user_profile(user_id)
            if initiator_profile_data and initiator_profile_data.get("holistic_embedding"):
                tasks.append(self._query_with_source(
                    "holistic",
                    self.db.match_users_by_profile(
                        query_embedding=initiator_profile_data["holistic_embedding"],
                        exclude_user_id=user_id,
                        exclude_user_ids=excluded_user_ids,
                        match_threshold=0.35,
                        match_count=self.HOLISTIC_MATCH_COUNT,
                    )
                ))

        # Execute all queries
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process and deduplicate results
        candidates_by_id: Dict[str, CandidateProfile] = {}

        for source, matches in results:
            if isinstance(matches, Exception):
                logger.warning(f"[ADAPTIVE_MATCHER] {source} query failed: {matches}")
                continue

            for match in matches:
                user_id_str = str(match.get("id"))

                if user_id_str in candidates_by_id:
                    # Merge: add source and update similarity
                    existing = candidates_by_id[user_id_str]
                    existing.match_sources.add(source)
                    existing.similarity_scores[source] = match.get("similarity", 0.0)
                else:
                    # Create new candidate
                    candidates_by_id[user_id_str] = CandidateProfile(
                        user_id=user_id_str,
                        name=match.get("name"),
                        phone_number=match.get("phone_number"),
                        university=match.get("university"),
                        major=match.get("major"),
                        year=match.get("year"),
                        location=match.get("location"),
                        career_interests=match.get("career_interests") or [],
                        all_demand=match.get("all_demand"),
                        all_value=match.get("all_value"),
                        context_summary=match.get("context_summary"),
                        needs=match.get("needs") or [],
                        linkedin_data=match.get("linkedin_data"),
                        match_sources={source},
                        similarity_scores={source: match.get("similarity", 0.0)},
                        # Holistic profile data (from holistic query results)
                        holistic_summary=match.get("holistic_summary"),
                        latent_needs=match.get("latent_needs") or [],
                        ideal_relationship_types=match.get("ideal_relationship_types") or [],
                        career_stage=match.get("career_stage"),
                    )

        # Sort by number of sources (more sources = more relevant) then by best similarity
        candidates = list(candidates_by_id.values())
        candidates.sort(
            key=lambda c: (
                len(c.match_sources),
                max(c.similarity_scores.values()) if c.similarity_scores else 0
            ),
            reverse=True
        )

        logger.info(
            f"[ADAPTIVE_MATCHER] Candidate pool: {len(candidates)} unique candidates "
            f"from {sum(1 for r in results if not isinstance(r[1], Exception))} queries"
        )

        return candidates

    async def _enrich_candidates_with_zep(
        self,
        candidates: List[CandidateProfile],
        initiator_demand: str,
        max_facts: int = 3,
    ) -> List[CandidateProfile]:
        """Enrich candidates with Zep knowledge graph facts.

        For each candidate, searches their Zep knowledge graph for facts
        relevant to the initiator's demand. This provides additional context
        for LLM-based match selection.

        Args:
            candidates: List of candidates to enrich
            initiator_demand: The initiator's networking demand
            max_facts: Maximum facts to fetch per candidate

        Returns:
            List of candidates with zep_facts populated
        """
        from app.config import settings

        if not getattr(settings, 'zep_graph_enabled', False):
            return candidates

        if not getattr(settings, 'zep_graph_enrich_candidates', True):
            return candidates

        from app.agents.tools.user_context import search_user_context

        async def enrich_one(candidate: CandidateProfile) -> CandidateProfile:
            try:
                facts = await search_user_context(
                    user_id=candidate.user_id,
                    query=initiator_demand,
                    limit=max_facts,
                )
                if facts:
                    # Create new candidate with zep_facts (immutable pattern)
                    # Preserve all fields including holistic profile data
                    return CandidateProfile(
                        user_id=candidate.user_id,
                        name=candidate.name,
                        phone_number=candidate.phone_number,
                        university=candidate.university,
                        major=candidate.major,
                        year=candidate.year,
                        location=candidate.location,
                        career_interests=candidate.career_interests,
                        all_demand=candidate.all_demand,
                        all_value=candidate.all_value,
                        context_summary=candidate.context_summary,
                        needs=candidate.needs,
                        linkedin_data=candidate.linkedin_data,
                        match_sources=candidate.match_sources,
                        similarity_scores=candidate.similarity_scores,
                        zep_facts=facts,
                        # Preserve holistic profile fields
                        holistic_summary=candidate.holistic_summary,
                        latent_needs=candidate.latent_needs,
                        ideal_relationship_types=candidate.ideal_relationship_types,
                        career_stage=candidate.career_stage,
                        relationship_strengths=candidate.relationship_strengths,
                    )
                return candidate
            except Exception as e:
                logger.debug(
                    f"[ADAPTIVE_MATCHER] Zep enrichment failed for {candidate.user_id[:8]}: {e}"
                )
                return candidate

        # Enrich top candidates in parallel
        pool_size = min(len(candidates), self.CANDIDATE_POOL_SIZE)
        tasks = [enrich_one(c) for c in candidates[:pool_size]]
        enriched = await asyncio.gather(*tasks)

        # Replace enriched candidates, keep rest unchanged
        result = list(enriched) + candidates[pool_size:]

        enriched_count = sum(1 for c in enriched if c.zep_facts)
        if enriched_count > 0:
            logger.info(
                f"[ADAPTIVE_MATCHER] Enriched {enriched_count}/{pool_size} candidates with Zep facts"
            )

        return result

    async def _enrich_candidates_with_profiles(
        self,
        candidates: List[CandidateProfile],
    ) -> List[CandidateProfile]:
        """Enrich candidates with holistic profile data.

        For candidates that don't already have holistic profile data
        (i.e., came from non-holistic queries), fetch their profiles
        from the database.

        Args:
            candidates: List of candidates to enrich

        Returns:
            List of candidates with holistic profile data populated
        """
        from app.config import settings

        if not getattr(settings, "profile_synthesis_use_in_matching", True):
            return candidates

        async def enrich_one(candidate: CandidateProfile) -> CandidateProfile:
            if candidate.holistic_summary:
                return candidate

            try:
                profile = await self.db.get_user_profile(candidate.user_id)
                if profile:
                    return CandidateProfile(
                        user_id=candidate.user_id,
                        name=candidate.name,
                        phone_number=candidate.phone_number,
                        university=candidate.university,
                        major=candidate.major,
                        year=candidate.year,
                        location=candidate.location,
                        career_interests=candidate.career_interests,
                        all_demand=candidate.all_demand,
                        all_value=candidate.all_value,
                        context_summary=candidate.context_summary,
                        needs=candidate.needs,
                        linkedin_data=candidate.linkedin_data,
                        match_sources=candidate.match_sources,
                        similarity_scores=candidate.similarity_scores,
                        zep_facts=candidate.zep_facts,
                        holistic_summary=profile.get("holistic_summary"),
                        latent_needs=profile.get("latent_needs") or [],
                        ideal_relationship_types=profile.get("ideal_relationship_types") or [],
                        career_stage=profile.get("career_stage"),
                        relationship_strengths=profile.get("relationship_strengths"),
                    )
                return candidate
            except Exception as e:
                logger.debug(
                    f"[ADAPTIVE_MATCHER] Profile enrichment failed for {candidate.user_id[:8]}: {e}"
                )
                return candidate

        pool_size = min(len(candidates), self.CANDIDATE_POOL_SIZE)
        tasks = [enrich_one(c) for c in candidates[:pool_size]]
        enriched = await asyncio.gather(*tasks)

        result = list(enriched) + candidates[pool_size:]

        enriched_count = sum(1 for c in enriched if c.holistic_summary)
        if enriched_count > 0:
            logger.info(
                f"[ADAPTIVE_MATCHER] Enriched {enriched_count}/{pool_size} candidates with holistic profiles"
            )

        return result

    async def _query_with_source(
        self,
        source: str,
        query_coro,
    ) -> tuple:
        """Execute a query and tag results with source."""
        try:
            result = await query_coro
            return (source, result)
        except Exception as e:
            return (source, e)

    async def _llm_select_best_match(
        self,
        initiator_profile: Dict[str, Any],
        demand_text: str,
        value_text: Optional[str],
        candidates: List[CandidateProfile],
    ) -> Optional[Dict[str, Any]]:
        """Use LLM to select the best match from candidates.

        Args:
            initiator_profile: The initiator's profile
            demand_text: What the initiator is looking for
            value_text: What the initiator can offer
            candidates: List of candidate profiles to evaluate

        Returns:
            Dict with selection result or None if failed
        """
        if not candidates:
            return None

        # Build initiator context
        initiator_name = initiator_profile.get("name", "Unknown")
        initiator_university = initiator_profile.get("university", "Not specified")
        initiator_major = initiator_profile.get("major", "Not specified")
        initiator_year = initiator_profile.get("year", "N/A")
        initiator_career_interests = ", ".join(
            initiator_profile.get("career_interests", [])
        ) or "Not specified"
        initiator_needs = ", ".join(
            str(n) for n in initiator_profile.get("needs", [])[:3]
        ) or "Not specified"

        # Build additional context
        initiator_context_parts = []
        if grade := initiator_profile.get("year"):
            initiator_context_parts.append(f"Grade level: {grade}")
        if location := initiator_profile.get("location"):
            initiator_context_parts.append(f"Location: {location}")
        initiator_context = "\n".join(initiator_context_parts) or "None"

        # Build candidates section
        candidates_section = ""
        for i, candidate in enumerate(candidates, 1):
            candidates_section += candidate.to_llm_format(i)

        user_prompt = f"""## Initiator Profile
Name: {initiator_name}
University: {initiator_university}
Major: {initiator_major}
Year: {initiator_year}
Career Interests: {initiator_career_interests}
Career Needs: {initiator_needs}

### What they're looking for (NETWORKING DEMAND):
{demand_text}

### What they can offer (VALUE):
{value_text or 'Not specified'}

### Additional Context:
{initiator_context}

---

## Candidates to Evaluate
{candidates_section}

---

Based on the initiator's networking demand and the available candidates, select the SINGLE BEST match that:
1. Best satisfies what the initiator is looking for
2. Has clear motivation to engage (mutual benefit)

Return ONLY valid JSON with your selection."""

        try:
            response = await self.openai.generate_response(
                system_prompt=MATCH_SELECTION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                model="gpt-4o-mini",
                temperature=0.3,
                trace_label="adaptive_match_selection",
            )

            # Parse JSON response
            cleaned = response.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            result = json.loads(cleaned)

            # Validate result has required fields
            if not result.get("selected_user_id"):
                logger.error("[ADAPTIVE_MATCHER] LLM response missing selected_user_id")
                return None

            logger.info(
                f"[ADAPTIVE_MATCHER] LLM selected: {result.get('selected_user_id')} "
                f"(confidence: {result.get('confidence', 0.0):.2f})"
            )

            return result

        except json.JSONDecodeError as e:
            logger.error(f"[ADAPTIVE_MATCHER] Failed to parse LLM response: {e}")
            logger.error(f"[ADAPTIVE_MATCHER] Raw response: {response[:500]}")
            return None
        except Exception as e:
            logger.error(f"[ADAPTIVE_MATCHER] LLM selection failed: {e}", exc_info=True)
            return None

    def _build_matching_reasons(
        self,
        candidate: CandidateProfile,
        rationale: Dict[str, str],
    ) -> List[str]:
        """Build human-readable matching reasons.

        Args:
            candidate: The selected candidate
            rationale: LLM's rationale for the selection

        Returns:
            List of matching reason strings
        """
        reasons = []

        # Add demand satisfaction reason
        if demand_sat := rationale.get("demand_satisfaction"):
            reasons.append(demand_sat)

        # Add mutual benefit reason
        if mutual := rationale.get("mutual_benefit"):
            reasons.append(mutual)

        # Add context if relevant
        if context := rationale.get("context_fit"):
            reasons.append(context)

        # Fallback to basic reasons if rationale is empty
        if not reasons:
            if candidate.all_value:
                reasons.append(f"Can help with: {candidate.all_value[:100]}...")
            if candidate.career_interests:
                careers = ", ".join(candidate.career_interests[:3])
                reasons.append(f"Background in: {careers}")
            if candidate.university:
                reasons.append(f"From {candidate.university}")

        return reasons[:3]
