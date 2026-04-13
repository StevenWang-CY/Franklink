import asyncio
import logging
import time
from functools import wraps
from typing import Callable, TypeVar

from postgrest.exceptions import APIError

logger = logging.getLogger(__name__)

T = TypeVar("T")


def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retryable_errors: tuple = (APIError,),
):
    """
    Decorator to add retry logic with exponential backoff for transient errors.

    Handles transient Supabase/Cloudflare errors like:
    - Error 1101: Worker threw exception
    - 502/503/504: Gateway errors
    - Connection timeouts
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def async_wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable_errors as e:
                    last_exception = e
                    error_str = str(e)
                    is_transient = any(
                        indicator in error_str
                        for indicator in [
                            "Worker threw exception",
                            "502",
                            "503",
                            "504",
                            "timeout",
                            "Timeout",
                            "connection",
                            "Connection",
                            "ECONNRESET",
                            "ETIMEDOUT",
                            "JSON could not be generated",
                        ]
                    )

                    if (not is_transient) or attempt >= max_retries:
                        raise

                    delay = min(base_delay * (2**attempt), max_delay)
                    delay = delay * (0.5 + 0.5 * (time.time() % 1))

                    logger.warning(
                        "[RETRY] %s attempt %d/%d failed with transient error, retrying in %.1fs: %s",
                        func.__name__,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                        error_str[:100],
                    )
                    await asyncio.sleep(delay)

            raise last_exception

        @wraps(func)
        def sync_wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable_errors as e:
                    last_exception = e
                    error_str = str(e)
                    is_transient = any(
                        indicator in error_str
                        for indicator in [
                            "Worker threw exception",
                            "502",
                            "503",
                            "504",
                            "timeout",
                            "Timeout",
                            "connection",
                            "Connection",
                            "JSON could not be generated",
                        ]
                    )

                    if (not is_transient) or attempt >= max_retries:
                        raise

                    delay = min(base_delay * (2**attempt), max_delay)
                    logger.warning(
                        "[RETRY] %s attempt %d/%d failed, retrying in %.1fs",
                        func.__name__,
                        attempt + 1,
                        max_retries + 1,
                        delay,
                    )
                    time.sleep(delay)

            raise last_exception

        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper

    return decorator

