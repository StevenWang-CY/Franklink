"""Internal database client implementation (group chat follow-up jobs)."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class _GroupChatFollowupMethods:
    async def schedule_group_chat_followup_job_v1(
        self,
        *,
        chat_guid: str,
        last_user_message_at: str,
        last_user_event_id: str,
        inactivity_minutes: int = 1440,
    ) -> Optional[Dict[str, Any]]:
        try:
            interval = f"{max(1, int(inactivity_minutes or 1440))} minutes"
            result = self.client.rpc(
                "schedule_group_chat_followup_job_v1",
                {
                    "p_chat_guid": str(chat_guid),
                    "p_last_user_message_at": str(last_user_message_at),
                    "p_last_user_event_id": str(last_user_event_id),
                    "p_inactivity_window": interval,
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error scheduling group chat followup job: {e}", exc_info=True)
            return None

    async def claim_group_chat_followup_jobs_v1(
        self,
        *,
        worker_id: str,
        max_jobs: int = 5,
        stale_minutes: int = 20,
    ) -> List[Dict[str, Any]]:
        try:
            max_jobs = max(1, min(int(max_jobs or 5), 50))
            stale_after = f"{max(1, int(stale_minutes or 20))} minutes"
            result = self.client.rpc(
                "claim_group_chat_followup_jobs_v1",
                {
                    "p_worker_id": str(worker_id),
                    "p_max_jobs": max_jobs,
                    "p_stale_after": stale_after,
                },
            ).execute()
            return result.data if isinstance(result.data, list) else []
        except Exception as e:
            logger.error(f"Error claiming group chat followup jobs: {e}", exc_info=True)
            return []

    async def release_group_chat_followup_job_v1(self, *, chat_guid: str, worker_id: str) -> bool:
        try:
            result = self.client.table("group_chat_followup_jobs_v1").update(
                {
                    "status": "queued",
                    "claimed_by": None,
                    "claimed_at": None,
                    "updated_at": datetime.utcnow().isoformat(),
                }
            ).eq("chat_guid", str(chat_guid)).eq("claimed_by", str(worker_id)).execute()
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error releasing group chat followup job: {e}", exc_info=True)
            return False

    async def complete_group_chat_followup_job_v1(
        self,
        *,
        chat_guid: str,
        worker_id: str,
        expected_last_user_event_id: str,
        nudge_sent_at: str,
        nudge_event_id: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            result = self.client.rpc(
                "complete_group_chat_followup_job_v1",
                {
                    "p_chat_guid": str(chat_guid),
                    "p_worker_id": str(worker_id),
                    "p_expected_last_user_event_id": str(expected_last_user_event_id),
                    "p_nudge_sent_at": str(nudge_sent_at),
                    "p_nudge_event_id": str(nudge_event_id),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error completing group chat followup job: {e}", exc_info=True)
            return None

    async def fail_group_chat_followup_job_v1(
        self,
        *,
        chat_guid: str,
        worker_id: str,
        expected_last_user_event_id: str,
        error: str,
        backoff_seconds: int,
        max_attempts: int = 6,
    ) -> Optional[Dict[str, Any]]:
        try:
            backoff_seconds = max(5, int(backoff_seconds or 60))
            max_attempts = max(1, int(max_attempts or 6))
            result = self.client.rpc(
                "fail_group_chat_followup_job_v1",
                {
                    "p_chat_guid": str(chat_guid),
                    "p_worker_id": str(worker_id),
                    "p_expected_last_user_event_id": str(expected_last_user_event_id),
                    "p_error": str(error or "")[:1800],
                    "p_backoff": f"{backoff_seconds} seconds",
                    "p_max_attempts": max_attempts,
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error failing group chat followup job: {e}", exc_info=True)
            return None
