#!/usr/bin/env python3
"""
Migrate existing user emails from Supabase to Zep knowledge graph.

This script:
1. Fetches all unique user_ids from user_emails table
2. For each user, retrieves their emails from Supabase
3. Syncs emails to Zep using the existing sync_emails_to_zep function

Usage:
    python scripts/migrate_emails_to_zep.py

    # Dry run (just count, don't sync):
    python scripts/migrate_emails_to_zep.py --dry-run

    # Limit to specific number of users:
    python scripts/migrate_emails_to_zep.py --limit 10

    # Skip first N users (for resuming):
    python scripts/migrate_emails_to_zep.py --offset 50
"""

import asyncio
import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def get_users_with_emails() -> List[Dict[str, Any]]:
    """Get all unique users who have emails in the database."""
    from app.database.client import DatabaseClient

    db = DatabaseClient()

    # Query distinct user_ids from user_emails
    # Note: Supabase doesn't support COUNT(*) GROUP BY easily via REST API
    # So we fetch all user_ids and count locally
    logger.info("Fetching user_ids from user_emails table...")

    all_user_ids: List[str] = []
    page_size = 1000
    offset = 0

    while True:
        result = (
            db.client.table("user_emails")
            .select("user_id")
            .range(offset, offset + page_size - 1)
            .execute()
        )

        if not result.data:
            break

        for row in result.data:
            uid = row.get("user_id")
            if uid:
                all_user_ids.append(uid)

        if len(result.data) < page_size:
            break

        offset += page_size
        logger.info(f"  Fetched {offset} records...")

    if not all_user_ids:
        return []

    # Count emails per user
    user_counts: Dict[str, int] = {}
    for uid in all_user_ids:
        user_counts[uid] = user_counts.get(uid, 0) + 1

    return [{"user_id": uid, "email_count": count} for uid, count in user_counts.items()]


async def get_user_emails_for_migration(user_id: str, limit: int = 500, only_unsynced: bool = True) -> List[Dict[str, Any]]:
    """
    Get emails for a user from Supabase for migration.

    Args:
        user_id: User identifier
        limit: Max emails to fetch per user
        only_unsynced: If True, only fetch emails where zep_synced_at IS NULL

    Returns:
        List of email dictionaries (includes 'id' for marking as synced)
    """
    from app.database.client import DatabaseClient

    db = DatabaseClient()

    try:
        query = (
            db.client.table("user_emails")
            .select("id,sender,sender_domain,subject,body,snippet,received_at,is_sent")
            .eq("user_id", user_id)
            .eq("is_sensitive", False)
        )

        if only_unsynced:
            query = query.is_("zep_synced_at", "null")

        result = (
            query
            .order("received_at", desc=True)
            .limit(limit)
            .execute()
        )

        return list(result.data or [])
    except Exception as e:
        logger.error(f"Error fetching emails for user {user_id[:8]}...: {e}")
        return []


async def migrate_user_emails(user_id: str, emails: List[Dict[str, Any]], mark_synced: bool = True) -> Dict[str, Any]:
    """
    Migrate a single user's emails to Zep.

    Args:
        user_id: User identifier
        emails: List of email dictionaries (must include 'id' for mark_synced)
        mark_synced: If True, mark emails as synced in database after success

    Returns:
        Migration result dict
    """
    from app.agents.tools.email_zep_sync import sync_emails_to_zep

    return await sync_emails_to_zep(user_id=user_id, emails=emails, mark_synced=mark_synced)


async def main(
    dry_run: bool = False,
    limit: int = 0,
    offset: int = 0,
    batch_delay: float = 0.5,
    all_emails: bool = False,
):
    """
    Main migration function.

    Args:
        dry_run: If True, just count without syncing
        limit: Max number of users to process (0 = all)
        offset: Skip first N users
        batch_delay: Delay between users to avoid rate limiting
        all_emails: If True, sync ALL emails (not just unsynced). Use with caution.
    """
    print("=" * 70)
    print("Email Migration: Supabase -> Zep Knowledge Graph")
    print("=" * 70)
    print(f"Mode: {'DRY RUN (no sync)' if dry_run else 'MIGRATION'}")
    print(f"Sync mode: {'ALL emails' if all_emails else 'ONLY unsynced emails (incremental)'}")
    print(f"Offset: {offset}, Limit: {limit if limit > 0 else 'unlimited'}")
    print("-" * 70)

    # Check if Zep is configured
    from app.config import settings
    if not settings.zep_graph_enabled:
        print("ERROR: Zep graph is not enabled (ZEP_GRAPH_ENABLED=false)")
        sys.exit(1)

    if not settings.zep_api_key:
        print("ERROR: ZEP_API_KEY not configured")
        sys.exit(1)

    print(f"Zep endpoint: {settings.zep_base_url}")
    print("-" * 70)

    # Get users with emails
    print("Fetching users with emails from Supabase...")
    users = await get_users_with_emails()

    if not users:
        print("No users with emails found.")
        return

    total_users = len(users)
    print(f"Found {total_users} users with emails")

    # Apply offset and limit
    if offset > 0:
        users = users[offset:]
        print(f"Skipped first {offset} users")

    if limit > 0:
        users = users[:limit]
        print(f"Processing {len(users)} users (limited)")

    print("-" * 70)

    # Sort by email count descending
    users.sort(key=lambda x: x.get("email_count", 0), reverse=True)

    # Summary stats
    total_emails = sum(u.get("email_count", 0) for u in users)
    print(f"Total emails to migrate: {total_emails}")
    print("-" * 70)

    if dry_run:
        print("\nDRY RUN - Users with emails:")
        for i, user in enumerate(users[:20]):  # Show first 20
            uid = user.get("user_id", "?")
            count = user.get("email_count", 0)
            print(f"  {i+1}. {uid[:8]}... ({count} emails)")

        if len(users) > 20:
            print(f"  ... and {len(users) - 20} more users")

        print(f"\nDRY RUN: Would migrate {total_emails} emails for {len(users)} users")
        return

    # Confirm migration
    print(f"\nAbout to migrate {total_emails} emails for {len(users)} users to Zep.")
    confirm = input("Type 'MIGRATE' to confirm: ")
    if confirm != "MIGRATE":
        print("Aborted.")
        return

    # Migration loop
    print("\nStarting migration...")
    start_time = datetime.now()

    migrated_users = 0
    migrated_emails = 0
    failed_users = 0
    errors: List[str] = []

    for i, user in enumerate(users):
        user_id = user.get("user_id")
        expected_count = user.get("email_count", 0)

        print(f"\n[{i+1}/{len(users)}] User {user_id[:8]}... ({expected_count} emails)")

        try:
            # Fetch emails from Supabase (only unsynced by default)
            emails = await get_user_emails_for_migration(
                user_id,
                only_unsynced=not all_emails,
            )

            if not emails:
                print(f"  No {'unsynced ' if not all_emails else ''}emails found")
                continue

            print(f"  Fetched {len(emails)} {'unsynced ' if not all_emails else ''}emails")

            # Sync to Zep (and mark as synced in database)
            result = await migrate_user_emails(user_id, emails, mark_synced=True)

            if result.get("success"):
                synced = result.get("emails_synced", 0)
                chunks = result.get("chunks_sent", 0)
                print(f"  Synced {synced}/{len(emails)} emails ({chunks} chunks)")
                migrated_users += 1
                migrated_emails += synced
            else:
                err = result.get("errors", ["Unknown error"])
                print(f"  FAILED: {err[:2]}")
                failed_users += 1
                errors.append(f"{user_id[:8]}: {err[0] if err else 'Unknown'}")

        except Exception as e:
            print(f"  ERROR: {e}")
            failed_users += 1
            errors.append(f"{user_id[:8]}: {str(e)}")

        # Rate limiting delay
        if i < len(users) - 1:
            await asyncio.sleep(batch_delay)

    # Summary
    duration = (datetime.now() - start_time).total_seconds()
    print("\n" + "=" * 70)
    print("MIGRATION COMPLETE")
    print("=" * 70)
    print(f"Duration: {duration:.1f} seconds")
    print(f"Users migrated: {migrated_users}/{len(users)}")
    print(f"Emails synced: {migrated_emails}/{total_emails}")
    print(f"Failed users: {failed_users}")

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for err in errors[:10]:
            print(f"  - {err}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate emails from Supabase to Zep")
    parser.add_argument("--dry-run", action="store_true", help="List users without migrating")
    parser.add_argument("--limit", type=int, default=0, help="Max users to process (0=all)")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N users")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between users (seconds)")
    parser.add_argument("--all", action="store_true", dest="all_emails", help="Sync ALL emails (not just unsynced)")
    args = parser.parse_args()

    asyncio.run(main(
        dry_run=args.dry_run,
        limit=args.limit,
        offset=args.offset,
        batch_delay=args.delay,
        all_emails=args.all_emails,
    ))
