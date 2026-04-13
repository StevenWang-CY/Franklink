"""Location update worker entry point.

Usage:
    python -m app.proactive.location.worker [--worker-id ID] [--loop] [--interval-seconds N]

Example:
    # Run once
    python -m app.proactive.location.worker

    # Run in loop mode (for continuous operation)
    python -m app.proactive.location.worker --loop --interval-seconds=3600
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
from app.integrations.photon_client import PhotonClient
from app.proactive.config import LOCATION_UPDATE_WORKER_POLL_SECONDS
from app.proactive.location.service import LocationUpdateService

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configure logging for the worker."""
    root = logging.getLogger()
    if root.handlers:
        return

    level_name = str(os.getenv("LOCATION_UPDATE_LOG_LEVEL") or "").strip().upper()
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
    return f"location_update:{host}:{pid}"


async def _run_worker_once(*, worker_id: str) -> int:
    """Run the worker once and return number of locations updated."""
    service = LocationUpdateService(
        db=DatabaseClient(),
        photon=PhotonClient(),
    )
    return await service.run_once()


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point for the location update worker."""
    _configure_logging()

    parser = argparse.ArgumentParser(description="Location update worker")
    parser.add_argument(
        "--worker-id",
        default=_default_worker_id(),
        help="Unique identifier for this worker instance",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously (use with --interval-seconds)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=LOCATION_UPDATE_WORKER_POLL_SECONDS,
        help="Seconds between poll iterations in loop mode",
    )
    args = parser.parse_args(argv)

    async def _run() -> int:
        if not args.loop:
            # Single run mode
            return await _run_worker_once(worker_id=args.worker_id)

        # Loop mode: run continuously
        if not getattr(settings, "location_update_worker_enabled", False):
            logger.info("[LOCATION_UPDATE] disabled (loop_mode=yes)")
            while True:
                await asyncio.sleep(3600)

        logger.info(
            "[LOCATION_UPDATE] starting loop mode worker_id=%s interval=%ds",
            args.worker_id,
            args.interval_seconds,
        )

        service = LocationUpdateService(
            db=DatabaseClient(),
            photon=PhotonClient(),
        )

        interval = max(5, int(args.interval_seconds or LOCATION_UPDATE_WORKER_POLL_SECONDS))
        while True:
            try:
                updated = await service.run_once()
                logger.info("[LOCATION_UPDATE] iteration done, updated=%d", updated)
            except Exception as e:
                logger.error("[LOCATION_UPDATE] loop_error err=%s", e, exc_info=True)

            await asyncio.sleep(interval)

    return int(asyncio.run(_run()) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
