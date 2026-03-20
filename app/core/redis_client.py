"""
Redis client management for feature store, sequence cache, and rate limiting.
Uses separate Redis databases for isolation.
"""

from __future__ import annotations

import redis.asyncio as redis

from app.core.config import get_settings

_pools: dict[str, redis.Redis] = {}


def _get_redis(url: str, db: int, max_connections: int) -> redis.Redis:
    key = f"{url}:{db}"
    if key not in _pools:
        _pools[key] = redis.from_url(
            url,
            db=db,
            max_connections=max_connections,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
        )
    return _pools[key]


def get_feature_store() -> redis.Redis:
    """Redis instance for real-time feature vectors (merchant + card profiles)."""
    settings = get_settings()
    return _get_redis(
        settings.redis_url,
        db=settings.redis_feature_store_db,
        max_connections=settings.redis_max_connections,
    )


def get_sequence_cache() -> redis.Redis:
    """Redis instance for behavioral sequence data (recent txn patterns)."""
    settings = get_settings()
    return _get_redis(
        settings.redis_url,
        db=settings.redis_sequence_cache_db,
        max_connections=settings.redis_max_connections,
    )


def get_general_redis() -> redis.Redis:
    """Redis instance for rate limiting, locks, and general caching."""
    settings = get_settings()
    return _get_redis(
        settings.redis_url,
        db=0,
        max_connections=settings.redis_max_connections,
    )


async def close_all_pools() -> None:
    for pool in _pools.values():
        await pool.aclose()
    _pools.clear()
