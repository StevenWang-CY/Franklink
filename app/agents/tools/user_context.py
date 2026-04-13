"""Unified user context builder using Zep knowledge graph.

Provides a single interface for retrieving comprehensive user context
from Zep's knowledge graph, with fallback to profile-based context.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)


async def get_enriched_user_context(
    user_id: str,
    query: Optional[str] = None,
    include_facts: bool = True,
    include_summary: bool = True,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Get comprehensive user context from Zep knowledge graph.

    This function provides a unified interface for retrieving user context,
    combining Zep's automatic fact extraction with user profile data.

    Args:
        user_id: User identifier
        query: Optional query to focus context retrieval
        include_facts: Whether to include extracted facts
        include_summary: Whether to include user summary
        thread_id: Optional thread ID for context-aware retrieval

    Returns:
        Dict containing:
        - source: "zep_graph" or "profile_fallback"
        - context: Context string for LLM prompts
        - user_summary: High-level user description
        - facts: List of relevant facts
        - email_insights: Email-derived insights
        - timestamp: When context was retrieved
    """
    result = {
        "source": "unknown",
        "context": "",
        "user_summary": "",
        "facts": [],
        "email_insights": [],
        "timestamp": datetime.utcnow().isoformat(),
    }

    if not user_id:
        result["source"] = "empty"
        return result

    # Try Zep graph context first
    if settings.zep_graph_enabled:
        zep_context = await _get_zep_context(
            user_id=user_id,
            query=query,
            thread_id=thread_id,
            include_facts=include_facts,
        )
        if zep_context:
            result.update(zep_context)
            result["source"] = "zep_graph"
            return result

    # Fallback to profile-based context
    profile_context = await _get_profile_context(user_id)
    if profile_context:
        result.update(profile_context)
        result["source"] = "profile_fallback"

    return result


async def _get_zep_context(
    user_id: str,
    query: Optional[str],
    thread_id: Optional[str],
    include_facts: bool,
) -> Optional[Dict[str, Any]]:
    """
    Get user context from Zep knowledge graph.

    Args:
        user_id: User identifier
        query: Optional focus query
        thread_id: Optional thread ID
        include_facts: Whether to fetch facts separately

    Returns:
        Context dict or None if unavailable
    """
    try:
        from app.integrations.zep_graph_client import get_zep_graph_client

        zep = get_zep_graph_client()
        if not zep.is_graph_enabled():
            return None

        result: Dict[str, Any] = {}

        # Get holistic user context
        context = await zep.get_user_context(
            user_id=user_id,
            thread_id=thread_id,
        )

        if context:
            result["context"] = context

            # Parse user summary from context if present
            if "<USER_SUMMARY>" in context:
                start = context.find("<USER_SUMMARY>") + len("<USER_SUMMARY>")
                end = context.find("</USER_SUMMARY>")
                if start > 0 and end > start:
                    result["user_summary"] = context[start:end].strip()

            # Parse facts from context if present
            if "<FACTS>" in context:
                start = context.find("<FACTS>") + len("<FACTS>")
                end = context.find("</FACTS>")
                if start > 0 and end > start:
                    facts_text = context[start:end].strip()
                    facts = []
                    for line in facts_text.split("\n"):
                        line = line.strip()
                        if line.startswith("-"):
                            facts.append(line[1:].strip())
                    result["facts"] = facts

        # Optionally get additional facts
        if include_facts and not result.get("facts"):
            facts = await zep.get_user_facts(user_id, limit=20)
            if facts:
                result["facts"] = [
                    f.get("fact", "") for f in facts if f.get("fact")
                ]

        # Search for email-related insights if we have a query
        if query:
            search_results = await zep.search_graph(
                user_id=user_id,
                query=query,
                scope="edges",
                limit=10,
            )
            if search_results:
                result["email_insights"] = [
                    r.fact for r in search_results if r.fact
                ]

        return result if result else None

    except Exception as e:
        logger.debug("[USER_CONTEXT] Zep context retrieval failed: %s", e)
        return None


async def _get_profile_context(user_id: str) -> Optional[Dict[str, Any]]:
    """
    Build context from user profile in Supabase.

    Args:
        user_id: User identifier

    Returns:
        Context dict or None if unavailable
    """
    try:
        from app.database.client import DatabaseClient

        db = DatabaseClient()
        profile = await db.get_user_by_id(user_id)

        if not profile:
            return None

        result: Dict[str, Any] = {}

        # Build user summary from profile fields
        summary_parts = []

        name = profile.get("name")
        if name:
            summary_parts.append(f"{name}")

        university = profile.get("university")
        major = profile.get("major")
        year = profile.get("year")
        if university:
            edu_parts = [university]
            if major:
                edu_parts.append(f"studying {major}")
            if year:
                edu_parts.append(f"({year})")
            summary_parts.append(" ".join(edu_parts))

        location = profile.get("location")
        if location:
            summary_parts.append(f"based in {location}")

        career_interests = profile.get("career_interests") or []
        if career_interests:
            summary_parts.append(f"interested in {', '.join(career_interests[:5])}")

        if summary_parts:
            result["user_summary"] = ". ".join(summary_parts)

        # Build facts from profile data
        facts = []

        all_demand = profile.get("all_demand")
        if all_demand:
            facts.append(f"Currently seeking: {all_demand}")

        all_value = profile.get("all_value")
        if all_value:
            facts.append(f"Can offer: {all_value}")

        if career_interests:
            facts.append(f"Career interests: {', '.join(career_interests)}")

        result["facts"] = facts

        # Build context string
        context_parts = []
        if result.get("user_summary"):
            context_parts.append(f"# User Profile\n{result['user_summary']}")
        if facts:
            context_parts.append(f"# Key Information\n" + "\n".join(f"- {f}" for f in facts))

        result["context"] = "\n\n".join(context_parts) if context_parts else ""

        return result if result.get("context") else None

    except Exception as e:
        logger.debug("[USER_CONTEXT] Profile context retrieval failed: %s", e)
        return None


async def get_context_for_matching(
    user_id: str,
    demand: Optional[str] = None,
    value: Optional[str] = None,
) -> Optional[str]:
    """
    Get focused context for connection matching.

    This returns a concise context string optimized for
    the adaptive matcher's LLM evaluation.

    Args:
        user_id: User identifier
        demand: User's current demand/need
        value: User's value proposition

    Returns:
        Context string or None
    """
    # Build a focused query from demand/value
    query_parts = []
    if demand:
        query_parts.append(f"seeking: {demand}")
    if value:
        query_parts.append(f"offering: {value}")

    query = ". ".join(query_parts) if query_parts else None

    context = await get_enriched_user_context(
        user_id=user_id,
        query=query,
        include_facts=True,
        include_summary=True,
    )

    if context.get("context"):
        return context["context"]

    # Build minimal context from available data
    parts = []
    if context.get("user_summary"):
        parts.append(context["user_summary"])
    if context.get("facts"):
        parts.extend(context["facts"][:5])
    if context.get("email_insights"):
        parts.extend(context["email_insights"][:3])

    return "\n".join(parts) if parts else None


async def search_user_context(
    user_id: str,
    query: str,
    limit: int = 10,
) -> List[str]:
    """
    Search user's knowledge graph for specific context.

    Args:
        user_id: User identifier
        query: Search query
        limit: Maximum results

    Returns:
        List of relevant facts/insights
    """
    if not settings.zep_graph_enabled:
        return []

    try:
        from app.integrations.zep_graph_client import get_zep_graph_client

        zep = get_zep_graph_client()
        if not zep.is_graph_enabled():
            return []

        results = await zep.search_graph(
            user_id=user_id,
            query=query,
            scope="edges",
            limit=limit,
        )

        return [r.fact for r in results if r.fact]

    except Exception as e:
        logger.debug("[USER_CONTEXT] Search failed: %s", e)
        return []
