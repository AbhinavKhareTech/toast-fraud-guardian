"""
Utility modules: rate limiter, circuit breaker, idempotency.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from enum import Enum
from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)


# --- Rate Limiter (Token Bucket) ---

class RateLimiter:
    """Async-safe token bucket rate limiter backed by Redis or in-memory."""

    def __init__(self, max_requests: int, window_seconds: float):
        self._max = max_requests
        self._window = window_seconds
        self._tokens: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def allow(self, key: str) -> bool:
        """Check if a request is allowed under the rate limit."""
        async with self._lock:
            now = time.monotonic()
            # Prune old entries
            self._tokens[key] = [
                t for t in self._tokens[key] if now - t < self._window
            ]
            if len(self._tokens[key]) >= self._max:
                return False
            self._tokens[key].append(now)
            return True


# --- Circuit Breaker ---

class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """
    Circuit breaker pattern for external service calls.
    Prevents cascading failures by failing fast when a dependency is down.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ):
        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, func: Callable, *args: Any, **kwargs: Any) -> Any:
        """Execute function through the circuit breaker."""
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.monotonic() - self._last_failure_time > self._recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    logger.info("circuit_breaker.half_open", name=self.name)
                else:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker '{self.name}' is OPEN"
                    )

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self._half_open_max_calls:
                    raise CircuitBreakerOpenError(
                        f"Circuit breaker '{self.name}' is HALF_OPEN, max test calls reached"
                    )
                self._half_open_calls += 1

        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            async with self._lock:
                self._failure_count = 0
                if self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.CLOSED
                    logger.info("circuit_breaker.closed", name=self.name)

            return result

        except Exception as e:
            async with self._lock:
                self._failure_count += 1
                self._last_failure_time = time.monotonic()

                if self._failure_count >= self._failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        "circuit_breaker.opened",
                        name=self.name,
                        failures=self._failure_count,
                    )

            raise


class CircuitBreakerOpenError(Exception):
    """Raised when a call is rejected by an open circuit breaker."""
    pass


# --- Idempotency Key Store ---

class IdempotencyStore:
    """
    In-memory idempotency key store.
    For production, back with Redis for distributed deduplication.
    """

    def __init__(self, ttl_seconds: float = 3600):
        self._store: dict[str, tuple[Any, float]] = {}
        self._ttl = ttl_seconds

    async def get(self, key: str) -> Any | None:
        if key in self._store:
            value, created_at = self._store[key]
            if time.monotonic() - created_at < self._ttl:
                return value
            else:
                del self._store[key]
        return None

    async def set(self, key: str, value: Any) -> None:
        self._store[key] = (value, time.monotonic())
