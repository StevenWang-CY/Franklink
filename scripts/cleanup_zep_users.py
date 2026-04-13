#!/usr/bin/env python3
"""
Delete all Zep users to start fresh.

This script:
1. Lists all users from Zep API
2. Deletes each user and their associated data (threads, graph, etc.)

Usage:
    python scripts/cleanup_zep_users.py

    # Dry run (just list, don't delete):
    python scripts/cleanup_zep_users.py --dry-run
"""

import asyncio
import argparse
import httpx
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


async def get_all_users(client: httpx.AsyncClient) -> list[dict]:
    """Fetch all users from Zep API."""
    try:
        response = await client.get("/api/v2/users")
        if response.status_code == 200:
            data = response.json()
            # Handle both array and object with users key
            if isinstance(data, list):
                return data
            return data.get("users", [])
        else:
            print(f"Failed to list users: {response.status_code} - {response.text[:200]}")
            return []
    except Exception as e:
        print(f"Error listing users: {e}")
        return []


async def delete_user(client: httpx.AsyncClient, user_id: str) -> bool:
    """Delete a single user."""
    try:
        response = await client.delete(f"/api/v2/users/{user_id}")
        if response.status_code in [200, 204, 404]:
            return True
        else:
            print(f"  Failed to delete {user_id}: {response.status_code} - {response.text[:100]}")
            return False
    except Exception as e:
        print(f"  Error deleting {user_id}: {e}")
        return False


async def main(dry_run: bool = False):
    """Main cleanup function."""
    # Get API key from environment or settings
    api_key = os.environ.get("ZEP_API_KEY")
    base_url = os.environ.get("ZEP_BASE_URL", "https://api.getzep.com")

    if not api_key:
        # Try loading from app config
        try:
            from app.config import settings
            api_key = settings.zep_api_key
            base_url = settings.zep_base_url
        except Exception:
            pass

    if not api_key:
        print("ERROR: ZEP_API_KEY not found in environment or settings")
        sys.exit(1)

    print(f"Connecting to Zep at: {base_url}")
    print(f"Mode: {'DRY RUN (no deletions)' if dry_run else 'DELETE MODE'}")
    print("-" * 60)

    headers = {
        "Authorization": f"Api-Key {api_key}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30.0) as client:
        # Get all users
        print("Fetching all users...")
        users = await get_all_users(client)

        if not users:
            print("No users found.")
            return

        print(f"Found {len(users)} users")
        print("-" * 60)

        # List users
        for user in users:
            user_id = user.get("user_id") or user.get("userId") or str(user)
            created = user.get("created_at", "")[:19] if user.get("created_at") else ""
            print(f"  - {user_id} (created: {created})")

        print("-" * 60)

        if dry_run:
            print(f"DRY RUN: Would delete {len(users)} users")
            return

        # Confirm deletion
        print(f"\nAbout to delete {len(users)} users. This cannot be undone!")
        confirm = input("Type 'DELETE' to confirm: ")
        if confirm != "DELETE":
            print("Aborted.")
            return

        # Delete users
        print("\nDeleting users...")
        deleted = 0
        failed = 0

        for user in users:
            user_id = user.get("user_id") or user.get("userId") or str(user)
            print(f"  Deleting {user_id}...", end=" ")
            if await delete_user(client, user_id):
                print("OK")
                deleted += 1
            else:
                print("FAILED")
                failed += 1

            # Small delay to avoid rate limiting
            await asyncio.sleep(0.1)

        print("-" * 60)
        print(f"Completed: {deleted} deleted, {failed} failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete all Zep users")
    parser.add_argument("--dry-run", action="store_true", help="List users without deleting")
    args = parser.parse_args()

    asyncio.run(main(dry_run=args.dry_run))
