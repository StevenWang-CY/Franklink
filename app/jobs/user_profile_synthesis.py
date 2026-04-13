"""Background job for synthesizing holistic user profiles from Zep knowledge graph.

This job analyzes a user's Zep knowledge graph (emails, conversations, signals)
and synthesizes a holistic understanding of who they are, including:
- Inferred traits (personality, communication style, work patterns)
- Latent needs (what they actually need vs what they ask for)
- Relationship potential (ideal relationship types, strengths, risks)
- Life trajectory (career stage, motivations, direction)

The synthesized profile is used to enhance matching by understanding users
at a deeper level than explicit demand/value statements.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings
from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.integrations.zep_graph_client import get_zep_graph_client

logger = logging.getLogger(__name__)


PROFILE_SYNTHESIS_SYSTEM_PROMPT = """You are analyzing a user's professional profile for Franklink, a networking platform for students and early-career professionals.

Given their Zep knowledge graph facts (extracted from emails and conversations) and profile data, synthesize a holistic understanding of who they are.

## CRITICAL: Be Specific, Not Generic

Your analysis MUST include CONCRETE DETAILS from the data. Generic statements like "proactive and ambitious" or "interested in technology" are USELESS for matching.

WRONG (too generic):
- "Eric is proactive and entrepreneurial"
- "Interested in startups and technology"
- "Direct communication style"

RIGHT (specific and actionable):
- "Eric cold-emails industry professionals requesting coffee chats about HFT and ML trading - shows initiative but may come across as transactional"
- "Building Franklink (AI career companion) with real users - has hands-on startup experience, not just interest"
- "Emails are short, direct, always include specific asks (e.g., '15-min call about portfolio construction') - efficient but may miss rapport-building"

## Analysis Instructions

1. **Inferred Traits**: What SPECIFIC patterns do you see?
   - Quote or reference ACTUAL emails/facts: "Sent 3 emails to Chuyue about quant trading" shows persistence
   - What topics do they repeatedly engage with? Name them specifically
   - HOW do they communicate? Short/long emails? Formal/casual? What specific phrases or patterns?

2. **Latent Needs**: What do they ACTUALLY need (with evidence)?
   - Base this on GAPS between their actions and stated goals
   - Example: "Emailing professionals about HFT but studying CS at Penn - may need bridge to finance industry"
   - Example: "Building a startup alone - likely needs co-founder or technical collaborators"

3. **Relationship Potential**: Be specific about WHY
   - Don't just say "would benefit from mentor" - say "needs mentor in quantitative finance given interest in HFT but academic focus on CS"
   - Consider: What specific person would complement them? "Someone with trading desk experience" not just "mentor"

4. **Life Trajectory**: Where SPECIFICALLY are they headed?
   - What career path do the facts suggest? "Exploring quant trading at firms like D.E. Shaw (received recruitment email)"
   - What decisions/transitions are they facing? "Choosing between startup path (Franklink) and finance path (quant interest)"

5. **Holistic Summary**: Make it MATCHABLE
   - Include specific interests: "quant trading, HFT, ML in finance, startup operations"
   - Include specific needs: "needs someone who has worked at a quant fund or trading desk"
   - Include specific offerings: "can offer startup operations experience, product development skills"
   - A good test: Could someone read this and know EXACTLY what kind of person to match them with?

## Output Format

Return ONLY valid JSON (no markdown, no explanation):
{
    "personality_summary": "2-3 sentences with SPECIFIC behavioral evidence from the data",
    "communication_style": "1-2 sentences describing HOW they communicate with examples",
    "work_patterns": "1-2 sentences on work habits with evidence",
    "latent_needs": ["specific_need_1", "specific_need_2", "specific_need_3"],
    "unspoken_gaps": "1-2 sentences on specific gaps between goals and current situation",
    "ideal_relationship_types": ["type1", "type2"],
    "relationship_strengths": "1-2 sentences with specific strengths",
    "relationship_risks": "1-2 sentences with specific risks/challenges",
    "trajectory_summary": "2-3 sentences on specific career direction with evidence",
    "core_motivations": ["specific_motivation_1", "specific_motivation_2"],
    "career_stage": "early_explorer|skill_builder|career_changer|established",
    "holistic_summary": "2-3 paragraphs with SPECIFIC details useful for matching",
    "confidence_score": 0.0-1.0
}

IMPORTANT: Base confidence_score on data richness:
- 0.9+ = Rich email history, clear patterns, consistent signals
- 0.7-0.9 = Good data, some patterns clear
- 0.5-0.7 = Limited data, making reasonable inferences
- <0.5 = Insufficient data, high uncertainty"""


FACT_FILTER_PROMPT = """You are filtering Zep knowledge graph facts for a user profile synthesis.

Your task: From the list of facts below, select ONLY the facts that reveal something meaningful about the USER's:
- Actions they took (emails sent, applications submitted, projects built)
- Interests and goals (what topics they engage with, what they're seeking)
- Professional activities (work, projects, collaborations)
- Communication patterns (how they reach out to people)

EXCLUDE facts that are:
- Generic university announcements or newsletters
- News articles or external events
- Assignment due dates or academic admin
- Marketing/promotional content
- Events the user didn't initiate

Return a JSON array of the INDICES (0-based) of facts to KEEP. Only include facts that reveal something specific about WHO this user is.

Example output: [0, 3, 5, 8, 12]

Facts to filter:
{facts}

Return ONLY the JSON array of indices, nothing else."""


async def _filter_facts_with_llm(
    facts: List[Dict[str, Any]],
    openai: "AzureOpenAIClient",
    user_name: str,
) -> List[Dict[str, Any]]:
    """
    Use a fast LLM to filter Zep facts for high-signal content.

    Args:
        facts: Raw Zep facts
        openai: OpenAI client for LLM filtering
        user_name: User's name to help identify user-specific facts

    Returns:
        Filtered list of high-signal facts
    """
    if not facts:
        return []

    # Format facts for filtering (include index for selection)
    facts_text = "\n".join(
        f"[{i}] {fact.get('fact', '')}"
        for i, fact in enumerate(facts[:100])  # Limit to 100 for cost
    )

    try:
        response = await openai.generate_response(
            system_prompt=FACT_FILTER_PROMPT.format(facts=facts_text),
            user_prompt=f"Filter facts for user: {user_name}",
            model="gpt-4o-mini",  # Fast model for filtering
            temperature=0.0,
            trace_label="fact_filter",
        )

        # Parse response as JSON array
        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        indices = json.loads(cleaned)

        # Return selected facts
        filtered = [facts[i] for i in indices if 0 <= i < len(facts)]
        logger.info(f"Filtered {len(facts[:100])} facts down to {len(filtered)} high-signal facts")
        return filtered

    except Exception as e:
        logger.warning(f"Fact filtering failed, using first 50 facts: {e}")
        return facts[:50]


def _format_zep_facts(facts: List[Dict[str, Any]]) -> str:
    """Format Zep facts for the LLM prompt."""
    if not facts:
        return "No Zep facts available."

    formatted = []
    for fact in facts[:75]:  # Allow more facts since they're filtered
        fact_text = fact.get("fact", "")
        if fact_text:
            created = fact.get("created_at", "")[:10] if fact.get("created_at") else ""
            prefix = f"[{created}] " if created else ""
            formatted.append(f"- {prefix}{fact_text}")

    return "\n".join(formatted) if formatted else "No Zep facts available."


def _format_user_data(
    facts: List[Dict[str, Any]],
    context: Optional[str],
    user: Dict[str, Any],
) -> str:
    """Format all user data for the LLM prompt."""
    from app.utils.demand_value_derived_fields import combine_texts

    demand_history = user.get("demand_history") or []
    value_history = user.get("value_history") or []

    demand_text = combine_texts(demand_history) if demand_history else "Not specified"
    value_text = combine_texts(value_history) if value_history else "Not specified"

    career_interests = user.get("career_interests") or []
    career_interests_str = ", ".join(career_interests) if career_interests else "Not specified"

    return f"""## Zep Knowledge Graph Facts
{_format_zep_facts(facts)}

## Zep User Context Summary
{context or "No context summary available."}

## Existing Profile Data
- Name: {user.get("name") or "Unknown"}
- University: {user.get("university") or "Not specified"}
- Major: {user.get("major") or "Not specified"}
- Year: {user.get("year") or "Not specified"}
- Location: {user.get("location") or "Not specified"}
- Career Interests: {career_interests_str}

## What they've explicitly asked for (demand_history):
{demand_text}

## What they've explicitly offered (value_history):
{value_text}

## Additional Context
- Career Goals: {user.get("career_goals") or "Not specified"}
- LinkedIn Headline: {(user.get("linkedin_data") or {}).get("headline") or "Not available"}
"""


async def synthesize_user_profile(
    user_id: str,
    db: Optional[DatabaseClient] = None,
    openai: Optional[AzureOpenAIClient] = None,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Synthesize a holistic profile for a single user.

    Args:
        user_id: User UUID
        db: Database client (creates one if not provided)
        openai: OpenAI client (creates one if not provided)
        force: If True, synthesize even if recent profile exists

    Returns:
        Synthesized profile dict or None if insufficient data
    """
    db = db or DatabaseClient()
    openai = openai or AzureOpenAIClient()
    zep = get_zep_graph_client()

    try:
        if not force:
            existing = await db.get_user_profile(user_id)
            if existing and existing.get("computed_at"):
                computed_at = datetime.fromisoformat(
                    existing["computed_at"].replace("Z", "+00:00")
                )
                age_days = (datetime.now(computed_at.tzinfo) - computed_at).days
                if age_days < getattr(settings, "profile_synthesis_stale_days", 7):
                    logger.debug(f"Profile for {user_id[:8]}... is fresh, skipping")
                    return existing

        user = await db.get_user_by_id(user_id)
        if not user:
            logger.warning(f"User {user_id} not found")
            return None

        # Fetch more facts than we need, then filter for signal
        raw_facts = await zep.get_user_facts(user_id, limit=200)
        context = await zep.get_user_context(user_id)

        demand_history = user.get("demand_history") or []
        min_facts = getattr(settings, "profile_synthesis_min_facts", 3)

        if len(raw_facts) < min_facts and not demand_history:
            logger.info(
                f"Insufficient data for {user_id[:8]}... "
                f"(facts={len(raw_facts)}, demands={len(demand_history)})"
            )
            return None

        # Filter facts for high-signal content using fast LLM
        user_name = user.get("name") or "Unknown"
        filtered_facts = await _filter_facts_with_llm(raw_facts, openai, user_name)

        logger.info(
            f"Filtered {len(raw_facts)} raw facts to {len(filtered_facts)} high-signal facts"
        )

        user_data_prompt = _format_user_data(filtered_facts, context, user)

        logger.info(f"Synthesizing profile for {user_id[:8]}... ({len(filtered_facts)} filtered facts)")

        response = await openai.generate_response(
            system_prompt=PROFILE_SYNTHESIS_SYSTEM_PROMPT,
            user_prompt=user_data_prompt,
            model="gpt-4o",
            temperature=0.3,
            trace_label=f"profile_synthesis_{user_id[:8]}",
        )

        cleaned = response.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        profile_data = json.loads(cleaned)

        holistic_summary = profile_data.get("holistic_summary", "")
        holistic_embedding = None
        if holistic_summary:
            holistic_embedding = await openai.get_embedding(holistic_summary)

        profile_data["holistic_embedding"] = holistic_embedding
        profile_data["zep_facts_count"] = len(filtered_facts)
        profile_data["computed_at"] = datetime.utcnow().isoformat()

        result = await db.upsert_user_profile(user_id, profile_data)

        logger.info(
            f"Synthesized profile for {user_id[:8]}... "
            f"(confidence={profile_data.get('confidence_score', 0):.2f})"
        )

        return result

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response for {user_id[:8]}...: {e}")
        return None
    except Exception as e:
        logger.error(f"Profile synthesis failed for {user_id[:8]}...: {e}", exc_info=True)
        return None


async def run_profile_synthesis_job(
    batch_size: int = 50,
    stale_days: int = 7,
    rate_limit_seconds: float = 2.0,
) -> Dict[str, Any]:
    """
    Background job to synthesize profiles for all eligible users.

    Args:
        batch_size: Max users to process per run
        stale_days: Consider profiles stale after this many days
        rate_limit_seconds: Pause between users to avoid API limits

    Returns:
        Dict with job statistics
    """
    if not getattr(settings, "profile_synthesis_enabled", True):
        logger.info("Profile synthesis job disabled via settings")
        return {"status": "disabled"}

    db = DatabaseClient()
    openai = AzureOpenAIClient()

    stats = {
        "started_at": datetime.utcnow().isoformat(),
        "users_processed": 0,
        "profiles_created": 0,
        "profiles_skipped": 0,
        "errors": 0,
    }

    try:
        users = await db.get_users_needing_profile_synthesis(
            stale_days=stale_days,
            batch_limit=batch_size,
        )

        logger.info(f"Profile synthesis job starting: {len(users)} users to process")

        for user_record in users:
            user_id = user_record.get("user_id")
            reason = user_record.get("reason", "unknown")

            try:
                result = await synthesize_user_profile(
                    user_id=user_id,
                    db=db,
                    openai=openai,
                    force=(reason == "stale_profile"),
                )

                stats["users_processed"] += 1

                if result:
                    stats["profiles_created"] += 1
                else:
                    stats["profiles_skipped"] += 1

            except Exception as e:
                logger.error(f"Error processing {user_id[:8]}...: {e}")
                stats["errors"] += 1

            await asyncio.sleep(rate_limit_seconds)

        stats["completed_at"] = datetime.utcnow().isoformat()
        stats["status"] = "completed"

        logger.info(
            f"Profile synthesis job completed: "
            f"{stats['profiles_created']} created, "
            f"{stats['profiles_skipped']} skipped, "
            f"{stats['errors']} errors"
        )

        return stats

    except Exception as e:
        logger.error(f"Profile synthesis job failed: {e}", exc_info=True)
        stats["status"] = "failed"
        stats["error"] = str(e)
        return stats


async def synthesize_profile_after_email_sync(user_id: str) -> Optional[Dict[str, Any]]:
    """
    Trigger profile synthesis after email sync completes.

    Called by email sync job when new emails are added to Zep.
    Only synthesizes if profile is missing or stale.

    Args:
        user_id: User UUID

    Returns:
        Synthesized profile or None
    """
    if not getattr(settings, "profile_synthesis_enabled", True):
        return None

    return await synthesize_user_profile(user_id, force=False)
