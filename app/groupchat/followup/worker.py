from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Optional

from app.config import settings
from app.database.client import DatabaseClient
from app.groupchat.followup.service import GroupChatFollowupService
from app.groupchat.followup.utils import configure_followup_logging, default_worker_id

logger = logging.getLogger(__name__)


async def _run_worker_once(*, worker_id: str, max_jobs: int) -> int:
    worker = GroupChatFollowupService(db=DatabaseClient(), worker_id=worker_id)
    return await worker.run_once(max_jobs=max_jobs)


def main(argv: Optional[list[str]] = None) -> int:
    configure_followup_logging()
    parser = argparse.ArgumentParser(description="Group chat inactivity follow-up worker")
    parser.add_argument("--worker-id", default=default_worker_id())
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=int(getattr(settings, "groupchat_followup_worker_max_jobs", 5) or 5),
    )
    parser.add_argument("--loop", action="store_true", help="Run forever (use with --interval-seconds)")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=int(getattr(settings, "groupchat_followup_poll_seconds", 10) or 10),
    )
    args = parser.parse_args(argv)

    async def _run() -> int:
        if not args.loop:
            return await _run_worker_once(worker_id=args.worker_id, max_jobs=args.max_jobs)

        if not getattr(settings, "groupchat_followup_enabled", False):
            logger.info("[GROUPCHAT][FOLLOWUP] disabled (loop_mode=yes)")
            while True:
                await asyncio.sleep(max(60, int(args.interval_seconds or 60)))

        worker = GroupChatFollowupService(db=DatabaseClient(), worker_id=args.worker_id)
        interval = max(5, int(args.interval_seconds or 10))
        while True:
            try:
                processed = await worker.run_once(max_jobs=args.max_jobs)
            except Exception as e:
                logger.error("[GROUPCHAT][FOLLOWUP] loop_error err=%s", e, exc_info=True)
                processed = 0
            await asyncio.sleep(1 if processed > 0 else interval)

    return int(asyncio.run(_run()) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
