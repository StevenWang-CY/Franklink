"""Email to Zep graph synchronization utilities.

Provides functions to sync emails from Supabase to Zep's knowledge graph
for semantic search and context retrieval.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Patterns indicating sensitive content that should NOT be synced to Zep
# These emails may contain PII, financial data, or medical information
SENSITIVE_SENDER_PATTERNS = [
    # Financial institutions
    "bank", "chase", "wellsfargo", "bankofamerica", "citi", "capitalone",
    "amex", "americanexpress", "discover", "paypal", "venmo", "zelle",
    "fidelity", "schwab", "vanguard", "etrade", "robinhood", "coinbase",
    "credit", "loan", "mortgage",
    # Medical/Healthcare
    "health", "medical", "hospital", "clinic", "pharmacy", "cvs", "walgreens",
    "doctor", "patient", "hipaa", "medicare", "medicaid", "insurance",
    "labcorp", "quest diagnostics", "myhealth", "mychart",
    # Government/Tax
    "irs", "turbotax", "hrblock", "taxact", "socialsecurity", "ssa.gov",
    # Identity/Security
    "equifax", "experian", "transunion", "lifelock", "identityguard",
]

SENSITIVE_SUBJECT_KEYWORDS = [
    # Financial
    "account statement", "bank statement", "credit score", "credit report",
    "payment due", "balance due", "transaction alert", "wire transfer",
    "direct deposit", "tax return", "tax refund", "w-2", "1099",
    "investment statement", "portfolio", "dividend",
    # Medical
    "test results", "lab results", "prescription", "appointment reminder",
    "medical record", "health record", "diagnosis", "treatment plan",
    "insurance claim", "eob", "explanation of benefits",
    # Security
    "password reset", "verify your identity", "security alert",
    "suspicious activity", "fraud alert", "account locked",
    "two-factor", "2fa", "verification code",
]

SENSITIVE_BODY_KEYWORDS = [
    # Financial identifiers
    "account number", "routing number", "ssn", "social security",
    "credit card", "debit card", "card ending in", "last 4 digits",
    "iban", "swift code", "bank account",
    # Medical terms
    "diagnosis", "prescription", "medication", "dosage", "blood test",
    "cholesterol", "glucose", "hemoglobin", "biopsy", "mri", "ct scan",
    "treatment", "prognosis", "symptoms",
    # Personal identifiers
    "date of birth", "dob", "driver's license", "passport number",
]


def _contains_sensitive_content(subject: str, body: str) -> bool:
    """
    Check if email contains sensitive content that should not be synced to Zep.

    This filters out:
    - Financial/banking emails (statements, transactions, tax docs)
    - Medical/healthcare emails (test results, prescriptions, appointments)
    - Security-related emails (password resets, verification codes)

    Args:
        subject: Email subject line
        body: Email body content

    Returns:
        True if email contains sensitive content and should be skipped
    """
    subject_lower = (subject or "").lower()
    body_lower = (body or "").lower()
    combined = f"{subject_lower} {body_lower}"

    # Check subject for sensitive keywords
    for keyword in SENSITIVE_SUBJECT_KEYWORDS:
        if keyword in subject_lower:
            return True

    # Check body for sensitive keywords
    for keyword in SENSITIVE_BODY_KEYWORDS:
        if keyword in body_lower:
            return True

    return False


def chunk_emails_for_zep(
    emails: List[Dict[str, Any]],
    max_chars: Optional[int] = None,
) -> List[List[Dict[str, Any]]]:
    """
    Chunk emails to respect Zep's 10,000 character limit per graph.add call.

    Args:
        emails: List of email dictionaries
        max_chars: Maximum characters per chunk (default from settings)

    Returns:
        List of email chunks, each within the character limit
    """
    if not emails:
        return []

    max_chars = max_chars or settings.zep_graph_chunk_size

    chunks: List[List[Dict[str, Any]]] = []
    current_chunk: List[Dict[str, Any]] = []
    current_size = 0

    for email in emails:
        email_text = format_single_email(email)
        email_size = len(email_text)

        if email_size > max_chars:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = []
                current_size = 0

            truncated = _truncate_email_for_size(email, max_chars - 100)
            chunks.append([truncated])
            continue

        if current_size + email_size > max_chars:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = [email]
            current_size = email_size
        else:
            current_chunk.append(email)
            current_size += email_size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def format_single_email(email: Dict[str, Any]) -> str:
    """
    Format a single email for Zep graph ingestion.

    Optimized for Zep's fact extraction:
    - Clear structure with labeled fields
    - PII already scrubbed (from build_email_signals)
    - Temporal markers for recency
    - Event dates extracted from content for time-sensitive retrieval

    Args:
        email: Email dictionary with sender, subject, body, etc.

    Returns:
        Formatted email string
    """
    from app.utils.event_date_extractor import extract_event_dates, format_event_dates_for_zep

    sender = email.get("sender") or email.get("sender_domain") or "unknown"
    subject = email.get("subject") or "(no subject)"
    body = email.get("body") or email.get("body_excerpt") or email.get("snippet") or ""
    received_at = email.get("received_at") or ""
    # Support both is_sent (user_emails) and is_from_me (user_email_highlights)
    is_sent = email.get("is_sent", False) or email.get("is_from_me", False)

    # Filter out sensitive content (medical/financial info) before syncing
    if _contains_sensitive_content(subject, body):
        return ""  # Skip this email entirely

    if len(body) > 500:
        body = body[:497] + "..."

    direction = "Sent" if is_sent else "Received"

    # Parse received_at for both display and as reference date for relative dates
    date_str = ""
    reference_date = None
    if received_at:
        try:
            if isinstance(received_at, str):
                dt = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
                date_str = dt.strftime("%Y-%m-%d")
                reference_date = dt.replace(tzinfo=None)
            elif isinstance(received_at, datetime):
                date_str = received_at.strftime("%Y-%m-%d")
                reference_date = received_at
        except (ValueError, TypeError):
            date_str = str(received_at)[:10] if received_at else ""

    # Extract event dates from subject + body for time-sensitive context
    combined_text = f"{subject} {body}"
    event_dates = []
    try:
        event_dates = extract_event_dates(combined_text, reference_date=reference_date)
    except Exception as e:
        logger.debug(f"Event date extraction failed: {e}")

    event_annotation = format_event_dates_for_zep(event_dates) if event_dates else ""

    lines = [
        f"Email ({direction}) from {sender}" + (f" on {date_str}" if date_str else "") + ":",
        f"Subject: {subject}",
    ]
    if body.strip():
        lines.append(f"Content: {body}")

    # Add event dates as structured annotation for Zep
    if event_annotation:
        lines.append(f"Event Dates Mentioned: {event_annotation}")

    lines.append("---")

    return "\n".join(lines)


def format_emails_for_graph(emails: List[Dict[str, Any]]) -> str:
    """
    Format multiple emails as text for Zep graph ingestion.

    Args:
        emails: List of email dictionaries

    Returns:
        Combined formatted email text (sensitive emails are filtered out)
    """
    if not emails:
        return ""

    # Filter out empty strings (sensitive emails that were skipped)
    formatted = [format_single_email(email) for email in emails]
    formatted = [f for f in formatted if f]  # Remove empty strings
    return "\n".join(formatted)


def _truncate_email_for_size(
    email: Dict[str, Any],
    max_chars: int,
) -> Dict[str, Any]:
    """
    Create a truncated copy of an email to fit within size limit.

    Args:
        email: Original email dictionary
        max_chars: Maximum characters for the formatted output

    Returns:
        Truncated email dictionary
    """
    truncated = {
        "sender": email.get("sender") or email.get("sender_domain") or "unknown",
        "subject": email.get("subject") or "(no subject)",
        "received_at": email.get("received_at"),
        "is_sent": email.get("is_sent", False),
    }

    header_size = len(format_single_email({**truncated, "body": ""}))
    available_for_body = max_chars - header_size - 50

    body = email.get("body") or email.get("snippet") or ""
    if len(body) > available_for_body:
        truncated["body"] = body[:available_for_body - 3] + "..."
    else:
        truncated["body"] = body

    return truncated


async def sync_emails_to_zep(
    user_id: str,
    emails: List[Dict[str, Any]],
    max_concurrent: int = 3,
    mark_synced: bool = False,
) -> Dict[str, Any]:
    """
    Sync emails to a user's Zep knowledge graph.

    This is designed to be called as a background task after email extraction.
    It chunks emails appropriately and adds them to the user's graph.

    Args:
        user_id: User identifier
        emails: List of email dictionaries to sync (must have 'id' field for mark_synced)
        max_concurrent: Maximum concurrent API calls
        mark_synced: If True, mark successfully synced emails in database

    Returns:
        Dict with sync results:
        - success: bool
        - emails_synced: int
        - chunks_sent: int
        - errors: List[str]
        - synced_email_ids: List[str] (if mark_synced=True)
    """
    from app.integrations.zep_graph_client import get_zep_graph_client

    result = {
        "success": False,
        "emails_synced": 0,
        "chunks_sent": 0,
        "errors": [],
        "synced_email_ids": [],
    }

    if not emails:
        result["success"] = True
        return result

    if not settings.zep_graph_enabled or not settings.zep_graph_sync_emails:
        logger.debug(f"Zep graph sync disabled, skipping {len(emails)} emails")
        result["success"] = True
        return result

    zep = get_zep_graph_client()
    if not zep.is_graph_enabled():
        logger.debug("Zep graph client not available, skipping sync")
        result["success"] = True
        return result

    chunks = chunk_emails_for_zep(emails)
    if not chunks:
        result["success"] = True
        return result

    logger.info(
        f"[ZEP_SYNC] Starting sync user={user_id[:8]}... "
        f"emails={len(emails)} chunks={len(chunks)}"
    )

    semaphore = asyncio.Semaphore(max_concurrent)
    synced_email_ids: List[str] = []

    async def sync_chunk(chunk: List[Dict[str, Any]], chunk_idx: int) -> bool:
        async with semaphore:
            try:
                text = format_emails_for_graph(chunk)
                add_result = await zep.add_to_graph(
                    user_id=user_id,
                    data=text,
                    data_type="text",
                )
                if add_result.success:
                    # Collect email IDs from this successful chunk
                    for email in chunk:
                        email_id = email.get("id")
                        if email_id:
                            synced_email_ids.append(email_id)
                    return True
                else:
                    result["errors"].append(
                        f"Chunk {chunk_idx}: {add_result.error}"
                    )
                    return False
            except Exception as e:
                result["errors"].append(f"Chunk {chunk_idx}: {str(e)}")
                return False

    tasks = [sync_chunk(chunk, i) for i, chunk in enumerate(chunks)]
    chunk_results = await asyncio.gather(*tasks, return_exceptions=True)

    successful_chunks = sum(
        1 for r in chunk_results
        if r is True
    )
    result["chunks_sent"] = successful_chunks

    emails_in_successful = sum(
        len(chunks[i])
        for i, r in enumerate(chunk_results)
        if r is True
    )
    result["emails_synced"] = emails_in_successful
    result["synced_email_ids"] = synced_email_ids

    # Mark emails as synced in database if requested
    if mark_synced and synced_email_ids:
        try:
            from app.database.client import DatabaseClient
            db = DatabaseClient()
            marked = await db.mark_emails_zep_synced(synced_email_ids)
            logger.info(
                f"[ZEP_SYNC] Marked {marked} emails as synced for user={user_id[:8]}..."
            )
        except Exception as e:
            logger.warning(
                f"[ZEP_SYNC] Failed to mark emails as synced: {e}"
            )
            result["errors"].append(f"mark_synced failed: {str(e)}")

    result["success"] = successful_chunks > 0

    logger.info(
        f"[ZEP_SYNC] Completed user={user_id[:8]}... "
        f"synced={result['emails_synced']}/{len(emails)} "
        f"chunks={successful_chunks}/{len(chunks)} "
        f"errors={len(result['errors'])}"
    )

    return result


async def sync_unsynced_emails_to_zep(
    user_id: str,
    max_emails: int = 500,
    max_concurrent: int = 3,
) -> Dict[str, Any]:
    """
    Sync only unsynced emails to Zep for a user (incremental sync).

    This queries the database for emails where zep_synced_at IS NULL,
    syncs them to Zep, and marks them as synced.

    Args:
        user_id: User identifier
        max_emails: Maximum emails to sync in one call
        max_concurrent: Maximum concurrent API calls

    Returns:
        Dict with sync results including emails_found and emails_synced
    """
    from app.database.client import DatabaseClient

    result = {
        "success": False,
        "emails_found": 0,
        "emails_synced": 0,
        "chunks_sent": 0,
        "errors": [],
    }

    if not settings.zep_graph_enabled or not settings.zep_graph_sync_emails:
        logger.debug(f"[ZEP_SYNC] Zep graph sync disabled for user={user_id[:8]}...")
        result["success"] = True
        return result

    try:
        db = DatabaseClient()
        unsynced_emails = await db.get_unsynced_emails_for_zep(
            user_id=user_id,
            limit=max_emails,
        )

        result["emails_found"] = len(unsynced_emails)

        if not unsynced_emails:
            logger.debug(f"[ZEP_SYNC] No unsynced emails for user={user_id[:8]}...")
            result["success"] = True
            return result

        logger.info(
            f"[ZEP_SYNC] Found {len(unsynced_emails)} unsynced emails for user={user_id[:8]}..."
        )

        # Sync with mark_synced=True to update database after success
        sync_result = await sync_emails_to_zep(
            user_id=user_id,
            emails=unsynced_emails,
            max_concurrent=max_concurrent,
            mark_synced=True,
        )

        result["success"] = sync_result["success"]
        result["emails_synced"] = sync_result["emails_synced"]
        result["chunks_sent"] = sync_result["chunks_sent"]
        result["errors"] = sync_result["errors"]

        return result

    except Exception as e:
        logger.error(
            f"[ZEP_SYNC] Error in incremental sync for user={user_id[:8]}...: {e}",
            exc_info=True,
        )
        result["errors"].append(str(e))
        return result


async def sync_unsynced_highlights_to_zep(
    user_id: str,
    max_highlights: int = 500,
    max_concurrent: int = 3,
) -> Dict[str, Any]:
    """
    Sync only unsynced email highlights to Zep for a user (incremental sync).

    This queries the user_email_highlights table for entries where zep_synced_at IS NULL,
    syncs them to Zep, and marks them as synced.

    Highlights are pre-filtered important emails (no ads/promotions) so this
    produces higher quality context for Zep's knowledge graph.

    Args:
        user_id: User identifier
        max_highlights: Maximum highlights to sync in one call
        max_concurrent: Maximum concurrent API calls

    Returns:
        Dict with sync results including highlights_found and highlights_synced
    """
    from app.database.client import DatabaseClient

    result = {
        "success": False,
        "highlights_found": 0,
        "highlights_synced": 0,
        "chunks_sent": 0,
        "errors": [],
    }

    if not settings.zep_graph_enabled or not settings.zep_graph_sync_emails:
        logger.debug(f"[ZEP_SYNC] Zep graph sync disabled for user={user_id[:8]}...")
        result["success"] = True
        return result

    try:
        db = DatabaseClient()
        unsynced_highlights = await db.get_unsynced_highlights_for_zep(
            user_id=user_id,
            limit=max_highlights,
        )

        result["highlights_found"] = len(unsynced_highlights)

        if not unsynced_highlights:
            logger.debug(f"[ZEP_SYNC] No unsynced highlights for user={user_id[:8]}...")
            result["success"] = True
            return result

        logger.info(
            f"[ZEP_SYNC] Found {len(unsynced_highlights)} unsynced highlights for user={user_id[:8]}..."
        )

        # Use existing sync_emails_to_zep with highlights (format_single_email handles both)
        # We need to track which highlights were synced for marking
        sync_result = await sync_emails_to_zep(
            user_id=user_id,
            emails=unsynced_highlights,
            max_concurrent=max_concurrent,
            mark_synced=False,  # We'll mark highlights separately
        )

        # Mark successfully synced highlights
        if sync_result.get("synced_email_ids"):
            marked = await db.mark_highlights_zep_synced(sync_result["synced_email_ids"])
            logger.info(
                f"[ZEP_SYNC] Marked {marked} highlights as synced for user={user_id[:8]}..."
            )

        result["success"] = sync_result["success"]
        result["highlights_synced"] = sync_result["emails_synced"]
        result["chunks_sent"] = sync_result["chunks_sent"]
        result["errors"] = sync_result["errors"]

        return result

    except Exception as e:
        logger.error(
            f"[ZEP_SYNC] Error in highlight sync for user={user_id[:8]}...: {e}",
            exc_info=True,
        )
        result["errors"].append(str(e))
        return result


async def sync_signals_to_zep(
    user_id: str,
    signals: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Sync networking signals to a user's Zep knowledge graph.

    Args:
        user_id: User identifier
        signals: List of signal dictionaries from signal_extractor

    Returns:
        Dict with sync results
    """
    from app.integrations.zep_graph_client import get_zep_graph_client

    result = {
        "success": False,
        "signals_synced": 0,
        "errors": [],
    }

    if not signals:
        result["success"] = True
        return result

    if not settings.zep_graph_enabled or not settings.zep_graph_sync_signals:
        logger.debug(f"Zep graph signal sync disabled, skipping {len(signals)} signals")
        result["success"] = True
        return result

    zep = get_zep_graph_client()
    if not zep.is_graph_enabled():
        logger.debug("Zep graph client not available, skipping signal sync")
        result["success"] = True
        return result

    signal_text = format_signals_for_graph(signals)
    if not signal_text:
        result["success"] = True
        return result

    logger.info(
        f"[ZEP_SYNC] Syncing signals user={user_id[:8]}... count={len(signals)}"
    )

    try:
        add_result = await zep.add_to_graph(
            user_id=user_id,
            data=signal_text,
            data_type="text",
        )
        if add_result.success:
            result["success"] = True
            result["signals_synced"] = len(signals)
        else:
            result["errors"].append(add_result.error or "Unknown error")

    except Exception as e:
        result["errors"].append(str(e))

    logger.info(
        f"[ZEP_SYNC] Signals completed user={user_id[:8]}... "
        f"synced={result['signals_synced']}/{len(signals)}"
    )

    return result


def format_signals_for_graph(signals: List[Dict[str, Any]]) -> str:
    """
    Format networking signals for Zep graph ingestion.

    Args:
        signals: List of signal dictionaries

    Returns:
        Formatted signal text
    """
    if not signals:
        return ""

    lines = []
    today = datetime.utcnow().strftime("%Y-%m-%d")

    for signal in signals:
        signal_text = signal.get("signal_text", "")
        if not signal_text:
            continue

        urgency = signal.get("urgency_score", 0.5)
        urgency_label = "high" if urgency >= 0.7 else "medium" if urgency >= 0.4 else "low"

        reasoning = signal.get("extraction_reasoning", "")
        match_type = signal.get("match_type", "single")

        lines.append(f"Networking Signal ({today}):")
        lines.append(f"The user is seeking: {signal_text}")
        lines.append(f"Urgency: {urgency_label}")
        lines.append(f"Match type: {match_type}")
        if reasoning:
            lines.append(f"Context: {reasoning[:200]}")
        lines.append("---")

    return "\n".join(lines)


async def sync_profile_to_zep(
    user_id: str,
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Sync user profile data to their Zep knowledge graph.

    This should be called when user profile is updated to keep
    the graph in sync with the latest user information.

    Args:
        user_id: User identifier
        profile: User profile dictionary

    Returns:
        Dict with sync results
    """
    from app.integrations.zep_graph_client import get_zep_graph_client

    result = {
        "success": False,
        "error": None,
    }

    if not settings.zep_graph_enabled:
        result["success"] = True
        return result

    zep = get_zep_graph_client()
    if not zep.is_graph_enabled():
        result["success"] = True
        return result

    profile_text = format_profile_for_graph(profile)
    if not profile_text:
        result["success"] = True
        return result

    try:
        add_result = await zep.add_to_graph(
            user_id=user_id,
            data=profile_text,
            data_type="text",
        )
        result["success"] = add_result.success
        if not add_result.success:
            result["error"] = add_result.error

    except Exception as e:
        result["error"] = str(e)

    return result


def format_profile_for_graph(profile: Dict[str, Any]) -> str:
    """
    Format user profile for Zep graph ingestion.

    Args:
        profile: User profile dictionary

    Returns:
        Formatted profile text
    """
    lines = ["User Profile:"]

    name = profile.get("name")
    if name:
        lines.append(f"Name: {name}")

    university = profile.get("university")
    if university:
        lines.append(f"University: {university}")

    major = profile.get("major")
    if major:
        lines.append(f"Major: {major}")

    year = profile.get("year")
    if year:
        lines.append(f"Year: {year}")

    location = profile.get("location")
    if location:
        lines.append(f"Location: {location}")

    career_interests = profile.get("career_interests") or []
    if career_interests:
        lines.append(f"Career interests: {', '.join(career_interests)}")

    all_demand = profile.get("all_demand")
    if all_demand:
        lines.append(f"What they're seeking: {all_demand}")

    all_value = profile.get("all_value")
    if all_value:
        lines.append(f"What they offer: {all_value}")

    if len(lines) <= 1:
        return ""

    lines.append("---")
    return "\n".join(lines)
