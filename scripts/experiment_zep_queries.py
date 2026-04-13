#!/usr/bin/env python3
"""Experiment with different Zep search queries to find the best one for connection purposes.

This script tests various query strategies and compares:
1. The raw facts returned by each query
2. The LLM suggestions generated from those facts
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.integrations.zep_graph_client import get_zep_graph_client
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.database.client import DatabaseClient


# Different query strategies to test
QUERY_STRATEGIES = {
    "current": "emails events deadlines activities interests projects",

    "time_sensitive": (
        "deadline due date tomorrow this week next week upcoming "
        "RSVP register apply submit application interview"
    ),

    "new_optimized": (
        "deadline due tomorrow this week next week upcoming RSVP register "
        "event session meeting partner teammate collaborator opportunity "
        "project research study interview application"
    ),
}


async def test_query(zep, user_id: str, query_name: str, query: str) -> dict:
    """Test a single query and return results."""
    print(f"\n{'='*60}")
    print(f"Testing query: {query_name}")
    print(f"Query: {query[:80]}...")
    print(f"{'='*60}")

    # Search Zep graph
    raw_facts = await zep.search_graph(
        user_id=user_id,
        query=query,
        scope="edges",
        limit=50,
    )

    # Extract fact strings
    facts = []
    for f in raw_facts:
        if hasattr(f, "fact"):
            facts.append(f.fact)
        elif isinstance(f, dict) and f.get("fact"):
            facts.append(f["fact"])

    print(f"\nReturned {len(facts)} facts")

    # Show sample facts
    print("\nSample facts (first 10):")
    for i, fact in enumerate(facts[:10]):
        print(f"  {i+1}. {fact[:100]}{'...' if len(fact) > 100 else ''}")

    return {
        "query_name": query_name,
        "query": query,
        "num_facts": len(facts),
        "facts": facts,
    }


async def generate_suggestions_for_query(
    openai: AzureOpenAIClient,
    query_name: str,
    facts: list,
    user_profile: dict,
) -> dict:
    """Generate LLM suggestions from facts."""

    today = datetime.now()
    today_formatted = today.strftime("%A, %B %d, %Y")

    # Build context
    context_parts = [f"## TODAY'S DATE: {today_formatted}"]

    if facts:
        context_parts.append(
            "## User's Email Activity:\n" + "\n".join(f"- {f}" for f in facts[:20])
        )

    hobbies = user_profile.get("hobbies", [])
    if hobbies:
        context_parts.append(f"## Hobbies: {', '.join(hobbies[:5])}")

    user_context = "\n\n".join(context_parts)

    system_prompt = """You suggest SPECIFIC, ACTIONABLE connection purposes based on a user's recent emails.

## Your Role
You analyze a user's recent emails to identify CONCRETE opportunities where connecting with someone would help.

## CRITICAL: TIME-SENSITIVE PRIORITIZATION
The user context includes TODAY'S DATE. Use it to evaluate time-sensitivity:
1. Events in the NEXT 3 DAYS = HIGHEST PRIORITY
2. Events in the next 7 days = HIGH PRIORITY
3. Ongoing activities = MEDIUM PRIORITY
4. PAST EVENTS = AUTOMATICALLY REJECT

## What to Look For
- Academic: study partner, thesis reviewer, research collaborator
- Events: event companion, hackathon teammate, info session buddy
- Professional: mock interview partner, networking buddy, mentor
- Projects: co-founder, collaborator, teammate

## Output Format
Return JSON only:
{
    "suggestions": [
        {
            "purpose": "short, specific connection purpose",
            "evidence": "specific email/fact that triggered this",
            "reasoning": "why this connection would help",
            "activity_type": "event|academic|professional|project|social",
            "event_date": "YYYY-MM-DD or null",
            "urgency": "high|medium|low"
        }
    ],
    "quality_notes": "brief assessment of how actionable/specific the suggestions are"
}

Return maximum 3 suggestions. If no good opportunities, return empty suggestions with skip_reason."""

    user_prompt = f"""Based on this user's context, suggest specific connection opportunities.

{user_context}

What SPECIFIC, ACTIONABLE connections could help this user?"""

    try:
        response = await openai.generate_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=800,
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
        suggestions = result.get("suggestions", [])
        quality_notes = result.get("quality_notes", "")
        skip_reason = result.get("skip_reason", "")

        return {
            "query_name": query_name,
            "num_suggestions": len(suggestions),
            "suggestions": suggestions,
            "quality_notes": quality_notes,
            "skip_reason": skip_reason,
        }
    except Exception as e:
        return {
            "query_name": query_name,
            "error": str(e),
        }


async def main():
    # Test user ID
    user_id = "570c9fdc-7919-4722-bcc3-93e673302a1b"

    print("=" * 80)
    print("ZEP QUERY EXPERIMENT")
    print(f"User ID: {user_id}")
    print(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    # Initialize clients
    zep = get_zep_graph_client()
    openai = AzureOpenAIClient()
    db = DatabaseClient()

    # Get user profile
    user = await db.get_user_by_id(user_id)
    user_profile = user or {}
    print(f"\nUser: {user_profile.get('name', 'Unknown')}")

    # Test each query strategy
    query_results = []
    for query_name, query in QUERY_STRATEGIES.items():
        result = await test_query(zep, user_id, query_name, query)
        query_results.append(result)

    # Generate suggestions for each query
    print("\n" + "=" * 80)
    print("GENERATING LLM SUGGESTIONS FOR EACH QUERY")
    print("=" * 80)

    suggestion_results = []
    for qr in query_results:
        print(f"\n--- Generating suggestions for: {qr['query_name']} ---")
        result = await generate_suggestions_for_query(
            openai,
            qr["query_name"],
            qr["facts"],
            user_profile,
        )
        suggestion_results.append(result)

        if result.get("suggestions"):
            print(f"Generated {len(result['suggestions'])} suggestions:")
            for i, s in enumerate(result["suggestions"]):
                print(f"  {i+1}. {s.get('purpose', 'N/A')[:60]}...")
                print(f"     Urgency: {s.get('urgency', 'N/A')}, Type: {s.get('activity_type', 'N/A')}")
        elif result.get("skip_reason"):
            print(f"Skipped: {result['skip_reason']}")
        elif result.get("error"):
            print(f"Error: {result['error']}")

    # Summary comparison
    print("\n" + "=" * 80)
    print("SUMMARY COMPARISON")
    print("=" * 80)

    print(f"\n{'Query Name':<25} {'Facts':<8} {'Suggestions':<12} {'Quality'}")
    print("-" * 80)

    for qr, sr in zip(query_results, suggestion_results):
        quality = sr.get("quality_notes", sr.get("skip_reason", sr.get("error", "N/A")))[:30]
        print(f"{qr['query_name']:<25} {qr['num_facts']:<8} {sr.get('num_suggestions', 0):<12} {quality}")

    # Detailed output for best queries
    print("\n" + "=" * 80)
    print("DETAILED SUGGESTIONS BY QUERY")
    print("=" * 80)

    for sr in suggestion_results:
        if sr.get("suggestions"):
            print(f"\n### {sr['query_name']} ###")
            for i, s in enumerate(sr["suggestions"]):
                print(f"\n  Suggestion {i+1}:")
                print(f"    Purpose: {s.get('purpose', 'N/A')}")
                print(f"    Evidence: {s.get('evidence', 'N/A')[:80]}...")
                print(f"    Urgency: {s.get('urgency', 'N/A')}")
                print(f"    Type: {s.get('activity_type', 'N/A')}")
                print(f"    Date: {s.get('event_date', 'N/A')}")


if __name__ == "__main__":
    asyncio.run(main())
