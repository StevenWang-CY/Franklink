"""LLM-based signal extraction from Zep knowledge graph.

Analyzes user's email activity stored in Zep to identify top networking signals.
Uses Zep's search_graph() and get_user_context() APIs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from app.integrations.azure_openai_client import AzureOpenAIClient
from app.proactive.config import PROACTIVE_OUTREACH_MAX_SIGNALS

logger = logging.getLogger(__name__)


ZEP_SIGNAL_EXTRACTION_SYSTEM_PROMPT = """You suggest SPECIFIC, ACTIONABLE networking signals based on a user's recent emails stored in their knowledge graph.

## Your Role
You analyze a user's recent email facts to identify CONCRETE opportunities where connecting with someone would help. These should be grounded in specific events, deadlines, or activities from their emails.

## CRITICAL: TIME-SENSITIVE PRIORITIZATION
The user context includes TODAY'S DATE. Use it to evaluate time-sensitivity:
1. Events in the NEXT 3 DAYS = HIGHEST PRIORITY (must suggest these!)
2. Events in the next 7 days = HIGH PRIORITY
3. Ongoing activities (gym buddy, study partner) = MEDIUM PRIORITY
4. PAST EVENTS = AUTOMATICALLY REJECT (do NOT suggest anything that already happened)

## AUTOMATIC REJECTION RULES
- If an event date is BEFORE today's date, DO NOT suggest it
- If you see "October midterm" and today is January, that's OLD - SKIP IT
- If you see "last week's hackathon", SKIP IT
- Only suggest events/deadlines that are UPCOMING or ongoing activities

## What to Look For (SPECIFIC opportunities from emails)
- Academic: "study partner for CIS 520 final next week", "someone to review my thesis draft"
- Events/Info Sessions: "someone to attend the Penn Blockchain info session with", "buddy for the startup career fair"
- Projects: "teammate for the hackathon this weekend", "co-founder for the AI project I'm working on"
- Research: "collaborator for HFT research", "someone also working on ML for finance"
- Social/Activities: "gym buddy at Pottruck", "someone to grab lunch with after class"
- Practice: "mock interview partner for quant roles", "someone to practice case studies with"
- Job Search: "referral at Google for PM role", "someone who went through the Jane Street interview process"

## MATCH TYPE CLASSIFICATION
- "single": Best for mentor/advisor, coffee chat, expert advice, job referral (1 ideal connection)
- "multi": Best for study groups, cofounder search, project collaboration, peer networking (2-5 people)

## What Makes a GOOD Signal
✅ Tied to a SPECIFIC email/event (mentions the actual name, date, or topic)
✅ Event is UPCOMING (in the future relative to today's date) or ongoing
✅ Clear what kind of person they need
✅ Actionable - we can search for this person in our network

## What Makes a BAD Signal
❌ Vague/generic ("find a mentor", "connect with someone in tech")
❌ Not grounded in their emails (just guessing based on profile)
❌ Too broad ("someone interested in AI")
❌ EVENT HAS ALREADY PASSED (check dates against today!)

## Output Format
Return JSON only:
{
    "signals": [
        {
            "signal_text": "Looking for someone with PM interview experience at FAANG to prep for upcoming Google interview",
            "group_name": "Google PM Interview Prep",
            "signal_rank": 1,
            "urgency_score": 0.9,
            "relevance_score": 0.85,
            "extraction_reasoning": "User has Google PM interview scheduled in 2 weeks based on email confirmation.",
            "match_type": "single",
            "max_matches": 1
        },
        {
            "signal_text": "Looking for people to form a study group for CS 161 algorithms final",
            "group_name": "CS 161 Study Group",
            "signal_rank": 2,
            "urgency_score": 0.7,
            "relevance_score": 0.9,
            "extraction_reasoning": "User has emails about CS 161 exam prep and mentioned looking for study partners.",
            "match_type": "multi",
            "max_matches": 4
        }
    ]
}

If no clear networking signals can be identified, return:
{
    "signals": [],
    "skip_reason": "No specific time-sensitive opportunities detected in recent emails"
}

## Rules
1. Maximum 3 signals
2. Each signal must be SPECIFIC (mention the actual event, class, project, topic)
3. Each signal MUST have evidence from their recent emails (in extraction_reasoning)
4. PRIORITIZE events happening in the next 3 days - these are HIGHEST VALUE
5. NEVER suggest past events - always check dates against today's date
6. Keep signal_text conversational and under 20 words
7. group_name: A short, catchy name for the iMessage group chat (max 30 chars)
   - Good: "Google PM Interview Prep", "CS 161 Study Group", "Hackathon Team"
   - Bad: "Looking for PM mentor", "Finding study partners"
"""


def _sort_facts_by_recency(
    raw_facts: List[Dict[str, Any]],
    recent_days: int = 3,
) -> tuple[List[str], List[str]]:
    """Sort facts into recent and older buckets based on valid_from or created_at.

    Args:
        raw_facts: List of fact dicts with fact, created_at, valid_from fields
        recent_days: Number of days to consider "recent"

    Returns:
        Tuple of (recent_facts, older_facts) as string lists
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=recent_days)

    recent = []
    older = []

    for item in raw_facts:
        fact_text = item.get("fact", "")
        if not fact_text:
            continue

        # Try valid_from first, then created_at
        timestamp_str = item.get("valid_from") or item.get("created_at")
        is_recent = False

        if timestamp_str:
            try:
                # Handle various timestamp formats
                if isinstance(timestamp_str, str):
                    # Try ISO format
                    if "T" in timestamp_str:
                        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                    else:
                        ts = datetime.fromisoformat(timestamp_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    is_recent = ts >= recent_cutoff
            except Exception:
                pass  # Default to older if parsing fails

        if is_recent:
            recent.append(fact_text)
        else:
            older.append(fact_text)

    return recent, older


def _build_user_context(user_profile: Dict[str, Any]) -> str:
    """Build user context string from profile."""
    parts = []

    name = user_profile.get("name")
    if name:
        parts.append(f"Name: {name}")

    university = user_profile.get("university")
    if university:
        parts.append(f"School: {university}")

    career_interests = user_profile.get("career_interests") or []
    if career_interests:
        parts.append(f"Career interests: {', '.join(career_interests)}")

    # Support both old and new naming
    signal = user_profile.get("latest_signal") or user_profile.get("latest_demand") or user_profile.get("all_demand")
    if signal:
        parts.append(f"Current networking goal: {signal}")

    value = user_profile.get("all_value")
    if value:
        parts.append(f"What they offer: {value}")

    return "\n".join(parts) if parts else "No profile context available"


async def _sync_signals_to_zep_background(
    user_id: str,
    signals: List[Dict[str, Any]],
) -> None:
    """
    Sync extracted signals to Zep knowledge graph.

    This adds networking signals as facts to the user's graph for
    future context retrieval and matching.
    """
    try:
        from app.config import settings

        if not settings.zep_graph_enabled or not settings.zep_graph_sync_signals:
            return

        from app.agents.tools.email_zep_sync import sync_signals_to_zep

        result = await sync_signals_to_zep(user_id=user_id, signals=signals)

        if result.get("success"):
            logger.debug(
                "[SIGNAL_EXTRACTOR] Synced %d signals to Zep for user=%s",
                result.get("signals_synced", 0),
                user_id[:8] if user_id else "?",
            )
        else:
            logger.debug(
                "[SIGNAL_EXTRACTOR] Zep signal sync failed for user=%s: %s",
                user_id[:8] if user_id else "?",
                result.get("errors", [])[:2],
            )
    except Exception as e:
        logger.debug(
            "[SIGNAL_EXTRACTOR] Zep signal sync error for user=%s: %s",
            user_id[:8] if user_id else "?",
            e,
        )


async def extract_signals_from_zep(
    *,
    user_id: str,
    user_profile: Dict[str, Any],
    max_signals: int = PROACTIVE_OUTREACH_MAX_SIGNALS,
) -> List[Dict[str, Any]]:
    """
    Extract top networking signals using Zep knowledge graph.

    Queries Zep's search_graph() for time-sensitive facts from user's emails,
    then uses LLM to extract actionable networking signals.

    Args:
        user_id: User ID
        user_profile: User's profile data
        max_signals: Maximum number of signals to extract

    Returns:
        List of signal dicts with keys:
        - signal_text: str
        - signal_rank: int (1, 2, or 3)
        - urgency_score: float
        - relevance_score: float
        - extraction_reasoning: str
        - match_type: str ('single' or 'multi')
        - max_matches: int
        - group_name: str (short name for iMessage group)
    """
    from datetime import datetime

    from app.integrations.zep_graph_client import ZepGraphClient

    try:
        zep = ZepGraphClient()

        # Check if Zep graph is available
        if not zep.is_graph_enabled():
            logger.info(
                "[SIGNAL_EXTRACTOR] zep_disabled user_id=%s",
                user_id[:8] if user_id else "?",
            )
            return []

        # Search for time-sensitive and collaboration-related facts
        # Query is intentionally broad to capture various opportunities
        search_query = (
            "deadline due tomorrow this week next week upcoming RSVP register "
            "event session meeting partner teammate collaborator opportunity "
            "project research study interview application job offer role "
            "hackathon conference info session career fair midterm final exam"
        )

        search_results = await zep.search_graph(
            user_id=user_id,
            query=search_query,
            scope="edges",
            limit=50,
        )

        # Extract facts from search results
        raw_facts = []
        for result in search_results:
            if hasattr(result, "fact") and result.fact:
                raw_facts.append({
                    "fact": result.fact,
                    "created_at": getattr(result, "created_at", None),
                    "valid_from": getattr(result, "valid_from", None),
                    "score": getattr(result, "score", 0.0),
                })
            elif isinstance(result, dict) and result.get("fact"):
                raw_facts.append(result)

        if not raw_facts:
            logger.info(
                "[SIGNAL_EXTRACTOR] no_facts user_id=%s",
                user_id[:8] if user_id else "?",
            )
            return []

        # Also get user summary for context
        zep_summary = ""
        try:
            context_result = await zep.get_user_context(user_id)
            if context_result:
                zep_summary = context_result.get("context", "")
        except Exception:
            pass  # Summary is optional

        # Sort facts by recency - prioritize last 3 days
        recent_facts, older_facts = _sort_facts_by_recency(raw_facts, recent_days=3)

        logger.info(
            "[SIGNAL_EXTRACTOR] facts user_id=%s recent=%d older=%d",
            user_id[:8] if user_id else "?",
            len(recent_facts),
            len(older_facts),
        )

        # Build context for LLM
        openai = AzureOpenAIClient()

        today = datetime.now()
        today_formatted = today.strftime("%A, %B %d, %Y")

        context_parts = []

        # Add today's date prominently for temporal reasoning
        context_parts.append(f"## TODAY'S DATE: {today_formatted}")

        if recent_facts:
            context_parts.append(
                "## Recent Activity (Last 3 Days) - PRIORITIZE THESE:\n"
                + "\n".join(f"- {f}" for f in recent_facts[:15])
            )

        if older_facts:
            context_parts.append(
                "## Older Context (for reference only):\n"
                + "\n".join(f"- {f}" for f in older_facts[:10])
            )

        if zep_summary:
            context_parts.append(f"## User Summary:\n{zep_summary}")

        # Add user profile context
        profile_context = _build_user_context(user_profile)
        if profile_context and profile_context != "No profile context available":
            context_parts.append(f"## User Profile:\n{profile_context}")

        user_context = "\n\n".join(context_parts)

        user_prompt = f"""Based on this user's recent email activity, identify the top {max_signals} networking signals.

{user_context}

What SPECIFIC, ACTIONABLE networking opportunities can you identify from their recent emails?
Focus on events, deadlines, projects, or activities where connecting with the right person would help."""

        response = await openai.generate_response(
            system_prompt=ZEP_SIGNAL_EXTRACTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=800,
            trace_label="proactive_signal_extraction",
        )

        # Parse JSON response
        signals = _parse_signal_response(response, max_signals)

        logger.info(
            "[SIGNAL_EXTRACTOR] extracted user_id=%s signals=%d",
            user_id[:8] if user_id else "?",
            len(signals),
        )

        # Sync signals to Zep graph (background, non-blocking)
        if signals:
            await _sync_signals_to_zep_background(user_id, signals)

        return signals

    except Exception as e:
        logger.error(
            "[SIGNAL_EXTRACTOR] failed user_id=%s error=%s",
            user_id[:8] if user_id else "?",
            str(e),
            exc_info=True,
        )
        return []


def _parse_signal_response(
    response: str,
    max_signals: int,
) -> List[Dict[str, Any]]:
    """Parse LLM response into signal dicts."""
    # Clean JSON from markdown code blocks
    cleaned = response.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning(
            "[SIGNAL_EXTRACTOR] json_parse_error error=%s response=%s",
            str(e),
            cleaned[:200],
        )
        return []

    signals = result.get("signals") or []
    if not signals:
        skip_reason = result.get("skip_reason")
        if skip_reason:
            logger.info("[SIGNAL_EXTRACTOR] skipped reason=%s", skip_reason)
        return []

    # Process each signal
    processed = []
    for i, s in enumerate(signals[:max_signals]):
        signal_text = s.get("signal_text") or ""
        if not signal_text:
            continue

        # Get match type (default to single)
        match_type = s.get("match_type", "single")
        if match_type not in ("single", "multi"):
            match_type = "single"

        max_matches = s.get("max_matches", 1)
        if match_type == "single":
            max_matches = 1
        elif max_matches < 2:
            max_matches = 3
        elif max_matches > 5:
            max_matches = 5

        processed.append({
            "signal_text": signal_text.strip(),
            "group_name": s.get("group_name", ""),
            "signal_rank": s.get("signal_rank") or (i + 1),
            "urgency_score": float(s.get("urgency_score") or 0.5),
            "relevance_score": float(s.get("relevance_score") or 0.5),
            "extraction_reasoning": s.get("extraction_reasoning") or "",
            "match_type": match_type,
            "max_matches": max_matches,
        })

    return processed
