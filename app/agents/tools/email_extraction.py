"""
Email extraction tool for fetching and storing user emails.

Provides a standalone, production-ready function to extract emails for a given user
with configurable time period and email count, storing them to the user_emails table.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EmailExtractionResult:
    """Result from email extraction operation."""

    success: bool
    total_fetched: int = 0
    total_stored: int = 0
    duplicates_skipped: int = 0
    sensitive_filtered: int = 0
    received_count: int = 0
    sent_count: int = 0
    error: Optional[str] = None
    error_code: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


def _build_gmail_query(
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    raw_query: Optional[str],
    is_sent: bool = False,
) -> str:
    """
    Build Gmail query from datetime objects or raw query.

    Gmail query format:
        - after:YYYY/MM/DD (inclusive)
        - before:YYYY/MM/DD (exclusive)
        - newer_than:Xd (relative)
        - in:sent (sent folder)

    Args:
        start_date: Start of range (inclusive)
        end_date: End of range (exclusive)
        raw_query: Raw query override
        is_sent: Whether to add "in:sent" prefix

    Returns:
        Gmail query string
    """
    sent_prefix = "in:sent " if is_sent else ""

    # Raw query takes precedence
    if raw_query:
        # If raw_query already has "in:sent", don't duplicate
        if is_sent and "in:sent" in raw_query.lower():
            return raw_query
        return f"{sent_prefix}{raw_query}" if is_sent else raw_query

    # Build from datetime objects
    query_parts = []

    if start_date:
        # Gmail uses YYYY/MM/DD format for after/before
        query_parts.append(f"after:{start_date.strftime('%Y/%m/%d')}")

    if end_date:
        query_parts.append(f"before:{end_date.strftime('%Y/%m/%d')}")

    # Default fallback if no dates provided
    if not query_parts:
        query_parts.append("newer_than:90d")

    base_query = " ".join(query_parts)
    return f"{sent_prefix}{base_query}"


async def _fetch_emails_with_processing(
    *,
    user_id: str,
    connected_account_id: str,
    composio: Any,
    query: str,
    limit: int,
    is_sent: bool,
) -> List[Dict[str, Any]]:
    """
    Fetch emails from Composio and process into storage format.

    Uses build_email_signals from email_context to process raw threads
    into the structured email format expected by store_user_emails.

    Args:
        user_id: User ID for Composio
        connected_account_id: Composio connected account ID
        composio: ComposioClient instance
        query: Gmail search query
        limit: Maximum emails to fetch
        is_sent: Whether these are sent emails

    Returns:
        List of processed email dictionaries ready for storage
    """
    from app.agents.tools.onboarding.email_context import build_email_signals

    try:
        # Fetch raw threads from Composio
        threads = await composio.fetch_recent_threads(
            user_id=user_id,
            connected_account_id=connected_account_id,
            query=query,
            limit=limit,
        )

        if not threads:
            logger.debug(
                "[EMAIL_EXTRACTION] No threads returned for query=%s limit=%d is_sent=%s",
                query,
                limit,
                is_sent,
            )
            return []

        # Mark threads with is_sent flag before processing
        for thread in threads:
            thread["is_sent"] = is_sent

        # Process through build_email_signals to get structured format
        # This handles: subject/body extraction, PII scrubbing, sensitive filtering
        signals = build_email_signals(
            threads=list(threads),
            query=query,
            max_evidence=limit * 2,  # Allow headroom for filtering
            updated_at=datetime.utcnow().isoformat(),
        )

        emails = signals.get("emails", [])

        # Ensure is_sent flag is correctly set on all emails
        for email in emails:
            email["is_sent"] = is_sent

        logger.debug(
            "[EMAIL_EXTRACTION] Processed %d threads into %d emails for query=%s",
            len(threads),
            len(emails),
            query,
        )

        return emails[:limit]

    except Exception as e:
        logger.warning(
            "[EMAIL_EXTRACTION] Failed to fetch emails query=%s: %s",
            query,
            e,
            exc_info=True,
        )
        return []


async def extract_and_store_emails(
    *,
    user_id: str,
    connected_account_id: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    raw_query: Optional[str] = None,
    include_sent: bool = True,
    max_received: int = 100,
    max_sent: int = 50,
    timeout_seconds: float = 60.0,
    skip_zep_sync: bool = False,
) -> EmailExtractionResult:
    """
    Extract emails from Gmail and store to user_emails table.

    Time Period Options (mutually exclusive):
        1. start_date + end_date: Fetch emails within date range
        2. start_date only: Fetch emails from start_date to now
        3. end_date only: Fetch emails before end_date (up to 90 days back)
        4. raw_query: Use exact Gmail query (overrides date params)
        5. None: Default to "newer_than:90d"

    Args:
        user_id: User ID for Composio and database
        connected_account_id: Optional pre-resolved Composio account ID
        start_date: Start of date range (inclusive)
        end_date: End of date range (exclusive)
        raw_query: Raw Gmail query override (e.g., "newer_than:30d", "label:important")
        include_sent: Whether to fetch sent emails (default: True)
        max_received: Maximum received emails to fetch (default: 100)
        max_sent: Maximum sent emails to fetch (default: 50)
        timeout_seconds: Total timeout for fetch operations (default: 60.0)
        skip_zep_sync: If True, skip background Zep sync (use when doing incremental sync separately)

    Returns:
        EmailExtractionResult with counts and status
    """
    from app.database.client import DatabaseClient
    from app.integrations.composio_client import ComposioClient

    start_time = time.monotonic()

    # === STEP 1: Input Validation ===
    if not user_id or not isinstance(user_id, str) or not user_id.strip():
        return EmailExtractionResult(
            success=False,
            error="user_id is required and must be a non-empty string",
            error_code="MISSING_USER_ID",
        )

    user_id = user_id.strip()

    # Validate date inputs
    if raw_query and (start_date or end_date):
        logger.warning(
            "[EMAIL_EXTRACTION] raw_query provided with date params; raw_query takes precedence"
        )

    if start_date and end_date and start_date >= end_date:
        return EmailExtractionResult(
            success=False,
            error="start_date must be before end_date",
            error_code="INVALID_DATE_RANGE",
        )

    # Validate limits
    if max_received < 0:
        max_received = 0
    if max_sent < 0:
        max_sent = 0

    # === STEP 2: Build Gmail Queries ===
    received_query = _build_gmail_query(start_date, end_date, raw_query, is_sent=False)
    sent_query = (
        _build_gmail_query(start_date, end_date, raw_query, is_sent=True)
        if include_sent and max_sent > 0
        else None
    )

    queries_used = {"received": received_query, "sent": sent_query}
    logger.info(
        "[EMAIL_EXTRACTION] Starting extraction for user=%s received_query=%s sent_query=%s",
        user_id[:8] if len(user_id) >= 8 else user_id,
        received_query,
        sent_query,
    )

    # === STEP 3: Initialize Composio Client ===
    composio = ComposioClient()
    if not composio.is_available():
        return EmailExtractionResult(
            success=False,
            error="Composio client unavailable - check API key configuration",
            error_code="COMPOSIO_UNAVAILABLE",
            metadata={"queries_used": queries_used},
        )

    # === STEP 4: Resolve Connected Account ===
    account_id = connected_account_id
    if not account_id:
        try:
            account_id = await asyncio.wait_for(
                composio.get_connected_account_id(user_id=user_id),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[EMAIL_EXTRACTION] Timeout getting connected account for user=%s",
                user_id[:8] if len(user_id) >= 8 else user_id,
            )
            return EmailExtractionResult(
                success=False,
                error="Timeout while looking up Gmail connection",
                error_code="CONNECTION_LOOKUP_TIMEOUT",
                metadata={"queries_used": queries_used},
            )
        except Exception as e:
            logger.warning(
                "[EMAIL_EXTRACTION] Failed to get connected account for user=%s: %s",
                user_id[:8] if len(user_id) >= 8 else user_id,
                e,
            )
            return EmailExtractionResult(
                success=False,
                error=f"Failed to get Gmail connection: {str(e)}",
                error_code="CONNECTION_LOOKUP_FAILED",
                metadata={"queries_used": queries_used},
            )

    if not account_id:
        return EmailExtractionResult(
            success=False,
            error="No connected Gmail account found for this user",
            error_code="NO_CONNECTED_ACCOUNT",
            metadata={"queries_used": queries_used},
        )

    # === STEP 5: Fetch Emails (parallel for received + sent) ===
    received_emails: List[Dict[str, Any]] = []
    sent_emails: List[Dict[str, Any]] = []
    fetch_errors: List[str] = []

    try:
        tasks = []

        # Only fetch received if max_received > 0
        if max_received > 0:
            tasks.append(
                _fetch_emails_with_processing(
                    user_id=user_id,
                    connected_account_id=account_id,
                    composio=composio,
                    query=received_query,
                    limit=max_received,
                    is_sent=False,
                )
            )

        # Only fetch sent if include_sent and max_sent > 0
        if include_sent and max_sent > 0 and sent_query:
            tasks.append(
                _fetch_emails_with_processing(
                    user_id=user_id,
                    connected_account_id=account_id,
                    composio=composio,
                    query=sent_query,
                    limit=max_sent,
                    is_sent=True,
                )
            )

        if not tasks:
            # No fetch tasks needed (both limits are 0)
            return EmailExtractionResult(
                success=True,
                total_fetched=0,
                total_stored=0,
                metadata={"queries_used": queries_used, "note": "No emails requested (limits are 0)"},
            )

        results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=timeout_seconds,
        )

        # Process results
        result_idx = 0
        if max_received > 0:
            if isinstance(results[result_idx], Exception):
                logger.error(
                    "[EMAIL_EXTRACTION] Received emails fetch failed: %s",
                    results[result_idx],
                )
                fetch_errors.append(f"received: {str(results[result_idx])}")
            else:
                received_emails = results[result_idx] or []
            result_idx += 1

        if include_sent and max_sent > 0 and sent_query and result_idx < len(results):
            if isinstance(results[result_idx], Exception):
                logger.error(
                    "[EMAIL_EXTRACTION] Sent emails fetch failed: %s",
                    results[result_idx],
                )
                fetch_errors.append(f"sent: {str(results[result_idx])}")
            else:
                sent_emails = results[result_idx] or []

    except asyncio.TimeoutError:
        logger.error(
            "[EMAIL_EXTRACTION] Fetch timeout after %.1fs for user=%s",
            timeout_seconds,
            user_id[:8] if len(user_id) >= 8 else user_id,
        )
        return EmailExtractionResult(
            success=False,
            error=f"Email fetch timed out after {timeout_seconds}s",
            error_code="FETCH_TIMEOUT",
            metadata={"queries_used": queries_used},
        )

    # === STEP 6: Deduplicate by message_id ===
    all_emails: List[Dict[str, Any]] = []
    seen_ids: set = set()
    duplicate_count = 0

    # Process received first, then sent
    for email in received_emails + sent_emails:
        msg_id = email.get("message_id")
        if not msg_id:
            # Skip emails without message_id (can't deduplicate)
            continue
        if msg_id in seen_ids:
            duplicate_count += 1
            continue
        seen_ids.add(msg_id)
        all_emails.append(email)

    total_fetched = len(all_emails)
    received_fetched = sum(1 for e in all_emails if not e.get("is_sent", False))
    sent_fetched = sum(1 for e in all_emails if e.get("is_sent", False))

    logger.info(
        "[EMAIL_EXTRACTION] Fetched %d emails (%d received + %d sent) for user=%s, %d duplicates removed",
        total_fetched,
        received_fetched,
        sent_fetched,
        user_id[:8] if len(user_id) >= 8 else user_id,
        duplicate_count,
    )

    if not all_emails:
        # No emails to store - this is still a success
        return EmailExtractionResult(
            success=True,
            total_fetched=0,
            total_stored=0,
            duplicates_skipped=duplicate_count,
            metadata={
                "queries_used": queries_used,
                "fetch_errors": fetch_errors if fetch_errors else None,
            },
        )

    # === STEP 7: Store to Database ===
    db = DatabaseClient()
    stored_count = 0

    try:
        stored_result = await db.store_user_emails(user_id=user_id, emails=all_emails)
        # store_user_emails returns the list of stored rows
        stored_count = len(stored_result) if isinstance(stored_result, list) else 0
    except Exception as e:
        logger.error(
            "[EMAIL_EXTRACTION] Database store failed for user=%s: %s",
            user_id[:8] if len(user_id) >= 8 else user_id,
            e,
            exc_info=True,
        )
        return EmailExtractionResult(
            success=False,
            total_fetched=total_fetched,
            received_count=received_fetched,
            sent_count=sent_fetched,
            error=f"Database storage failed: {str(e)}",
            error_code="STORE_FAILED",
            metadata={"queries_used": queries_used},
        )

    # === STEP 8: Sync to Zep Graph (background, non-blocking) ===
    # Note: When called from DailyEmailService, skip_zep_sync=True and
    # incremental sync is handled separately via sync_unsynced_emails_to_zep
    if not skip_zep_sync:
        try:
            from app.config import settings
            if settings.zep_graph_enabled and settings.zep_graph_sync_emails and stored_count > 0:
                from app.agents.tools.email_zep_sync import sync_emails_to_zep

                asyncio.create_task(
                    _sync_to_zep_background(user_id, all_emails)
                )
                logger.debug(
                    "[EMAIL_EXTRACTION] Initiated Zep sync for user=%s emails=%d",
                    user_id[:8] if len(user_id) >= 8 else user_id,
                    len(all_emails),
                )
        except Exception as e:
            logger.warning(
                "[EMAIL_EXTRACTION] Failed to initiate Zep sync: %s",
                e,
            )

    # === STEP 9: Calculate final counts ===
    # Duplicates skipped = fetched - stored (some may have been in DB already)
    db_duplicates = total_fetched - stored_count
    total_duplicates = duplicate_count + db_duplicates

    # Count stored by type (approximate - based on order)
    received_stored = min(received_fetched, stored_count)
    sent_stored = max(0, stored_count - received_stored)

    duration_ms = int((time.monotonic() - start_time) * 1000)

    logger.info(
        "[EMAIL_EXTRACTION] Completed for user=%s: fetched=%d stored=%d duplicates=%d duration=%dms",
        user_id[:8] if len(user_id) >= 8 else user_id,
        total_fetched,
        stored_count,
        total_duplicates,
        duration_ms,
    )

    return EmailExtractionResult(
        success=True,
        total_fetched=total_fetched,
        total_stored=stored_count,
        duplicates_skipped=total_duplicates,
        sensitive_filtered=0,  # Filtering happens in build_email_signals
        received_count=received_stored,
        sent_count=sent_stored,
        metadata={
            "queries_used": queries_used,
            "duration_ms": duration_ms,
            "fetch_errors": fetch_errors if fetch_errors else None,
        },
    )


async def _sync_to_zep_background(
    user_id: str,
    emails: List[Dict[str, Any]],
) -> None:
    """
    Background task to sync emails to Zep graph.

    This runs asynchronously and doesn't block the main extraction flow.
    Errors are logged but don't affect the extraction result.
    """
    try:
        from app.agents.tools.email_zep_sync import sync_emails_to_zep

        result = await sync_emails_to_zep(user_id=user_id, emails=emails)

        if result["success"]:
            logger.info(
                "[EMAIL_EXTRACTION] Zep sync completed user=%s synced=%d/%d",
                user_id[:8] if len(user_id) >= 8 else user_id,
                result.get("emails_synced", 0),
                len(emails),
            )
        else:
            logger.warning(
                "[EMAIL_EXTRACTION] Zep sync partial failure user=%s errors=%s",
                user_id[:8] if len(user_id) >= 8 else user_id,
                result.get("errors", [])[:3],
            )
    except Exception as e:
        logger.error(
            "[EMAIL_EXTRACTION] Zep sync background task failed user=%s: %s",
            user_id[:8] if len(user_id) >= 8 else user_id,
            e,
        )
