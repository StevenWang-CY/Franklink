"""Database client methods for daily_email_jobs table."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _DailyEmailJobMethods:
    """Mixin for daily email job operations."""

    async def schedule_daily_email_job_v1(
        self,
        *,
        user_id: str,
        run_after: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Schedule or reschedule a daily email job for a user.

        Args:
            user_id: User ID to schedule job for
            run_after: Optional specific time (ISO format), defaults to next 5 PM UTC

        Returns:
            The scheduled job row or None on error
        """
        try:
            result = self.client.rpc(
                "schedule_daily_email_job_v1",
                {
                    "p_user_id": str(user_id),
                    "p_run_after": run_after,
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error scheduling daily email job: {e}", exc_info=True)
            return None

    async def claim_daily_email_jobs_v1(
        self,
        *,
        worker_id: str,
        max_jobs: int = 10,
        stale_minutes: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Claim due jobs using a Postgres RPC (multi-instance safe via FOR UPDATE SKIP LOCKED).

        Args:
            worker_id: Unique identifier for this worker instance
            max_jobs: Maximum number of jobs to claim
            stale_minutes: Consider jobs stale after this many minutes

        Returns:
            List of claimed job rows
        """
        try:
            max_jobs = max(1, min(int(max_jobs or 10), 100))
            stale_after = f"{max(1, int(stale_minutes or 30))} minutes"
            result = self.client.rpc(
                "claim_daily_email_jobs_v1",
                {
                    "p_worker_id": str(worker_id),
                    "p_max_jobs": max_jobs,
                    "p_stale_after": stale_after,
                },
            ).execute()
            return result.data if isinstance(result.data, list) else []
        except Exception as e:
            logger.error(f"Error claiming daily email jobs: {e}", exc_info=True)
            return []

    async def complete_daily_email_job_v1(
        self,
        *,
        user_id: str,
        worker_id: str,
        emails_fetched: int,
        highlights_created: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Mark a job as complete and schedule next run for tomorrow.

        Args:
            user_id: User ID of the job
            worker_id: Worker that processed the job
            emails_fetched: Number of emails fetched
            highlights_created: Number of highlights created

        Returns:
            Updated job row or None on error
        """
        try:
            result = self.client.rpc(
                "complete_daily_email_job_v1",
                {
                    "p_user_id": str(user_id),
                    "p_worker_id": str(worker_id),
                    "p_emails_fetched": int(emails_fetched or 0),
                    "p_highlights_created": int(highlights_created or 0),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error completing daily email job: {e}", exc_info=True)
            return None

    async def fail_daily_email_job_v1(
        self,
        *,
        user_id: str,
        worker_id: str,
        error: str,
        backoff_seconds: int = 1800,
        max_attempts: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """
        Mark a job as failed with exponential backoff.

        Args:
            user_id: User ID of the job
            worker_id: Worker that processed the job
            error: Error message
            backoff_seconds: Seconds to wait before retry
            max_attempts: Maximum retry attempts before marking as failed

        Returns:
            Updated job row or None on error
        """
        try:
            result = self.client.rpc(
                "fail_daily_email_job_v1",
                {
                    "p_user_id": str(user_id),
                    "p_worker_id": str(worker_id),
                    "p_error": str(error or "")[:2000],
                    "p_backoff_seconds": max(60, int(backoff_seconds or 1800)),
                    "p_max_attempts": max(1, int(max_attempts or 5)),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error failing daily email job: {e}", exc_info=True)
            return None

    async def release_daily_email_job_v1(
        self,
        *,
        user_id: str,
        worker_id: str,
    ) -> bool:
        """
        Release a claimed job back to queued status.

        Args:
            user_id: User ID of the job
            worker_id: Worker that claimed the job

        Returns:
            True if released successfully
        """
        try:
            self.client.rpc(
                "release_daily_email_job_v1",
                {
                    "p_user_id": str(user_id),
                    "p_worker_id": str(worker_id),
                },
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Error releasing daily email job: {e}", exc_info=True)
            return False
