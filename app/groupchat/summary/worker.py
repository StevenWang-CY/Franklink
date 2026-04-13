from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.database.client import DatabaseClient
from app.integrations.azure_openai_client import AzureOpenAIClient

from app.groupchat.summary.prompts import build_groupchat_summary_messages
from app.groupchat.summary.utils import clip, parse_timestamp, utc_iso

logger = logging.getLogger(__name__)

_CHAT_GUID_LOG_LEN = 40
_EVENT_ID_LOG_LEN = 18


def _configure_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return

    level_name = str(os.getenv("GROUPCHAT_SUMMARY_LOG_LEVEL") or "").strip().upper()
    if not level_name:
        level_name = "DEBUG" if bool(getattr(settings, "debug", False)) else "INFO"
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    for noisy in ("httpx", "httpcore", "hpack", "h2", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _default_worker_id() -> str:
    host = socket.gethostname() or "host"
    pid = os.getpid()
    return f"{host}:{pid}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _compute_backoff_seconds(attempts: int) -> int:
    # 30s, 60s, 120s, 240s, 480s, 600s...
    base = 30
    cap = 600
    try:
        n = int(attempts or 0)
    except Exception:
        n = 0
    return min(cap, int(base * (2 ** max(0, n))))


def _extract_latest_user_anchor(messages: List[Dict[str, Any]]) -> Tuple[Optional[datetime], str]:
    best_at: Optional[datetime] = None
    best_id = ""
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "user":
            continue
        ts = parse_timestamp(msg.get("sent_at"))
        if not ts:
            continue
        if best_at is None or ts > best_at:
            best_at = ts
            best_id = str(msg.get("event_id") or "").strip()
    return best_at, best_id


def _format_transcript_lines(
    *,
    messages: List[Dict[str, Any]],
    name_by_user_id: Optional[Dict[str, str]] = None,
    max_lines: int = 120,
    max_chars_per_line: int = 360,
) -> List[str]:
    out: List[str] = []
    for msg in messages or []:
        role = str(msg.get("role") or "").lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        sender_user_id = str(msg.get("sender_user_id") or "").strip()
        name = "frank" if role == "assistant" else "user"
        if role == "user" and sender_user_id and name_by_user_id and sender_user_id in name_by_user_id:
            name = str(name_by_user_id.get(sender_user_id) or name).strip() or name
        typ = str(msg.get("msg_type") or "").strip()
        ts = parse_timestamp(msg.get("sent_at"))
        ts_s = utc_iso(ts) if ts else ""
        label = f"{role}:{name}"
        if typ:
            label = f"{label} ({clip(typ, 32)})"
        out.append(f"[{ts_s}] {label}: {clip(content, max_chars_per_line)}")
    if len(out) <= max_lines:
        return out
    return out[-max_lines:]


@dataclass
class GroupChatSummaryWorker:
    db: DatabaseClient
    openai: AzureOpenAIClient
    worker_id: str

    async def _fail_job(
        self,
        *,
        chat_guid: str,
        expected_last_user_event_id: str,
        attempts: int,
        error: str,
    ) -> None:
        backoff = _compute_backoff_seconds(attempts)
        logger.warning(
            "[GROUPCHAT][SUMMARY] job_fail chat=%s event_id=%s attempts=%d backoff_sec=%d err=%s",
            clip(chat_guid, _CHAT_GUID_LOG_LEN),
            clip(expected_last_user_event_id, _EVENT_ID_LOG_LEN),
            int(attempts or 0),
            int(backoff),
            clip(str(error or ""), 160),
        )
        out = await self.db.fail_group_chat_summary_job_v1(
            chat_guid=chat_guid,
            worker_id=self.worker_id,
            expected_last_user_event_id=expected_last_user_event_id,
            error=error,
            backoff_seconds=backoff,
            max_attempts=int(getattr(settings, "groupchat_summary_worker_max_attempts", 6) or 6),
        )
        if out is None:
            await self.db.release_group_chat_summary_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)
            logger.warning(
                "[GROUPCHAT][SUMMARY] job_fail_release chat=%s event_id=%s",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                clip(expected_last_user_event_id, _EVENT_ID_LOG_LEN),
            )
        else:
            logger.info(
                "[GROUPCHAT][SUMMARY] job_fail_recorded chat=%s event_id=%s status=%s attempts=%s run_after=%s",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                clip(expected_last_user_event_id, _EVENT_ID_LOG_LEN),
                str(out.get("status") or ""),
                str(out.get("attempts") or ""),
                str(out.get("run_after") or ""),
            )

    async def _complete_job(self, *, chat_guid: str, expected_last_user_event_id: str) -> None:
        out = await self.db.complete_group_chat_summary_job_v1(
            chat_guid=chat_guid,
            worker_id=self.worker_id,
            expected_last_user_event_id=expected_last_user_event_id,
        )
        if out is None:
            await self.db.release_group_chat_summary_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)
            logger.warning(
                "[GROUPCHAT][SUMMARY] job_complete_release chat=%s event_id=%s",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                clip(expected_last_user_event_id, _EVENT_ID_LOG_LEN),
            )
        else:
            logger.info(
                "[GROUPCHAT][SUMMARY] job_complete chat=%s event_id=%s status=%s run_after=%s",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                clip(expected_last_user_event_id, _EVENT_ID_LOG_LEN),
                str(out.get("status") or ""),
                str(out.get("run_after") or ""),
            )

    async def run_once(self, *, max_jobs: int) -> int:
        if not getattr(settings, "groupchat_summary_enabled", False):
            logger.info("[GROUPCHAT][SUMMARY] disabled")
            return 0

        jobs = await self.db.claim_group_chat_summary_jobs_v1(
            worker_id=self.worker_id,
            max_jobs=max_jobs,
            stale_minutes=int(getattr(settings, "groupchat_summary_worker_stale_minutes", 20) or 20),
        )
        if not jobs:
            logger.debug("[GROUPCHAT][SUMMARY] idle worker=%s max_jobs=%d", str(self.worker_id), int(max_jobs or 0))
            return 0
        logger.info("[GROUPCHAT][SUMMARY] claimed worker=%s count=%d", str(self.worker_id), len(jobs))

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
                    "[GROUPCHAT][SUMMARY] job_crash chat=%s err=%s",
                    clip(chat_guid, _CHAT_GUID_LOG_LEN),
                    str(e),
                    exc_info=True,
                )
                if chat_guid:
                    if expected_event_id:
                        try:
                            await self._fail_job(
                                chat_guid=chat_guid,
                                expected_last_user_event_id=expected_event_id,
                                attempts=attempts,
                                error=f"job_crash:{type(e).__name__}:{e}",
                            )
                        except Exception:
                            await self.db.release_group_chat_summary_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)
                    else:
                        await self.db.release_group_chat_summary_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)
        return processed

    async def _fetch_recent_messages(self, *, chat_guid: str, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 200), 1200))
        return await self.db.get_group_chat_raw_messages_window_v1(chat_guid=chat_guid, limit=limit)

    async def _process_claimed_job(self, job: Dict[str, Any]) -> None:
        chat_guid = str(job.get("chat_guid") or "").strip()
        expected_event_id = str(job.get("last_user_event_id") or "").strip()
        if not chat_guid:
            return
        if not expected_event_id:
            logger.error("[GROUPCHAT][SUMMARY] invalid_job_missing_event_id chat=%s", clip(chat_guid, _CHAT_GUID_LOG_LEN))
            await self.db.release_group_chat_summary_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)
            return

        attempts = int(job.get("attempts") or 0)
        last_user_message_at = parse_timestamp(job.get("last_user_message_at"))
        run_after = parse_timestamp(job.get("run_after"))
        logger.info(
            "[GROUPCHAT][SUMMARY] job_start chat=%s event_id=%s attempts=%d run_after=%s",
            clip(chat_guid, _CHAT_GUID_LOG_LEN),
            clip(expected_event_id, _EVENT_ID_LOG_LEN),
            int(attempts or 0),
            str(job.get("run_after") or ""),
        )
        if not last_user_message_at or not run_after:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error="missing_job_timestamps",
            )
            return

        # Defensive: if the job row was scheduled late/out-of-order, use Zep to find the true latest user anchor
        # and reschedule before summarizing.
        messages = await self._fetch_recent_messages(chat_guid=chat_guid, limit=200)
        if not messages:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error="raw_memory_empty",
            )
            return
        latest_user_at, latest_user_id = _extract_latest_user_anchor(messages)
        if latest_user_at and (latest_user_at - last_user_message_at) > timedelta(seconds=1):
            logger.info(
                "[GROUPCHAT][SUMMARY] job_reschedule_newer_user chat=%s old=%s new=%s",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                utc_iso(last_user_message_at),
                utc_iso(latest_user_at),
            )
            try:
                await self.db.schedule_group_chat_summary_job_v1(
                    chat_guid=chat_guid,
                    last_user_message_at=utc_iso(latest_user_at),
                    last_user_event_id=latest_user_id or expected_event_id,
                    inactivity_minutes=int(settings.groupchat_summary_inactivity_minutes),
                )
            except Exception as e:
                await self._fail_job(
                    chat_guid=chat_guid,
                    expected_last_user_event_id=expected_event_id,
                    attempts=attempts,
                    error=f"reschedule_failed:{type(e).__name__}:{e}",
                )
                return
            await self.db.release_group_chat_summary_job_v1(chat_guid=chat_guid, worker_id=self.worker_id)
            return

        # Fetch segment start from summary memory.
        memory_row = await self.db.get_group_chat_summary_memory_v1(chat_guid=chat_guid)
        segment_start_at = parse_timestamp((memory_row or {}).get("last_segment_end_at"))
        existing_event_ids = (memory_row or {}).get("last_user_event_id") or []
        if isinstance(existing_event_ids, list) and expected_event_id in {str(x) for x in existing_event_ids}:
            logger.info(
                "[GROUPCHAT][SUMMARY] already_stored chat=%s event_id=%s",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                clip(expected_event_id, _EVENT_ID_LOG_LEN),
            )
            await self._complete_job(chat_guid=chat_guid, expected_last_user_event_id=expected_event_id)
            return

        segment_end_at = run_after

        # Slice transcript window.
        window_msgs: List[Dict[str, Any]] = []
        for msg in messages or []:
            ts = parse_timestamp(msg.get("sent_at"))
            if not ts:
                continue
            if segment_start_at and ts < segment_start_at:
                continue
            if ts >= segment_end_at:
                continue
            window_msgs.append(msg)

        anchor_present = any(
            str(m.get("event_id") or "").strip() == expected_event_id and str(m.get("role") or "").lower() == "user"
            for m in window_msgs
        )
        if not anchor_present:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error="anchor_not_found_in_raw_window",
            )
            return

        # Resolve participant names (best-effort).
        user_a_name = "user a"
        user_b_name = "user b"
        name_by_user_id: Dict[str, str] = {}
        try:
            chat = await self.db.get_group_chat_by_guid(chat_guid)
        except Exception:
            chat = None

        if isinstance(chat, dict):
            try:
                user_a_id = str(chat.get("user_a_id") or "").strip()
                user_b_id = str(chat.get("user_b_id") or "").strip()
                user_a = await self.db.get_user_by_id(user_a_id)
                user_b = await self.db.get_user_by_id(user_b_id)
                user_a_name = str((user_a or {}).get("name") or "").strip() or user_a_name
                user_b_name = str((user_b or {}).get("name") or "").strip() or user_b_name
                if user_a_id:
                    name_by_user_id[user_a_id] = user_a_name
                if user_b_id:
                    name_by_user_id[user_b_id] = user_b_name
            except Exception:
                pass

        transcript_lines = _format_transcript_lines(messages=window_msgs, name_by_user_id=name_by_user_id)
        if not transcript_lines:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error="empty_transcript_window",
            )
            return

        # Summarize.
        model = str(getattr(settings, "groupchat_summary_model", "gpt-4o-mini") or "gpt-4o-mini")
        logger.info(
            "[GROUPCHAT][SUMMARY] summarize_try chat=%s event_id=%s model=%s msgs=%d window=%s..%s",
            clip(chat_guid, _CHAT_GUID_LOG_LEN),
            clip(expected_event_id, _EVENT_ID_LOG_LEN),
            str(model),
            len(window_msgs),
            utc_iso(segment_start_at) if segment_start_at else "",
            utc_iso(segment_end_at),
        )
        msgs = build_groupchat_summary_messages(
            chat_guid=chat_guid,
            participant_a_name=user_a_name,
            participant_b_name=user_b_name,
            segment_start_at=utc_iso(segment_start_at) if segment_start_at else None,
            segment_end_at=utc_iso(segment_end_at),
            transcript_lines=transcript_lines,
        )

        started = time.perf_counter()
        try:
            summary_md = await self.openai.generate_response(
                messages=msgs,
                model=model,
                temperature=0.2,
                trace_label="groupchat_summary_v1",
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
                "[GROUPCHAT][SUMMARY] llm_done chat=%s event_id=%s dur_sec=%.2f",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                clip(expected_event_id, _EVENT_ID_LOG_LEN),
                dur,
            )

        summary_md = (summary_md or "").strip()
        if not summary_md:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error="empty_llm_output",
            )
            return

        # Persist (atomic append).
        try:
            out = await self.db.append_group_chat_summary_memory_segment_v1(
                chat_guid=chat_guid,
                last_user_event_id=expected_event_id,
                last_user_message_at=utc_iso(last_user_message_at),
                segment_end_at=utc_iso(segment_end_at),
                summary_md=summary_md,
            )
            if out is None:
                raise ValueError("append returned no data")
            logger.info(
                "[GROUPCHAT][SUMMARY] stored chat=%s event_id=%s segment_end_at=%s summary_len=%d",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                clip(expected_event_id, _EVENT_ID_LOG_LEN),
                utc_iso(segment_end_at),
                len(summary_md),
            )
        except Exception as e:
            await self._fail_job(
                chat_guid=chat_guid,
                expected_last_user_event_id=expected_event_id,
                attempts=attempts,
                error=f"append_failed:{str(e)[:240]}",
            )
            return

        # Prune raw transcript now that this segment is durably stored.
        try:
            out = await self.db.prune_group_chat_raw_memory_before_v1(
                chat_guid=chat_guid,
                before=utc_iso(segment_end_at),
                keep_tail=40,
            )
            logger.info(
                "[GROUPCHAT][SUMMARY] pruned_raw chat=%s event_id=%s ok=%s before=%s",
                clip(chat_guid, _CHAT_GUID_LOG_LEN),
                clip(expected_event_id, _EVENT_ID_LOG_LEN),
                "yes" if out is not None else "no",
                utc_iso(segment_end_at),
            )
        except Exception:
            pass

        await self._complete_job(chat_guid=chat_guid, expected_last_user_event_id=expected_event_id)


async def _run_worker_once(*, worker_id: str, max_jobs: int) -> int:
    worker = GroupChatSummaryWorker(
        db=DatabaseClient(),
        openai=AzureOpenAIClient(),
        worker_id=worker_id,
    )
    return await worker.run_once(max_jobs=max_jobs)


def main(argv: Optional[List[str]] = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(description="Group chat summary worker (Supabase job queue)")
    parser.add_argument("--worker-id", default=_default_worker_id())
    parser.add_argument("--max-jobs", type=int, default=int(getattr(settings, "groupchat_summary_worker_max_jobs", 5) or 5))
    parser.add_argument("--loop", action="store_true", help="Run forever (use with --interval-seconds)")
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args(argv)

    async def _run() -> int:
        if not args.loop:
            return await _run_worker_once(worker_id=args.worker_id, max_jobs=args.max_jobs)

        # Loop mode: reuse clients to avoid leaking connections over time.
        if not getattr(settings, "groupchat_summary_enabled", False):
            logger.info("[GROUPCHAT][SUMMARY] disabled (loop_mode=yes)")
            while True:
                await asyncio.sleep(max(60, int(args.interval_seconds or 60)))

        worker = GroupChatSummaryWorker(
            db=DatabaseClient(),
            openai=AzureOpenAIClient(),
            worker_id=args.worker_id,
        )

        interval = max(5, int(args.interval_seconds or 60))
        while True:
            try:
                processed = await worker.run_once(max_jobs=args.max_jobs)
            except Exception as e:
                logger.error("[GROUPCHAT][SUMMARY] loop_error err=%s", e, exc_info=True)
                processed = 0

            # Drain backlog faster when work was done, otherwise idle.
            await asyncio.sleep(1 if processed > 0 else interval)

    return int(asyncio.run(_run()) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
