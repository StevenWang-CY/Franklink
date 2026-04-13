"""Internal database client implementation (user_emails)."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from postgrest.exceptions import APIError

logger = logging.getLogger(__name__)


class _UserEmailMethods:
    async def store_user_emails(self, user_id: str, emails: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Store fetched emails, skipping duplicates by message_id.
        Returns the rows stored (empty if none).
        """
        if not emails:
            return []

        try:
            # First, get existing message_ids for this user to avoid duplicates
            existing = (
                self.client.table("user_emails")
                .select("message_id")
                .eq("user_id", user_id)
                .execute()
            )
            existing_ids = {r.get("message_id") for r in (existing.data or []) if r.get("message_id")}

            rows = []
            for email in emails:
                message_id = email.get("message_id") or email.get("id")
                # Skip if we already have this email
                if message_id and message_id in existing_ids:
                    continue

                sender = email.get("sender") or ""
                sender_domain = None
                if "@" in sender:
                    # Extract domain from "Name <email@domain.com>" or "email@domain.com"
                    at_idx = sender.rfind("@")
                    domain_part = sender[at_idx + 1 :]
                    # Remove trailing > if present
                    sender_domain = domain_part.rstrip(">").split()[0].lower()

                rows.append(
                    {
                        "user_id": user_id,
                        "message_id": message_id,
                        "sender": sender,
                        "sender_domain": sender_domain,
                        "subject": email.get("subject"),
                        "body": email.get("body"),
                        "snippet": email.get("snippet"),
                        "received_at": email.get("received_at"),
                        "fetched_at": datetime.utcnow().isoformat(),
                        "is_sensitive": email.get("is_sensitive", False),
                        "is_sent": email.get("is_sent", False),
                    }
                )

            if not rows:
                logger.info(f"No new emails to store for user {user_id} (all duplicates)")
                return []

            # Insert new emails - duplicates are already filtered above
            # If a duplicate key error still occurs (race condition), catch and continue
            try:
                result = (
                    self.client.table("user_emails")
                    .insert(rows)
                    .execute()
                )
                stored_count = len(result.data) if result.data else 0
                logger.info(f"Stored {stored_count} new emails for user {user_id}")
                return rows
            except APIError as insert_error:
                # Handle duplicate key errors gracefully - some emails may already exist
                if "duplicate key" in str(insert_error).lower() or getattr(insert_error, "code", "") == "23505":
                    logger.warning(f"Duplicate key during batch insert for user {user_id}, retrying individually")
                else:
                    raise

            # Fallback: insert one by one if batch failed due to duplicates
            inserted_rows: List[Dict[str, Any]] = []
            for row in rows:
                try:
                    result = self.client.table("user_emails").insert(row).execute()
                    if result.data:
                        inserted_rows.append(row)
                except APIError as e:
                    if "duplicate key" in str(e).lower() or getattr(e, "code", "") == "23505":
                        continue  # Skip duplicates
                    raise
            logger.info(f"Stored {len(inserted_rows)} new emails for user {user_id} (individual insert)")
            return inserted_rows

        except APIError as e:
            logger.error(f"API error storing emails: {str(e)}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Error storing user emails: {str(e)}", exc_info=True)
            raise

    async def get_user_emails(
        self,
        user_id: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Get user emails from database.
        Returns emails ordered by fetched_at desc.
        """
        try:
            result = (
                self.client.table("user_emails")
                .select("sender,sender_domain,subject,body,snippet,received_at,fetched_at")
                .eq("user_id", user_id)
                .eq("is_sensitive", False)
                .order("fetched_at", desc=True)
                .limit(limit)
                .execute()
            )

            return list(result.data or [])

        except APIError as e:
            logger.error(f"API error fetching emails: {str(e)}", exc_info=True)
            return []
        except Exception as e:
            logger.error(f"Error fetching user emails: {str(e)}", exc_info=True)
            return []

    async def get_filtered_user_emails(
        self,
        user_id: str,
        *,
        keywords: List[str] = None,
        exclude_sender_patterns: List[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Get user emails with keyword-based filtering.

        Args:
            user_id: User ID
            keywords: List of keywords to search in subject/body (OR logic)
            exclude_sender_patterns: Patterns to exclude from sender (e.g., 'noreply', 'notifications')
            limit: Max emails to return

        Returns:
            Filtered emails - keyword matches first, then recent emails
        """
        try:
            # Build base query
            query = (
                self.client.table("user_emails")
                .select("sender,sender_domain,subject,body,snippet,received_at,fetched_at")
                .eq("user_id", user_id)
                .eq("is_sensitive", False)
            )

            # Execute query to get all candidate emails
            result = query.order("fetched_at", desc=True).limit(limit * 2).execute()
            emails = list(result.data or [])

            if not emails:
                return []

            # Filter out excluded sender patterns
            if exclude_sender_patterns:
                filtered = []
                for email in emails:
                    sender = (email.get("sender") or "").lower()
                    sender_domain = (email.get("sender_domain") or "").lower()
                    exclude = False
                    for pattern in exclude_sender_patterns:
                        pattern_lower = pattern.lower()
                        if pattern_lower in sender or pattern_lower in sender_domain:
                            exclude = True
                            break
                    if not exclude:
                        filtered.append(email)
                emails = filtered

            # Score and sort by keyword relevance
            if keywords:
                scored_emails = []
                for email in emails:
                    subject = (email.get("subject") or "").lower()
                    body = (email.get("body") or "").lower()
                    sender = (email.get("sender") or "").lower()
                    combined = f"{subject} {body} {sender}"

                    score = 0
                    matched_keywords = []
                    for kw in keywords:
                        kw_lower = kw.lower()
                        # Subject matches worth more
                        if kw_lower in subject:
                            score += 3
                            matched_keywords.append(kw)
                        elif kw_lower in sender:
                            score += 2
                            if kw not in matched_keywords:
                                matched_keywords.append(kw)
                        elif kw_lower in body:
                            score += 1
                            if kw not in matched_keywords:
                                matched_keywords.append(kw)

                    email["_relevance_score"] = score
                    email["_matched_keywords"] = matched_keywords
                    scored_emails.append(email)

                # Sort by score descending, then by fetched_at descending
                scored_emails.sort(key=lambda x: (-x.get("_relevance_score", 0),))
                emails = scored_emails

            # Return top N, removing internal scoring fields
            result_emails = []
            for email in emails[:limit]:
                clean_email = {k: v for k, v in email.items() if not k.startswith("_")}
                result_emails.append(clean_email)

            return result_emails

        except APIError as e:
            logger.error(f"API error fetching filtered emails: {str(e)}", exc_info=True)
            return []
        except Exception as e:
            logger.error(f"Error fetching filtered user emails: {str(e)}", exc_info=True)
            return []

    async def get_user_sent_emails(
        self,
        user_id: str,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Get user's SENT emails for professional needs/value analysis.

        Args:
            user_id: User ID
            limit: Max emails to return

        Returns:
            List of sent emails with subject, body, etc.
        """
        try:
            result = (
                self.client.table("user_emails")
                .select("sender,sender_domain,subject,body,snippet,received_at,fetched_at")
                .eq("user_id", user_id)
                .eq("is_sent", True)
                .eq("is_sensitive", False)
                .order("fetched_at", desc=True)
                .limit(limit)
                .execute()
            )
            return list(result.data or [])

        except APIError as e:
            logger.error(f"API error fetching sent emails: {str(e)}", exc_info=True)
            return []
        except Exception as e:
            logger.error(f"Error fetching user sent emails: {str(e)}", exc_info=True)
            return []

    async def get_emails_last_fetched(self, user_id: str) -> Optional[datetime]:
        """
        Check when emails were last fetched for this user.
        Returns None if no emails exist.
        """
        try:
            result = (
                self.client.table("user_emails")
                .select("fetched_at")
                .eq("user_id", user_id)
                .order("fetched_at", desc=True)
                .limit(1)
                .execute()
            )

            if result.data and result.data[0].get("fetched_at"):
                fetched_str = result.data[0]["fetched_at"]
                # Parse ISO8601 datetime
                if isinstance(fetched_str, str):
                    # Handle various ISO8601 formats
                    fetched_str = fetched_str.replace("Z", "+00:00")
                    try:
                        return datetime.fromisoformat(fetched_str)
                    except ValueError:
                        # Fallback: strip timezone and parse
                        return datetime.fromisoformat(fetched_str[:19])
                return fetched_str

            return None

        except Exception as e:
            logger.error(f"Error checking emails last fetched: {str(e)}", exc_info=True)
            return None

    async def get_unsynced_emails_for_zep(
        self,
        user_id: str,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """
        Get emails that haven't been synced to Zep yet.

        Args:
            user_id: User ID
            limit: Max emails to return

        Returns:
            List of emails where zep_synced_at IS NULL
        """
        try:
            result = (
                self.client.table("user_emails")
                .select("id,message_id,sender,sender_domain,subject,body,snippet,received_at,is_sent")
                .eq("user_id", user_id)
                .is_("zep_synced_at", "null")
                .eq("is_sensitive", False)
                .order("fetched_at", desc=True)
                .limit(limit)
                .execute()
            )
            return list(result.data or [])

        except APIError as e:
            logger.error(f"API error fetching unsynced emails: {str(e)}", exc_info=True)
            return []
        except Exception as e:
            logger.error(f"Error fetching unsynced emails: {str(e)}", exc_info=True)
            return []

    async def mark_emails_zep_synced(
        self,
        email_ids: List[str],
    ) -> int:
        """
        Mark emails as synced to Zep by setting zep_synced_at timestamp.

        Args:
            email_ids: List of email UUIDs to mark as synced

        Returns:
            Number of emails updated
        """
        if not email_ids:
            return 0

        try:
            now = datetime.utcnow().isoformat()
            result = (
                self.client.table("user_emails")
                .update({"zep_synced_at": now})
                .in_("id", email_ids)
                .execute()
            )
            updated = len(result.data) if result.data else 0
            logger.info(f"Marked {updated} emails as synced to Zep")
            return updated

        except APIError as e:
            logger.error(f"API error marking emails as synced: {str(e)}", exc_info=True)
            return 0
        except Exception as e:
            logger.error(f"Error marking emails as synced: {str(e)}", exc_info=True)
            return 0

    async def get_zep_sync_stats(self, user_id: str) -> Dict[str, Any]:
        """
        Get Zep sync statistics for a user.

        Args:
            user_id: User ID

        Returns:
            Dict with total_emails, synced_count, unsynced_count, last_sync_at
        """
        try:
            # Get total count
            total_result = (
                self.client.table("user_emails")
                .select("id", count="exact")
                .eq("user_id", user_id)
                .execute()
            )
            total = total_result.count if total_result.count else 0

            # Get unsynced count
            unsynced_result = (
                self.client.table("user_emails")
                .select("id", count="exact")
                .eq("user_id", user_id)
                .is_("zep_synced_at", "null")
                .execute()
            )
            unsynced = unsynced_result.count if unsynced_result.count else 0

            # Get last sync time
            last_sync_result = (
                self.client.table("user_emails")
                .select("zep_synced_at")
                .eq("user_id", user_id)
                .not_.is_("zep_synced_at", "null")
                .order("zep_synced_at", desc=True)
                .limit(1)
                .execute()
            )
            last_sync_at = None
            if last_sync_result.data and last_sync_result.data[0].get("zep_synced_at"):
                last_sync_at = last_sync_result.data[0]["zep_synced_at"]

            return {
                "total_emails": total,
                "synced_count": total - unsynced,
                "unsynced_count": unsynced,
                "last_sync_at": last_sync_at,
            }

        except Exception as e:
            logger.error(f"Error getting Zep sync stats: {str(e)}", exc_info=True)
            return {
                "total_emails": 0,
                "synced_count": 0,
                "unsynced_count": 0,
                "last_sync_at": None,
                "error": str(e),
            }
