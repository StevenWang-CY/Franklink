"""Internal database client implementation.

This package splits the Supabase DatabaseClient into focused mixins.
"""

import logging
import json
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from uuid import UUID

from postgrest.exceptions import APIError

from .retry import with_retry

logger = logging.getLogger(__name__)


class _GroupChatSummaryMethods:
    async def ingest_group_chat_user_message_and_schedule_summary_v1(
        self,
        *,
        chat_guid: str,
        event_id: str,
        message_id: Optional[str],
        sender_user_id: Optional[str],
        sender_handle: Optional[str],
        sent_at: str,
        content: str,
        media_url: Optional[str] = None,
        inactivity_minutes: int = 5,
        keep_last_n: int = 800,
    ) -> Optional[Dict[str, Any]]:
        """
        Atomic ingest for inbound user groupchat messages:
        - append raw transcript into group_chat_raw_memory_v1 (idempotent by event_id)
        - schedule/upsert group_chat_summary_jobs (debounced)
        """
        try:
            interval = f"{max(1, int(inactivity_minutes or 5))} minutes"
            result = self.client.rpc(
                "ingest_group_chat_user_message_and_schedule_summary_v1",
                {
                    "p_chat_guid": str(chat_guid),
                    "p_event_id": str(event_id),
                    "p_message_id": str(message_id or ""),
                    "p_sender_user_id": str(sender_user_id) if sender_user_id else None,
                    "p_sender_handle": str(sender_handle or ""),
                    "p_sent_at": str(sent_at),
                    "p_content": str(content or ""),
                    "p_media_url": str(media_url or ""),
                    "p_inactivity_window": interval,
                    "p_keep_last_n": max(1, min(int(keep_last_n or 800), 4000)),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error ingesting group chat user message: {e}", exc_info=True)
            return None

    async def append_group_chat_raw_message_v1(
        self,
        *,
        chat_guid: str,
        event_id: str,
        message_id: Optional[str],
        role: str,
        sender_user_id: Optional[str],
        sender_handle: Optional[str],
        sent_at: str,
        content: str,
        media_url: Optional[str] = None,
        msg_type: Optional[str] = None,
        keep_last_n: int = 800,
    ) -> Optional[Dict[str, Any]]:
        """
        Append a raw transcript message into group_chat_raw_memory_v1 (atomic + idempotent).
        Used for assistant outbound messages and best-effort for non-managed chats.
        """
        try:
            result = self.client.rpc(
                "append_group_chat_raw_message_v1",
                {
                    "p_chat_guid": str(chat_guid),
                    "p_event_id": str(event_id),
                    "p_message_id": str(message_id or ""),
                    "p_role": str(role or ""),
                    "p_sender_user_id": str(sender_user_id) if sender_user_id else None,
                    "p_sender_handle": str(sender_handle or ""),
                    "p_sent_at": str(sent_at),
                    "p_content": str(content or ""),
                    "p_media_url": str(media_url or ""),
                    "p_msg_type": str(msg_type or ""),
                    "p_keep_last_n": max(1, min(int(keep_last_n or 800), 4000)),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error appending group chat raw message: {e}", exc_info=True)
            return None

    async def get_group_chat_raw_messages_window_v1(
        self,
        *,
        chat_guid: str,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """
        Fetch group chat transcript window as rows from group_chat_raw_memory_v1.
        """
        try:
            limit = max(1, min(int(limit or 200), 500))
            result = self.client.rpc(
                "get_group_chat_raw_messages_window_v1",
                {
                    "p_chat_guid": str(chat_guid),
                    "p_start_at": str(start_at) if start_at else None,
                    "p_end_at": str(end_at) if end_at else None,
                    "p_limit": limit,
                },
            ).execute()
            return result.data if isinstance(result.data, list) else []
        except Exception as e:
            logger.error(f"Error getting group chat raw messages window: {e}", exc_info=True)
            return []

    async def prune_group_chat_raw_memory_before_v1(
        self,
        *,
        chat_guid: str,
        before: str,
        keep_tail: int = 40,
    ) -> Optional[Dict[str, Any]]:
        """
        Prune group_chat_raw_memory_v1 arrays to keep only messages at/after `before`
        (with a small overlap tail).
        """
        try:
            keep_tail = max(0, min(int(keep_tail or 40), 400))
            result = self.client.rpc(
                "prune_group_chat_raw_memory_before_v1",
                {
                    "p_chat_guid": str(chat_guid),
                    "p_before": str(before),
                    "p_keep_tail": keep_tail,
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error pruning group chat raw memory: {e}", exc_info=True)
            return None

    async def schedule_group_chat_summary_job_v1(
        self,
        *,
        chat_guid: str,
        last_user_message_at: str,
        last_user_event_id: str,
        inactivity_minutes: int = 5,
    ) -> Optional[Dict[str, Any]]:
        """
        Debounced schedule/upsert for the group chat summary job row (one row per chat).
        Best-effort: safe to call on every inbound user message.
        """
        try:
            interval = f"{max(1, int(inactivity_minutes or 5))} minutes"
            result = self.client.rpc(
                "schedule_group_chat_summary_job_v1",
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
            logger.error(f"Error scheduling group chat summary job: {e}", exc_info=True)
            return None

    async def claim_group_chat_summary_jobs_v1(
        self,
        *,
        worker_id: str,
        max_jobs: int = 5,
        stale_minutes: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Claim due jobs using a Postgres RPC (multi-instance safe via FOR UPDATE SKIP LOCKED).
        """
        try:
            max_jobs = max(1, min(int(max_jobs or 5), 50))
            stale_after = f"{max(1, int(stale_minutes or 20))} minutes"
            result = self.client.rpc(
                "claim_group_chat_summary_jobs_v1",
                {
                    "p_worker_id": str(worker_id),
                    "p_max_jobs": max_jobs,
                    "p_stale_after": stale_after,
                },
            ).execute()
            return result.data if isinstance(result.data, list) else []
        except Exception as e:
            logger.error(f"Error claiming group chat summary jobs: {e}", exc_info=True)
            return []

    async def release_group_chat_summary_job_v1(self, *, chat_guid: str, worker_id: str) -> bool:
        """
        Clear claim fields and mark the job queued (does not modify run_after/anchors).
        Use when a worker decides to skip/re-schedule after claiming.
        """
        try:
            result = self.client.table("group_chat_summary_jobs").update(
                {
                    "status": "queued",
                    "claimed_by": None,
                    "claimed_at": None,
                    "updated_at": datetime.utcnow().isoformat(),
                }
            ).eq("chat_guid", str(chat_guid)).eq("claimed_by", str(worker_id)).execute()
            return bool(result.data)
        except Exception as e:
            logger.error(f"Error releasing group chat summary job: {e}", exc_info=True)
            return False

    async def complete_group_chat_summary_job_v1(
        self,
        *,
        chat_guid: str,
        worker_id: str,
        expected_last_user_event_id: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Finish a running job. If the job row's last_user_event_id changed while running, this
        releases it back to queued instead of marking done (prevents dropping new segments).
        """
        try:
            result = self.client.rpc(
                "complete_group_chat_summary_job_v1",
                {
                    "p_chat_guid": str(chat_guid),
                    "p_worker_id": str(worker_id),
                    "p_expected_last_user_event_id": str(expected_last_user_event_id),
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error completing group chat summary job: {e}", exc_info=True)
            return None

    async def fail_group_chat_summary_job_v1(
        self,
        *,
        chat_guid: str,
        worker_id: str,
        expected_last_user_event_id: str,
        error: str,
        backoff_seconds: int,
        max_attempts: int = 6,
    ) -> Optional[Dict[str, Any]]:
        """
        Record a failure, apply backoff, and release claim fields.
        """
        try:
            backoff_seconds = max(5, int(backoff_seconds or 60))
            max_attempts = max(1, int(max_attempts or 6))
            result = self.client.rpc(
                "fail_group_chat_summary_job_v1",
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
            logger.error(f"Error failing group chat summary job: {e}", exc_info=True)
            return None

    async def get_group_chat_summary_memory_v1(self, *, chat_guid: str) -> Optional[Dict[str, Any]]:
        """
        Fetch the one-row-per-chat summary memory record (if present).
        """
        try:
            result = self.client.table("group_chat_summary_memory_v1").select(
                "chat_guid,last_segment_end_at,last_user_event_id"
            ).eq("chat_guid", str(chat_guid)).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting group chat summary memory: {e}", exc_info=True)
            return None

    async def get_group_chat_summary_segments_v1(
        self,
        *,
        chat_guid: str,
        start_at: Optional[str] = None,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        """
        Fetch recent stored summary segments for a chat (newest-first) from the debug view.
        """
        try:
            limit = max(1, min(int(limit or 6), 200))
            query = self.client.table("group_chat_summary_segments_v1").select(
                "segment_end_at,last_user_event_id,summary_md,segment_index"
            ).eq("chat_guid", str(chat_guid))
            if start_at:
                query = query.gte("segment_end_at", str(start_at))
            result = query.order("segment_end_at", desc=True).limit(limit).execute()
            return result.data if isinstance(result.data, list) else []
        except Exception as e:
            logger.error(f"Error getting group chat summary segments: {e}", exc_info=True)
            return []

    async def append_group_chat_summary_memory_segment_v1(
        self,
        *,
        chat_guid: str,
        last_user_event_id: str,
        last_user_message_at: str,
        segment_end_at: str,
        summary_md: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Append a segment summary into group_chat_summary_memory_v1 (atomic + idempotent).
        """
        try:
            result = self.client.rpc(
                "append_group_chat_summary_memory_segment_v1",
                {
                    "p_chat_guid": str(chat_guid),
                    "p_last_user_event_id": str(last_user_event_id),
                    "p_last_user_message_at": str(last_user_message_at),
                    "p_segment_end_at": str(segment_end_at),
                    "p_summary_md": str(summary_md or "")[:20000],
                },
            ).execute()
            if isinstance(result.data, dict):
                return result.data
            if isinstance(result.data, list) and result.data:
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"Error appending group chat summary memory: {e}", exc_info=True)
            return None
