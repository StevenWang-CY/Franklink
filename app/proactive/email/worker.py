"""Daily email extraction worker entry point.

Usage:
    python -m app.proactive.email.worker [--worker-id ID] [--max-jobs N] [--loop] [--interval-seconds N]

Example:
    # Run once
    python -m app.proactive.email.worker --max-jobs=50

    # Run in loop mode (for continuous operation)
    python -m app.proactive.email.worker --loop --interval-seconds=300
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
from typing import List, Optional

from app.config import settings
from app.database.client import DatabaseClient
from app.proactive.config import (
    DAILY_EMAIL_WORKER_MAX_JOBS,
    DAILY_EMAIL_WORKER_POLL_SECONDS,
)
from app.proactive.email.service import DailyEmailService

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configure logging for the worker."""
    root = logging.getLogger()
    if root.handlers:
        return

    level_name = str(os.getenv("DAILY_EMAIL_LOG_LEVEL") or "").strip().upper()
    if not level_name:
        level_name = "DEBUG" if bool(getattr(settings, "debug", False)) else "INFO"
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    # Quiet noisy loggers
    for noisy in ("httpx", "httpcore", "hpack", "h2", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _default_worker_id() -> str:
    """Generate default worker ID from hostname and PID."""
    host = socket.gethostname() or "host"
    pid = os.getpid()
    return f"daily_email:{host}:{pid}"


async def _run_worker_once(*, worker_id: str, max_jobs: int) -> int:
    """Run the worker once and return number of jobs processed."""
    service = DailyEmailService(
        db=DatabaseClient(),
        worker_id=worker_id,
    )
    return await service.run_once(max_jobs=max_jobs)


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point for the daily email worker."""
    _configure_logging()

    parser = argparse.ArgumentParser(description="Daily email extraction worker")
    parser.add_argument(
        "--worker-id",
        default=_default_worker_id(),
        help="Unique identifier for this worker instance",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=DAILY_EMAIL_WORKER_MAX_JOBS,
        help="Maximum number of jobs to process per iteration",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously (use with --interval-seconds)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DAILY_EMAIL_WORKER_POLL_SECONDS,
        help="Seconds between poll iterations in loop mode",
    )
    args = parser.parse_args(argv)

    async def _run() -> int:
        if not args.loop:
            # Single run mode
            return await _run_worker_once(
                worker_id=args.worker_id,
                max_jobs=args.max_jobs,
            )

        # Loop mode: run continuously
        if not getattr(settings, "daily_email_worker_enabled", False):
            logger.info("[DAILY_EMAIL] disabled (loop_mode=yes)")
            while True:
                await asyncio.sleep(3600)

        logger.info(
            "[DAILY_EMAIL] starting loop mode worker_id=%s interval=%ds",
            args.worker_id,
            args.interval_seconds,
        )

        service = DailyEmailService(
            db=DatabaseClient(),
            worker_id=args.worker_id,
        )

        interval = max(5, int(args.interval_seconds or DAILY_EMAIL_WORKER_POLL_SECONDS))
        while True:
            try:
                processed = await service.run_once(max_jobs=args.max_jobs)
            except Exception as e:
                logger.error("[DAILY_EMAIL] loop_error err=%s", e, exc_info=True)
                processed = 0

            # Drain backlog faster when work was done, otherwise idle
            await asyncio.sleep(1 if processed > 0 else interval)

    return int(asyncio.run(_run()) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
