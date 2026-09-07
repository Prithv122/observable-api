"""The response cache: hits, single-flight, and the behaviour when Redis is gone."""

from __future__ import annotations

import asyncio
import contextlib

from redis.asyncio import Redis

from observableapi.cache import (
    BYPASS,
    COALESCED,
    ERROR,
    HIT,
    MISS,
    ResponseCache,
    cache_key,
)


def test_cache_key_is_order_independent() -> None:
    """``?a=1&b=2`` and ``?b=2&a=1`` are the same request and must be one cache entry."""
    assert cache_key("/r", {"a": 1, "b": 2}) == cache_key("/r", {"b": 2, "a": 1})


def test_cache_key_separates_routes_and_values() -> None:
    assert cache_key("/r", {"a": 1}) != cache_key("/other", {"a": 1})
    assert cache_key("/r", {"a": 1}) != cache_key("/r", {"a": 2})


def test_cache_key_length_is_bounded() -> None:
    huge = cache_key("/r", {"q": "x" * 10_000})
    assert len(huge) < 100


async def test_miss_then_hit(redis: Redis) -> None:
    cache = ResponseCache(redis, ttl_seconds=30)
    calls = 0

    async def compute() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": 42}

    first, first_outcome = await cache.get_or_compute("k", compute)
    second, second_outcome = await cache.get_or_compute("k", compute)

    assert (first_outcome, second_outcome) == (MISS, HIT)
    assert first == second == {"value": 42}
    assert calls == 1


async def test_disabled_cache_always_bypasses(redis: Redis) -> None:
    cache = ResponseCache(redis, ttl_seconds=30, enabled=False)
    calls = 0

    async def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_compute("k", compute) == (1, BYPASS)
    assert await cache.get_or_compute("k", compute) == (2, BYPASS)
    assert await redis.get("k") is None


async def test_entry_expires_after_its_ttl(redis: Redis) -> None:
    cache = ResponseCache(redis, ttl_seconds=1)

    async def compute() -> str:
        return "v"

    await cache.get_or_compute("k", compute)
    assert 0 < await redis.ttl("k") <= 1


async def test_single_flight_collapses_a_stampede(redis: Redis) -> None:
    """20 concurrent misses on one key must run the expensive work exactly once.

    Without single-flight all 20 would compute, which is the cache stampede this design
    exists to prevent -- and it lands hardest on the most popular key.
    """
    cache = ResponseCache(redis, ttl_seconds=30, poll_interval_seconds=0.005)
    calls = 0

    async def slow_compute() -> dict[str, str]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.15)
        return {"expensive": "result"}

    results = await asyncio.gather(*(cache.get_or_compute("hot", slow_compute) for _ in range(20)))

    outcomes = [outcome for _, outcome in results]
    assert calls == 1, f"expected one computation, got {calls}"
    assert outcomes.count(MISS) == 1
    assert outcomes.count(COALESCED) == 19
    assert all(value == {"expensive": "result"} for value, _ in results)


async def test_waiter_computes_for_itself_if_the_holder_never_delivers(redis: Redis) -> None:
    """A crashed lock holder must not hang the requests waiting on it."""
    cache = ResponseCache(redis, ttl_seconds=30, lock_timeout_ms=100, poll_interval_seconds=0.01)
    await redis.set("orphan:lock", "someone-else", px=5000)
    calls = 0

    async def compute() -> str:
        nonlocal calls
        calls += 1
        return "computed anyway"

    value, outcome = await cache.get_or_compute("orphan", compute)

    assert (value, outcome) == ("computed anyway", MISS)
    assert calls == 1


async def test_cache_fails_open_when_redis_is_unreachable() -> None:
    """A cache that takes the API down with it has made availability worse, not better."""
    dead = Redis.from_url(
        "redis://127.0.0.1:6399/0", decode_responses=True, socket_connect_timeout=0.2
    )
    cache = ResponseCache(dead, ttl_seconds=30)

    async def compute() -> str:
        return "served from the source"

    value, outcome = await cache.get_or_compute("k", compute)

    assert (value, outcome) == ("served from the source", ERROR)
    await dead.aclose()


async def test_lock_is_released_after_a_failed_computation(redis: Redis) -> None:
    """A handler that raises must not leave the key locked for the lock's full TTL."""
    cache = ResponseCache(redis, ttl_seconds=30)

    async def boom() -> None:
        raise ValueError("handler blew up")

    with contextlib.suppress(ValueError):
        await cache.get_or_compute("fragile", boom)

    assert await redis.get("fragile:lock") is None
