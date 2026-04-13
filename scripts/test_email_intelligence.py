#!/usr/bin/env python3
"""
Test script for Email Intelligence Pipeline.

Run: python -m scripts.test_email_intelligence [--user-id <uuid>]

This script:
1. Finds a user with Composio email connection (or uses provided user_id)
2. Fetches their recent emails
3. Runs the LLM analysis
4. Shows the results (without saving to DB by default)
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.integrations.composio_client import ComposioClient
from app.email_intelligence.prompts import EMAIL_INTELLIGENCE_SYSTEM_PROMPT
from app.email_intelligence.utils import (
    build_email_analysis_prompt,
    build_incremental_query,
    filter_actionable_emails,
    get_max_email_date,
    parse_llm_response,
)
from app.utils.demand_value_history import normalize_history

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def find_test_user(db: DatabaseClient) -> dict | None:
    """Find a user who has connected their email via Composio."""
    try:
        result = (
            db.client.table("users")
            .select("id,phone_number,name,email,demand_history,value_history,all_demand,all_value,metadata")
            .eq("is_onboarded", True)
            .not_.is_("email", "null")
            .limit(10)
            .execute()
        )

        if not result.data:
            logger.warning("No users with email found")
            return None

        # Return first user with email
        for user in result.data:
            if user.get("email"):
                logger.info(f"Found test user: {user.get('name')} ({user.get('email')})")
                return user

        return None
    except Exception as e:
        logger.error(f"Error finding test user: {e}")
        return None


async def test_email_intelligence(user_id: str | None = None, save_to_db: bool = False):
    """Run the email intelligence pipeline for a test user."""

    db = DatabaseClient()
    openai = AzureOpenAIClient()
    composio = ComposioClient()

    print("\n" + "="*60)
    print("EMAIL INTELLIGENCE PIPELINE TEST")
    print("="*60)

    # Step 1: Get test user
    if user_id:
        user = await db.get_user_by_id(user_id)
        if not user:
            print(f"\n❌ User {user_id} not found")
            return
    else:
        user = await find_test_user(db)
        if not user:
            print("\n❌ No test user found with email connection")
            return

    user_id = str(user["id"])
    print(f"\n📧 Test User:")
    print(f"   ID: {user_id[:8]}...")
    print(f"   Name: {user.get('name')}")
    print(f"   Email: {user.get('email')}")

    # Step 2: Check Composio connection
    print("\n🔗 Checking Composio connection...")
    try:
        connected_account_id = await asyncio.wait_for(
            composio.get_connected_account_id(user_id=user_id),
            timeout=15.0,
        )
        if not connected_account_id:
            print("   ❌ No Composio connected account found")
            print("   User needs to connect their email first via OAuth")
            return
        print(f"   ✅ Connected account: {connected_account_id[:8]}...")
    except Exception as e:
        print(f"   ❌ Composio error: {e}")
        return

    # Step 3: Fetch emails
    print("\n📬 Fetching emails from Composio...")
    metadata = user.get("metadata") if isinstance(user.get("metadata"), dict) else {}
    email_intel = metadata.get("email_intelligence", {}) if isinstance(metadata.get("email_intelligence"), dict) else {}
    last_email_date = email_intel.get("last_email_date")

    query = build_incremental_query(last_email_date)
    print(f"   Query: {query}")

    try:
        emails = await asyncio.wait_for(
            composio.fetch_recent_threads(
                user_id=user_id,
                connected_account_id=connected_account_id,
                query=query,
                limit=10,
            ),
            timeout=30.0,
        )
        print(f"   ✅ Fetched {len(emails)} emails")
    except Exception as e:
        print(f"   ❌ Fetch error: {e}")
        return

    if not emails:
        print("   No new emails found")
        return

    # Step 4: Filter actionable emails
    print("\n🔍 Filtering actionable emails...")
    actionable = filter_actionable_emails(emails)
    print(f"   ✅ {len(actionable)} actionable emails (from {len(emails)} total)")

    if not actionable:
        print("   No actionable emails after filtering")
        return

    # Show email summaries
    print("\n📋 Email Summaries:")
    for i, email in enumerate(actionable[:5], 1):
        sender = email.get("sender") or email.get("from") or "unknown"
        subject = email.get("subject") or "no subject"
        print(f"   {i}. From: {sender[:40]}")
        print(f"      Subject: {subject[:50]}")

    # Step 5: Get current state
    print("\n📊 Current demand/value state:")
    current_state = await db.get_demand_value_state(user_id)

    all_demand_raw = current_state.get("all_demand")
    all_value_raw = current_state.get("all_value")

    # Convert to list if string
    if isinstance(all_demand_raw, str) and all_demand_raw:
        all_demand = [item.strip() for item in all_demand_raw.split("\n") if item.strip()]
    elif isinstance(all_demand_raw, list):
        all_demand = all_demand_raw
    else:
        all_demand = []

    if isinstance(all_value_raw, str) and all_value_raw:
        all_value = [item.strip() for item in all_value_raw.split("\n") if item.strip()]
    elif isinstance(all_value_raw, list):
        all_value = all_value_raw
    else:
        all_value = []

    print(f"   Current demands: {all_demand or 'None'}")
    print(f"   Current values: {all_value or 'None'}")

    demand_history = normalize_history(current_state.get("demand_history"))
    value_history = normalize_history(current_state.get("value_history"))

    # Step 6: Build prompt and call LLM
    print("\n🤖 Calling LLM for analysis...")

    user_prompt = build_email_analysis_prompt(
        emails=actionable,
        all_demand=all_demand,
        all_value=all_value,
        recent_demand_history=demand_history[-10:],
        recent_value_history=value_history[-10:],
    )

    print(f"   Prompt length: {len(user_prompt)} chars")

    try:
        response = await openai.generate_response(
            system_prompt=EMAIL_INTELLIGENCE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=getattr(settings, "email_intelligence_model", "gpt-4o-mini"),
            temperature=0.3,
            max_tokens=1500,
            trace_label="email_intelligence_test",
        )
        print(f"   ✅ LLM response received ({len(response)} chars)")
    except Exception as e:
        print(f"   ❌ LLM error: {e}")
        return

    # Step 7: Parse response
    print("\n📝 Parsing LLM response...")
    analysis = parse_llm_response(response)

    if not analysis:
        print("   ❌ Failed to parse JSON response")
        print(f"   Raw response: {response[:500]}...")
        return

    print("   ✅ JSON parsed successfully")

    # Step 8: Show results
    print("\n" + "="*60)
    print("ANALYSIS RESULTS")
    print("="*60)

    print(f"\n📌 Latest Demand: {analysis.get('latest_demand') or 'None'}")
    print(f"📌 Latest Value: {analysis.get('latest_value') or 'None'}")

    print(f"\n📋 Final All Demands:")
    for d in (analysis.get("final_all_demand") or []):
        print(f"   • {d}")

    print(f"\n📋 Final All Values:")
    for v in (analysis.get("final_all_value") or []):
        print(f"   • {v}")

    print(f"\n🆕 New Demand Items:")
    for item in (analysis.get("new_demand_items") or []):
        text = item.get("text") if isinstance(item, dict) else item
        reason = item.get("reason", "") if isinstance(item, dict) else ""
        print(f"   • {text}")
        if reason:
            print(f"     Reason: {reason}")

    print(f"\n🆕 New Value Items:")
    for item in (analysis.get("new_value_items") or []):
        text = item.get("text") if isinstance(item, dict) else item
        reason = item.get("reason", "") if isinstance(item, dict) else ""
        print(f"   • {text}")
        if reason:
            print(f"     Reason: {reason}")

    print(f"\n🗑️ Removed Items: {analysis.get('removed_items') or 'None'}")
    print(f"\n💭 Reasoning: {analysis.get('reasoning') or 'None'}")

    # Step 9: Optionally save to DB
    if save_to_db:
        print("\n💾 Saving to database...")
        from app.email_intelligence.worker import EmailIntelligenceWorker

        worker = EmailIntelligenceWorker(
            db=db,
            openai=openai,
            composio=composio,
        )
        await worker._apply_updates(user_id, analysis, current_state)

        max_date = get_max_email_date(actionable)
        await worker._mark_processed(
            user_id,
            success=True,
            reason="test_updated",
            last_email_date=max_date,
            emails_processed=len(actionable),
        )
        print("   ✅ Saved to database!")
    else:
        print("\n⚠️  Results NOT saved (use --save to persist)")

    print("\n" + "="*60)
    print("TEST COMPLETE")
    print("="*60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Test Email Intelligence Pipeline")
    parser.add_argument(
        "--user-id",
        type=str,
        help="Specific user UUID to test (otherwise finds first user with email)",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save results to database (default: dry run)",
    )

    args = parser.parse_args()

    asyncio.run(test_email_intelligence(
        user_id=args.user_id,
        save_to_db=args.save,
    ))


if __name__ == "__main__":
    main()
