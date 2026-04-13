"""
Redis Client with Connection Pooling for High Concurrency

This module provides a singleton Redis client with connection pooling
optimized for handling 200+ concurrent payment operations.

Features:
- Connection pooling with configurable max connections
- Idempotency checking for webhooks
- Subscription status caching
- Rate limiting
- Circuit breaker pattern for resilience

Location: app/utils/redis_client.py
"""

import redis
from redis.connection import ConnectionPool
from typing import Optional, Any
import json
from datetime import timedelta
import logging
from functools import wraps
from app.config import settings

logger = logging.getLogger(__name__)


class RedisClient:
    """
    Singleton Redis client with connection pooling.

    This client is designed to handle high concurrency (200+ simultaneous operations)
    without exhausting connections or blocking the event loop.
    """

    _instance: Optional['RedisClient'] = None
    _pool: Optional[ConnectionPool] = None
    _client: Optional[redis.Redis] = None

    def __new__(cls):
        """Ensure singleton instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Initialize Redis connection pool."""
        try:
            # Create connection pool with configured max connections
            self._pool = ConnectionPool.from_url(
                settings.redis_url,
                max_connections=settings.redis_max_connections,
                decode_responses=True,  # Automatically decode bytes to strings
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
                health_check_interval=30  # Health check every 30s
            )

            self._client = redis.Redis(connection_pool=self._pool)

            # Test connection
            self._client.ping()
            logger.info(
                f"[REDIS] Connection pool initialized: "
                f"max_connections={settings.redis_max_connections}"
            )

        except Exception as e:
            logger.error(f"[REDIS] Failed to initialize connection pool: {e}", exc_info=True)
            raise

    @property
    def client(self) -> redis.Redis:
        """Get Redis client instance."""
        if self._client is None:
            raise RuntimeError("Redis client not initialized")
        return self._client

    def close(self):
        """Close Redis connection pool."""
        if self._pool:
            self._pool.disconnect()
            logger.info("[REDIS] Connection pool closed")

    # ==================== IDEMPOTENCY ====================

    def check_idempotency(self, key: str, ttl: Optional[int] = None) -> bool:
        """
        Check if an operation has already been processed (idempotency).

        Args:
            key: Unique identifier for the operation (e.g., "stripe_event:evt_123")
            ttl: Time-to-live in seconds (default: from settings)

        Returns:
            True if operation is new (not seen before)
            False if operation is duplicate (already processed)

        Example:
            >>> redis_client = RedisClient()
            >>> if not redis_client.check_idempotency("stripe_event:evt_123"):
            >>>     return {"status": "duplicate"}
            >>> # Process the event...
        """
        try:
            ttl = ttl or settings.redis_idempotency_ttl

            # SET NX (set if not exists) returns True if key was set, None if key already exists
            # In redis-py 4.x, None means the key exists (not False!)
            is_new = self.client.set(key, "processed", nx=True, ex=ttl)

            # DEBUG: Log what Redis actually returned
            logger.critical(f"[REDIS DEBUG] Raw return: {repr(is_new)}, type={type(is_new).__name__}, key={key[:60]}")

            # Convert to boolean: True = new operation, None/False = duplicate
            is_new_bool = (is_new is True)

            if is_new_bool:
                logger.info(f"[REDIS IDEMPOTENCY] New operation: {key}")
            else:
                logger.warning(f"[REDIS IDEMPOTENCY] Duplicate detected: {key}")

            return is_new_bool

        except Exception as e:
            logger.error(f"[REDIS IDEMPOTENCY] Error checking {key}: {e}", exc_info=True)
            # On Redis failure, allow operation to proceed (fail open)
            return True

    def mark_processed(self, key: str, ttl: Optional[int] = None) -> bool:
        """
        Mark an operation as processed.

        Args:
            key: Unique identifier
            ttl: Time-to-live in seconds

        Returns:
            True if successfully marked
        """
        try:
            ttl = ttl or settings.redis_idempotency_ttl
            self.client.setex(key, ttl, "processed")
            return True
        except Exception as e:
            logger.error(f"[REDIS IDEMPOTENCY] Error marking processed {key}: {e}")
            return False

    # ==================== CACHING ====================

    def get_cached(self, key: str) -> Optional[Any]:
        """
        Get cached value.

        Args:
            key: Cache key

        Returns:
            Cached value (deserialized from JSON) or None if not found
        """
        try:
            value = self.client.get(key)
            if value:
                logger.debug(f"[REDIS CACHE] Hit: {key}")
                return json.loads(value)
            logger.debug(f"[REDIS CACHE] Miss: {key}")
            return None
        except Exception as e:
            logger.error(f"[REDIS CACHE] Error getting {key}: {e}")
            return None

    def set_cached(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        Set cached value.

        Args:
            key: Cache key
            value: Value to cache (will be JSON serialized)
            ttl: Time-to-live in seconds (default: from settings)

        Returns:
            True if successfully cached
        """
        try:
            ttl = ttl or settings.redis_cache_ttl
            serialized = json.dumps(value)
            self.client.setex(key, ttl, serialized)
            logger.debug(f"[REDIS CACHE] Set: {key} (TTL={ttl}s)")
            return True
        except Exception as e:
            logger.error(f"[REDIS CACHE] Error setting {key}: {e}")
            return False

    def invalidate_cache(self, key: str) -> bool:
        """
        Invalidate cached value.

        Args:
            key: Cache key to delete

        Returns:
            True if key was deleted
        """
        try:
            deleted = self.client.delete(key)
            if deleted:
                logger.debug(f"[REDIS CACHE] Invalidated: {key}")
            return bool(deleted)
        except Exception as e:
            logger.error(f"[REDIS CACHE] Error invalidating {key}: {e}")
            return False

    # ==================== RATE LIMITING ====================

    def check_rate_limit(
        self,
        key: str,
        max_requests: int,
        window_seconds: Optional[int] = None
    ) -> tuple[bool, int]:
        """
        Check rate limit using sliding window.

        Args:
            key: Rate limit key (e.g., "stripe_api:user_123")
            max_requests: Maximum requests allowed in window
            window_seconds: Time window in seconds (default: from settings)

        Returns:
            Tuple of (allowed: bool, current_count: int)

        Example:
            >>> allowed, count = redis_client.check_rate_limit("stripe_api:create_payment", 100, 60)
            >>> if not allowed:
            >>>     raise RateLimitExceeded(f"Rate limit exceeded: {count}/{max_requests}")
        """
        try:
            window_seconds = window_seconds or settings.redis_rate_limit_window

            # Increment counter
            pipe = self.client.pipeline()
            pipe.incr(key)
            pipe.expire(key, window_seconds)
            result = pipe.execute()

            current_count = result[0]
            allowed = current_count <= max_requests

            if not allowed:
                logger.warning(
                    f"[REDIS RATE LIMIT] Exceeded: {key} "
                    f"({current_count}/{max_requests} in {window_seconds}s)"
                )

            return allowed, current_count

        except Exception as e:
            logger.error(f"[REDIS RATE LIMIT] Error checking {key}: {e}", exc_info=True)
            # On Redis failure, allow request (fail open)
            return True, 0

    # ==================== SUBSCRIPTION CACHING ====================

    def get_subscription_status(self, user_id: str) -> Optional[dict]:
        """
        Get cached subscription status for a user.

        Args:
            user_id: User UUID

        Returns:
            Dict with subscription info or None if not cached
        """
        key = f"subscription:{user_id}"
        return self.get_cached(key)

    def cache_subscription_status(
        self,
        user_id: str,
        subscription_data: dict,
        ttl: Optional[int] = None
    ) -> bool:
        """
        Cache subscription status for a user.

        Args:
            user_id: User UUID
            subscription_data: Subscription info to cache
            ttl: Cache TTL in seconds

        Returns:
            True if successfully cached
        """
        key = f"subscription:{user_id}"
        return self.set_cached(key, subscription_data, ttl)

    def invalidate_subscription_cache(self, user_id: str) -> bool:
        """
        Invalidate subscription cache when status changes.

        Args:
            user_id: User UUID

        Returns:
            True if cache was invalidated
        """
        key = f"subscription:{user_id}"
        return self.invalidate_cache(key)

    # ==================== CHAT GUID CACHING ====================

    def cache_chat_guid(self, phone_number: str, chat_guid: str, ttl: int = 86400) -> bool:
        """
        Cache the real chat GUID from Apple for a phone number or email.

        This stores the actual chat GUID that Apple provides, which includes
        the correct service prefix (iMessage;-; or SMS;-;). This ensures
        typing indicators work correctly for phone numbers.

        Args:
            phone_number: Phone number or email address
            chat_guid: The actual chat GUID from Apple's iMessage system
            ttl: Time-to-live in seconds (default: 24 hours)

        Returns:
            True if successfully cached
        """
        try:
            key = f"chat_guid:{phone_number}"
            self.client.setex(key, ttl, chat_guid)
            logger.debug(f"[REDIS CHAT GUID] Cached: {phone_number} → {chat_guid[:20]}...")
            return True
        except Exception as e:
            logger.error(f"[REDIS CHAT GUID] Error caching {phone_number}: {e}")
            return False

    def get_cached_chat_guid(self, phone_number: str) -> Optional[str]:
        """
        Retrieve cached chat GUID for a phone number or email.

        Args:
            phone_number: Phone number or email address

        Returns:
            The cached chat GUID string, or None if not found
        """
        try:
            key = f"chat_guid:{phone_number}"
            value = self.client.get(key)
            if value:
                logger.debug(f"[REDIS CHAT GUID] Hit: {phone_number} → {value[:20]}...")
                return value
            logger.debug(f"[REDIS CHAT GUID] Miss: {phone_number}")
            return None
        except Exception as e:
            logger.error(f"[REDIS CHAT GUID] Error getting {phone_number}: {e}")
            return None

    # ==================== CIRCUIT BREAKER ====================

    def get_circuit_breaker_status(self, service: str) -> Optional[str]:
        """
        Get circuit breaker status for a service.

        Args:
            service: Service name (e.g., "stripe_api")

        Returns:
            Status: "open", "closed", "half_open", or None
        """
        key = f"circuit_breaker:{service}"
        return self.client.get(key)

    def open_circuit_breaker(self, service: str, ttl: int = 60) -> bool:
        """
        Open circuit breaker (stop calling failing service).

        Args:
            service: Service name
            ttl: How long to keep circuit open (seconds)

        Returns:
            True if circuit was opened
        """
        try:
            key = f"circuit_breaker:{service}"
            self.client.setex(key, ttl, "open")
            logger.warning(f"[REDIS CIRCUIT BREAKER] Opened: {service} (TTL={ttl}s)")
            return True
        except Exception as e:
            logger.error(f"[REDIS CIRCUIT BREAKER] Error opening {service}: {e}")
            return False

    def close_circuit_breaker(self, service: str) -> bool:
        """
        Close circuit breaker (service recovered).

        Args:
            service: Service name

        Returns:
            True if circuit was closed
        """
        try:
            key = f"circuit_breaker:{service}"
            deleted = self.client.delete(key)
            if deleted:
                logger.info(f"[REDIS CIRCUIT BREAKER] Closed: {service}")
            return bool(deleted)
        except Exception as e:
            logger.error(f"[REDIS CIRCUIT BREAKER] Error closing {service}: {e}")
            return False


# Global singleton instance
redis_client = RedisClient()


# ==================== DECORATORS ====================

def with_idempotency(key_prefix: str):
    """
    Decorator to add idempotency checking to a function.

    Args:
        key_prefix: Prefix for idempotency key

    Example:
        >>> @with_idempotency("stripe_webhook")
        >>> async def process_webhook(event_id: str):
        >>>     # Will automatically check if event_id was already processed
        >>>     pass
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract event ID from first argument (assumes it's event_id or similar)
            event_id = args[0] if args else kwargs.get('event_id', 'unknown')
            idempotency_key = f"{key_prefix}:{event_id}"

            # Check if already processed
            if not redis_client.check_idempotency(idempotency_key):
                logger.warning(f"[IDEMPOTENCY] Skipping duplicate: {idempotency_key}")
                return {"status": "duplicate", "processed": False}

            # Process the function
            result = await func(*args, **kwargs)
            return result

        return wrapper
    return decorator


def with_rate_limit(key_prefix: str, max_requests: int, window_seconds: int = 60):
    """
    Decorator to add rate limiting to a function.

    Args:
        key_prefix: Prefix for rate limit key
        max_requests: Max requests allowed in window
        window_seconds: Time window in seconds

    Example:
        >>> @with_rate_limit("stripe_create_payment", 100, 60)
        >>> async def create_payment_link(user_id: str):
        >>>     # Will be rate limited to 100 calls per minute
        >>>     pass
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Use function name as part of key
            rate_limit_key = f"{key_prefix}:{func.__name__}"

            allowed, count = redis_client.check_rate_limit(
                rate_limit_key,
                max_requests,
                window_seconds
            )

            if not allowed:
                logger.error(f"[RATE LIMIT] Exceeded for {rate_limit_key}: {count}/{max_requests}")
                raise Exception(f"Rate limit exceeded: {count}/{max_requests} in {window_seconds}s")

            return await func(*args, **kwargs)

        return wrapper
    return decorator
