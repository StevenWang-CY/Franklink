"""Daily email extraction service.

Extracts new emails for users and creates highlights.
Runs daily at 5 PM UTC.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config import settings
from app.database.client import DatabaseClient
from app.proactive.config import (
    DAILY_EMAIL_WORKER_MAX_ATTEMPTS,
    DAILY_EMAIL_WORKER_STALE_MINUTES,
    compute_backoff_seconds,
)

logger = logging.getLogger(__name__)


def _clip(s: str, max_len: int = 40) -> str:
    """Truncate string for logging."""
    return s[:max_len] if len(s) > max_len else s


@dataclass
class DailyEmailService:
    """Service for daily email extraction jobs."""

    db: DatabaseClient
    worker_id: str

    async def run_once(self, *, max_jobs: int) -> int:
        """
        Process a batch of daily email extraction jobs.

        Returns:
            Number of jobs processed
        """
        if not getattr(settings, "daily_email_worker_enabled", False):
            logger.info("[DAILY_EMAIL] disabled")
            return 0

        # Claim available jobs
        jobs = await self.db.claim_daily_email_jobs_v1(
            worker_id=self.worker_id,
            max_jobs=max_jobs,
            stale_minutes=DAILY_EMAIL_WORKER_STALE_MINUTES,
        )

        if not jobs:
            logger.debug(
                "[DAILY_EMAIL] idle worker=%s max_jobs=%d",
                self.worker_id,
                max_jobs,
            )
            return 0

        logger.info(
            "[DAILY_EMAIL] claimed worker=%s count=%d",
            self.worker_id,
            len(jobs),
        )

        processed = 0
        for job in jobs:
            processed += 1
            user_id = str((job or {}).get("user_id") or "").strip()
            attempts = int((job or {}).get("attempts") or 0)

            try:
                await self._process_job(job)
            except Exception as e:
                logger.error(
                    "[DAILY_EMAIL] job_crash user_id=%s err=%s",
                    _clip(user_id),
                    str(e),
                    exc_info=True,
                )
                if user_id:
                    try:
                        await self._fail_job(
                            user_id=user_id,
                            attempts=attempts,
                            error=f"job_crash:{type(e).__name__}:{e}",
                        )
                    except Exception:
                        await self.db.release_daily_email_job_v1(
                            user_id=user_id,
                            worker_id=self.worker_id,
                        )

        return processed

    async def _process_job(self, job: Dict[str, Any]) -> None:
        """Process a single daily email extraction job."""
        user_id = str(job.get("user_id") or "").strip()
        if not user_id:
            return

        last_run_at = job.get("last_run_at")
        attempts = int(job.get("attempts") or 0)

        logger.info(
            "[DAILY_EMAIL] job_start user_id=%s last_run_at=%s attempts=%d",
            _clip(user_id),
            str(last_run_at or ""),
            attempts,
        )

        # Get user to check email connection status
        user = await self.db.get_user_by_id(user_id)
        if not user:
            logger.warning("[DAILY_EMAIL] user_not_found user_id=%s", _clip(user_id))
            await self._complete_job(user_id=user_id, emails_fetched=0, highlights_created=0)
            return

        # Check if user is onboarded
        if not user.get("is_onboarded"):
            logger.info("[DAILY_EMAIL] user_not_onboarded user_id=%s", _clip(user_id))
            await self._complete_job(user_id=user_id, emails_fetched=0, highlights_created=0)
            return

        # Check email connection status
        personal_facts = user.get("personal_facts") or {}
        email_connect = personal_facts.get("email_connect") or {}
        if email_connect.get("status") != "connected":
            logger.info("[DAILY_EMAIL] email_not_connected user_id=%s", _clip(user_id))
            await self._complete_job(user_id=user_id, emails_fetched=0, highlights_created=0)
            return

        # Extract emails since last run
        from app.agents.tools.email_extraction import extract_and_store_emails
        from datetime import datetime

        start_date = None
        if last_run_at:
            # Parse the last_run_at timestamp
            if isinstance(last_run_at, str):
                try:
                    start_date = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
                except Exception:
                    pass
            elif isinstance(last_run_at, datetime):
                start_date = last_run_at

        logger.info(
            "[DAILY_EMAIL] extracting user_id=%s start_date=%s",
            _clip(user_id),
            str(start_date) if start_date else "None (first run)",
        )

        result = await extract_and_store_emails(
            user_id=user_id,
            start_date=start_date,
            include_sent=True,
            max_received=100,
            max_sent=50,
            skip_zep_sync=True,  # We handle Zep sync separately with incremental sync
        )

        if not result.success:
            logger.error(
                "[DAILY_EMAIL] extraction_failed user_id=%s error=%s",
                _clip(user_id),
                result.error,
            )
            await self._fail_job(
                user_id=user_id,
                attempts=attempts,
                error=f"extraction_failed:{result.error}",
            )
            return

        emails_fetched = result.total_stored
        logger.info(
            "[DAILY_EMAIL] extracted user_id=%s fetched=%d stored=%d",
            _clip(user_id),
            result.total_fetched,
            result.total_stored,
        )

        # Process highlights for all stored emails (not just new ones)
        # This ensures keywords are applied with latest user profile
        from app.agents.tools.email_highlights import process_user_email_highlights

        highlight_result = await process_user_email_highlights(user_id=user_id)
        highlights_created = highlight_result.get("stored", 0)

        logger.info(
            "[DAILY_EMAIL] highlights_processed user_id=%s created=%d",
            _clip(user_id),
            highlights_created,
        )

        # Sync unsynced emails to Zep (incremental sync)
        zep_synced = 0
        try:
            from app.agents.tools.email_zep_sync import sync_unsynced_emails_to_zep

            zep_result = await sync_unsynced_emails_to_zep(
                user_id=user_id,
                max_emails=500,
            )
            zep_synced = zep_result.get("emails_synced", 0)
            if zep_result.get("errors"):
                logger.warning(
                    "[DAILY_EMAIL] zep_sync_errors user_id=%s errors=%s",
                    _clip(user_id),
                    zep_result["errors"][:2],
                )
        except Exception as e:
            logger.warning(
                "[DAILY_EMAIL] zep_sync_failed user_id=%s err=%s",
                _clip(user_id),
                str(e),
            )

        logger.info(
            "[DAILY_EMAIL] zep_sync_completed user_id=%s synced=%d",
            _clip(user_id),
            zep_synced,
        )

        await self._complete_job(
            user_id=user_id,
            emails_fetched=emails_fetched,
            highlights_created=highlights_created,
        )

    async def _complete_job(
        self,
        *,
        user_id: str,
        emails_fetched: int,
        highlights_created: int,
    ) -> None:
        """Mark job as complete."""
        result = await self.db.complete_daily_email_job_v1(
            user_id=user_id,
            worker_id=self.worker_id,
            emails_fetched=emails_fetched,
            highlights_created=highlights_created,
        )
        if result:
            logger.info(
                "[DAILY_EMAIL] job_complete user_id=%s emails=%d highlights=%d",
                _clip(user_id),
                emails_fetched,
                highlights_created,
            )
        else:
            logger.warning(
                "[DAILY_EMAIL] job_complete_failed user_id=%s",
                _clip(user_id),
            )

    async def _fail_job(
        self,
        *,
        user_id: str,
        attempts: int,
        error: str,
    ) -> None:
        """Mark job as failed with backoff."""
        backoff = compute_backoff_seconds(attempts)
        logger.warning(
            "[DAILY_EMAIL] job_fail user_id=%s attempts=%d backoff_sec=%d err=%s",
            _clip(user_id),
            attempts,
            backoff,
            _clip(str(error), 160),
        )
        result = await self.db.fail_daily_email_job_v1(
            user_id=user_id,
            worker_id=self.worker_id,
            error=error,
            backoff_seconds=backoff,
            max_attempts=DAILY_EMAIL_WORKER_MAX_ATTEMPTS,
        )
        if result is None:
            await self.db.release_daily_email_job_v1(
                user_id=user_id,
                worker_id=self.worker_id,
            )
