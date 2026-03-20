"""
Unit tests for utility modules: rate limiter, circuit breaker, idempotency store.
"""

from __future__ import annotations

import asyncio

import pytest

from app.utils import CircuitBreaker, CircuitBreakerOpenError, CircuitState, IdempotencyStore, RateLimiter


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_within_limit(self):
        rl = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            assert await rl.allow("key1") is True

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self):
        rl = RateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            await rl.allow("key1")
        assert await rl.allow("key1") is False

    @pytest.mark.asyncio
    async def test_separate_keys_independent(self):
        rl = RateLimiter(max_requests=2, window_seconds=60)
        assert await rl.allow("key1") is True
        assert await rl.allow("key1") is True
        assert await rl.allow("key1") is False
        # Different key should still be allowed
        assert await rl.allow("key2") is True

    @pytest.mark.asyncio
    async def test_window_expiry(self):
        rl = RateLimiter(max_requests=1, window_seconds=0.05)
        assert await rl.allow("key1") is True
        assert await rl.allow("key1") is False
        await asyncio.sleep(0.06)
        assert await rl.allow("key1") is True


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_closed_state_passes_through(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        assert cb.state == CircuitState.CLOSED

        result = await cb.call(asyncio.coroutine(lambda: "ok"))
        assert result == "ok"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_opens_after_failure_threshold(self):
        cb = CircuitBreaker("test", failure_threshold=2)

        async def failing():
            raise ValueError("boom")

        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.call(failing)

        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_state_rejects_immediately(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=60)

        async def failing():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await cb.call(failing)

        assert cb.state == CircuitState.OPEN

        with pytest.raises(CircuitBreakerOpenError):
            await cb.call(failing)

    @pytest.mark.asyncio
    async def test_half_open_recovery(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.05, half_open_max_calls=1)

        async def failing():
            raise ValueError("boom")

        async def succeeding():
            return "recovered"

        with pytest.raises(ValueError):
            await cb.call(failing)

        assert cb.state == CircuitState.OPEN
        await asyncio.sleep(0.06)

        result = await cb.call(succeeding)
        assert result == "recovered"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.05, half_open_max_calls=1)

        async def failing():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await cb.call(failing)

        await asyncio.sleep(0.06)

        # Fail again during half-open
        with pytest.raises(ValueError):
            await cb.call(failing)

        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        cb = CircuitBreaker("test", failure_threshold=3)

        async def failing():
            raise ValueError("boom")

        async def succeeding():
            return "ok"

        # Two failures
        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.call(failing)

        # One success resets count
        await cb.call(succeeding)

        # Two more failures should not trip it (count reset)
        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.call(failing)

        assert cb.state == CircuitState.CLOSED  # Still closed (only 2 since reset)


class TestIdempotencyStore:
    @pytest.mark.asyncio
    async def test_set_and_get(self):
        store = IdempotencyStore(ttl_seconds=60)
        await store.set("key1", {"result": "value"})
        result = await store.get("key1")
        assert result == {"result": "value"}

    @pytest.mark.asyncio
    async def test_missing_key_returns_none(self):
        store = IdempotencyStore()
        assert await store.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_expired_key_returns_none(self):
        store = IdempotencyStore(ttl_seconds=0.05)
        await store.set("key1", "value")
        await asyncio.sleep(0.06)
        assert await store.get("key1") is None

    @pytest.mark.asyncio
    async def test_overwrite_key(self):
        store = IdempotencyStore()
        await store.set("key1", "first")
        await store.set("key1", "second")
        assert await store.get("key1") == "second"
