"""Database client methods for proactive_outreach_jobs table."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _ProactiveOutreachJobMethods:
    """Mixin for proactive outreach job operations."""

    async def schedule_proactive_outreach_job_v1(
        self,
        *,
        user_id: str,
        run_after: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Schedule or reschedule a proactive outreach job for a user.

        Args:
            user_id: User ID to schedule job for
            run_after: Optional specific time (ISO format), defaults to next 6 PM UTC

        Returns:
            The scheduled job row or None on error
        """
        try:
            result = self.client.rpc(
                "schedule_proactive_outreach_job_v1",
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
            logger.error(f"Error scheduling proactive outreach job: {e}", exc_info=True)
            return None

    async def claim_proactive_outreach_jobs_v1(
        self,
        *,
        worker_id: str,
        max_jobs: int = 5,
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
            max_jobs = max(1, min(int(max_jobs or 5), 50))
            stale_after = f"{max(1, int(stale_minutes or 30))} minutes"
            result = self.client.rpc(
                "claim_proactive_outreach_jobs_v1",
                {
                    "p_worker_id": str(worker_id),
                    "p_max_jobs": max_jobs,
                    "p_stale_after": stale_after,
                },
            ).execute()
            return result.data if isinstance(result.data, list) else []
        except Exception as e:
            logger.error(f"Error claiming proactive outreach jobs: {e}", exc_info=True)
            return []

    async def complete_proactive_outreach_job_v1(
        self,
        *,
        user_id: str,
        worker_id: str,
        demand_id: Optional[str] = None,
        connection_request_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Mark a job as complete (outreach was sent) and schedule next run.

        Args:
            user_id: User ID of the job
            worker_id: Worker that processed the job
            demand_id: Optional demand ID that was used
            connection_request_id: Optional connection request ID created

        Returns:
            Updated job row or None on error
        """
        try:
            result = self.client.rpc(
                "complete_proactive_outreach_job_v1",
                {
                    "p_user_id": str(user_id),
                    "p_worker_id": str(worker_id),
                    "p_demand_id": str(demand_id) if demand_id else None,
                    "p_connection_request_id": str(connection_request_id) if connection_request_id else None,
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error completing proactive outreach job: {e}", exc_info=True)
            return None

    async def skip_proactive_outreach_job_v1(
        self,
        *,
        user_id: str,
        worker_id: str,
        skip_reason: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Mark a job as skipped (no outreach sent) and schedule next run.

        Args:
            user_id: User ID of the job
            worker_id: Worker that processed the job
            skip_reason: Why the job was skipped

        Returns:
            Updated job row or None on error
        """
        try:
            result = self.client.rpc(
                "skip_proactive_outreach_job_v1",
                {
                    "p_user_id": str(user_id),
                    "p_worker_id": str(worker_id),
                    "p_skip_reason": str(skip_reason or "")[:500],
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error skipping proactive outreach job: {e}", exc_info=True)
            return None

    async def fail_proactive_outreach_job_v1(
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
                "fail_proactive_outreach_job_v1",
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
            logger.error(f"Error failing proactive outreach job: {e}", exc_info=True)
            return None

    async def release_proactive_outreach_job_v1(
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
                "release_proactive_outreach_job_v1",
                {
                    "p_user_id": str(user_id),
                    "p_worker_id": str(worker_id),
                },
            ).execute()
            return True
        except Exception as e:
            logger.error(f"Error releasing proactive outreach job: {e}", exc_info=True)
            return False
