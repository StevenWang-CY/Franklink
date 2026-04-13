from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.config import settings
from app.database.client import DatabaseClient
from app.groupchat.followup.context import build_summary_segments, fetch_recent_messages, load_participants
from app.groupchat.followup.prompts import build_groupchat_followup_messages
from app.groupchat.followup.utils import (
    clean_followup,
    compute_backoff_seconds,
    effective_group_mode,
    extract_latest_user_anchor,
    nudge_event_id,
    now_utc,
    resolve_inactivity_minutes,
)
from app.groupchat.io.recorder import GroupChatRecorder
from app.groupchat.io.sender import GroupChatSender
from app.groupchat.summary.utils import clip, parse_timestamp, utc_iso
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.integrations.photon_client import PhotonClient

logger = logging.getLogger(__name__)

_CHAT_GUID_LOG_LEN = 40
_EVENT_ID_LOG_LEN = 18


@dataclass
class GroupChatFollowupService:
    db: DatabaseClient
    worker_id: str
    openai: AzureOpenAIClient | None = None
    sender: GroupChatSender | None = None

    def __post_init__(self) -> None:
        if self.openai is None:
            self.openai = AzureOpenAIClient()
        if self.sender is None:
            recorder = GroupChatRecorder(db=self.db)
            self.sender = GroupChatSender(photon=PhotonClient(), recorder=recorder)

    async def run_once(self, *, max_jobs: int) -> int:
        if not getattr(settings, "groupchat_followup_enabled", False):
            logger.info("[GROUPCHAT][FOLLOWUP] disabled")
            return 0

        jobs = await self.db.claim_group_chat_followup_jobs_v1(
            worker_id=self.worker_id,
            max_jobs=max_jobs,
            stale_minutes=int(getattr(settings, "groupchat_followup_worker_stale_minutes", 20) or 20),
        )
        if not jobs:
            logger.debug("[GROUPCHAT][FOLLOWUP] idle worker=%s max_jobs=%d", str(self.worker_id), int(max_jobs or 0))
            return 0

        processed = 0
        for job in jobs:
            processed += 1
            chat_guid = str((job or {}).get("chat_guid") or "").strip()
            expected_event_id = str((job or {}).get("last_user_event_id") or "").strip()
            attempts = int((job or {}).get("attempts") or 0)
            try:
                await self._process_claimed_job(job)
            except Exception as e:
                logger.error(
                    "[GROUPCHAT][FOLLOWUP] job_crash chat=%s err=%s",
                    clip(chat_guid, _CHAT_GUID_LOG_LEN),
                    str(e),
                    exc_info=True,
                )
                if chat_guid and expected_event_id:
                    try:
                        await self._fail_job(
                            chat_guid=chat_guid,
                            expected_last_user_event_id=expected_event_id,
                            attempts=attempts,
                            error=f"job_crash:{type(e).__name__}:{e}",
                        )
                    except Exception:
                        await self.db.release_group_chat_followup_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)
        return processed

    async def _fail_job(
        self,
        *,
        chat_guid: str,
        expected_last_user_event_id: str,
        attempts: int,
        error: str,
    ) -> None:
        backoff = compute_backoff_seconds(attempts)
        logger.warning(
            "[GROUPCHAT][FOLLOWUP] job_fail chat=%s event_id=%s attempts=%d backoff_sec=%d err=%s",
            clip(chat_guid, _CHAT_GUID_LOG_LEN),
            clip(expected_last_user_event_id, _EVENT_ID_LOG_LEN),
            int(attempts or 0),
            int(backoff),
            clip(str(error or ""), 160),
        )
        out = await self.db.fail_group_chat_followup_job_v1(
            chat_guid=chat_guid,
            worker_id=self.worker_id,
            expected_last_user_event_id=expected_last_user_event_id,
            error=error,
            backoff_seconds=backoff,
            max_attempts=int(getattr(settings, "groupchat_followup_worker_max_attempts", 6) or 6),
        )
        if out is None:
            await self.db.release_group_chat_followup_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)

    async def _complete_job(
        self,
        *,
        chat_guid: str,
        expected_last_user_event_id: str,
        nudge_sent_at: datetime,
        nudge_event_id: str,
    ) -> None:
        out = await self.db.complete_group_chat_followup_job_v1(
            chat_guid=chat_guid,
            worker_id=self.worker_id,
            expected_last_user_event_id=expected_last_user_event_id,
            nudge_sent_at=utc_iso(nudge_sent_at),
            nudge_event_id=nudge_event_id,
        )
        if out is None:
            await self.db.release_group_chat_followup_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)

    async def _process_claimed_job(self, job: Dict[str, Any]) -> None:
        chat_guid = str(job.get("chat_guid") or "").strip()
        expected_event_id = str(job.get("last_user_event_id") or "").strip()
        if not chat_guid or not expected_event_id:
            return

        attempts = int(job.get("attempts") or 0)
        last_user_message_at = parse_timestamp(job.get("last_user_message_at"))
        run_after = parse_timestamp(job.get("run_after"))
        last_nudge_at = parse_timestamp(job.get("last_nudge_at"))

        if not last_user_message_at or not run_after:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error="missing_job_timestamps",
            )
            return

        if last_nudge_at and last_nudge_at >= last_user_message_at:
            await self._complete_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                nudge_sent_at=last_nudge_at,
                nudge_event_id=str(job.get("last_nudge_event_id") or ""),
            )
            return

        now = now_utc()
        if run_after > now:
            await self.db.release_group_chat_followup_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)
            return

        chat, user_a_name, user_b_name = await load_participants(self.db, chat_guid=chat_guid)
        if not chat:
            await self._complete_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                nudge_sent_at=now,
                nudge_event_id="skip:chat_missing",
            )
            return

        mode = effective_group_mode(chat.get("user_a_mode"), chat.get("user_b_mode"))
        if mode == "muted":
            await self._complete_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                nudge_sent_at=now,
                nudge_event_id="skip:muted",
            )
            return

        messages = await fetch_recent_messages(self.db, chat_guid=chat_guid, limit=120)
        latest_user_at, latest_user_id = extract_latest_user_anchor(messages)
        if latest_user_at and (latest_user_at - last_user_message_at) > timedelta(seconds=1):
            try:
                await self.db.schedule_group_chat_followup_job_v1(
                    chat_guid=chat_guid,
                    last_user_message_at=utc_iso(latest_user_at),
                    last_user_event_id=latest_user_id or expected_event_id,
                    inactivity_minutes=resolve_inactivity_minutes(),
                )
            except Exception as e:
                await self._fail_job(
                    chat_guid=chat_guid,
                    expected_last_user_event_id=expected_event_id,
                    attempts=attempts,
                    error=f"reschedule_failed:{type(e).__name__}:{e}",
                )
                return
            await self.db.release_group_chat_followup_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)
            return

        window_days = int(getattr(settings, "groupchat_followup_summary_window_days", 7) or 7)
        segments = await build_summary_segments(
            self.db,
            chat_guid=chat_guid,
            window_days=window_days,
            now=now,
        )
        if not segments:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error="summary_empty",
            )
            return

        inactivity_minutes = resolve_inactivity_minutes()
        msgs = build_groupchat_followup_messages(
            chat_guid=chat_guid,
            participant_a_name=user_a_name,
            participant_b_name=user_b_name,
            inactivity_minutes=inactivity_minutes,
            last_user_message_at=utc_iso(last_user_message_at),
            summary_segments=segments,
        )

        model = str(getattr(settings, "groupchat_followup_model", "gpt-4o-mini") or "gpt-4o-mini")
        started = time.perf_counter()
        try:
            raw = await self.openai.generate_response(
                messages=msgs,
                model=model,
                temperature=0.6,
                trace_label="groupchat_followup_v1",
            )
        except Exception as e:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error=f"llm_failed:{str(e)[:240]}",
            )
            return
        finally:
            dur = time.perf_counter() - started
            logger.info(
                "[GROUPCHAT][FOLLOWUP] llm_done chat=%s event_id=%s dur_sec=%.2f",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                clip(expected_event_id, _EVENT_ID_LOG_LEN),
                dur,
            )

        content = clean_followup(raw)
        if not content:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error="empty_llm_output",
            )
            return

        try:
            await self.sender.send_and_record(
                chat_guid=chat_guid,
                content=content,
                metadata={"type": "relationship_followup_nudge_v1"},
            )
        except Exception as e:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error=f"send_failed:{str(e)[:240]}",
            )
            return

        await self._complete_job(
            chat_guid=chat_guid,
            expected_last_user_event_id=expected_event_id,
            nudge_sent_at=now,
            nudge_event_id=nudge_event_id(expected_event_id),
        )
