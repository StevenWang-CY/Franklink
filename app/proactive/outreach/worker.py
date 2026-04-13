"""Proactive outreach worker entry point.

Usage:
    python -m app.proactive.outreach.worker [--worker-id ID] [--max-jobs N] [--loop] [--interval-seconds N]

Example:
    # Run once
    python -m app.proactive.outreach.worker --max-jobs=20

    # Run in loop mode (for continuous operation)
    python -m app.proactive.outreach.worker --loop --interval-seconds=300
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
from app.integrations.azure_openai_client import AzureOpenAIClient
from app.proactive.config import (
    PROACTIVE_OUTREACH_WORKER_MAX_JOBS,
    PROACTIVE_OUTREACH_WORKER_POLL_SECONDS,
)
from app.proactive.outreach.service import ProactiveOutreachService

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configure logging for the worker."""
    root = logging.getLogger()
    if root.handlers:
        return

    level_name = str(os.getenv("PROACTIVE_OUTREACH_LOG_LEVEL") or "").strip().upper()
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
    return f"proactive_outreach:{host}:{pid}"


async def _run_worker_once(*, worker_id: str, max_jobs: int) -> int:
    """Run the worker once and return number of jobs processed."""
    service = ProactiveOutreachService(
        db=DatabaseClient(),
        worker_id=worker_id,
        openai=AzureOpenAIClient(),
    )
    return await service.run_once(max_jobs=max_jobs)


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point for the proactive outreach worker."""
    _configure_logging()

    parser = argparse.ArgumentParser(description="Proactive outreach worker")
    parser.add_argument(
        "--worker-id",
        default=_default_worker_id(),
        help="Unique identifier for this worker instance",
    )
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=PROACTIVE_OUTREACH_WORKER_MAX_JOBS,
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
        default=PROACTIVE_OUTREACH_WORKER_POLL_SECONDS,
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
        if not getattr(settings, "proactive_outreach_worker_enabled", False):
            logger.info("[PROACTIVE_OUTREACH] disabled (loop_mode=yes)")
            while True:
                await asyncio.sleep(3600)

        logger.info(
            "[PROACTIVE_OUTREACH] starting loop mode worker_id=%s interval=%ds",
            args.worker_id,
            args.interval_seconds,
        )

        service = ProactiveOutreachService(
            db=DatabaseClient(),
            worker_id=args.worker_id,
            openai=AzureOpenAIClient(),
        )

        interval = max(5, int(args.interval_seconds or PROACTIVE_OUTREACH_WORKER_POLL_SECONDS))
        while True:
            try:
                processed = await service.run_once(max_jobs=args.max_jobs)
            except Exception as e:
                logger.error("[PROACTIVE_OUTREACH] loop_error err=%s", e, exc_info=True)
                processed = 0

            # Drain backlog faster when work was done, otherwise idle
            await asyncio.sleep(1 if processed > 0 else interval)

    return int(asyncio.run(_run()) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
