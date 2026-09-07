"""The rate limiter, against a real Redis.

The boundary-burst test is the one that matters. Everything else here would pass against a
fixed-window limiter too.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from redis.asyncio import Redis

from observableapi.ratelimit import RateLimiter


async def test_allows_up_to_the_limit_then_throttles(redis: Redis) -> None:
    limiter = RateLimiter(redis, limit=5, window_seconds=60)

    results = [await limiter.check("alice") for _ in range(7)]

    assert [r.allowed for r in results] == [True] * 5 + [False] * 2


async def test_remaining_counts_down_and_floors_at_zero(redis: Redis) -> None:
    limiter = RateLimiter(redis, limit=3, window_seconds=60)

    remaining = [(await limiter.check("bob")).remaining for _ in range(5)]

    assert remaining == [2, 1, 0, 0, 0]


async def test_clients_have_independent_budgets(redis: Redis) -> None:
    limiter = RateLimiter(redis, limit=2, window_seconds=60)

    for _ in range(3):
        await limiter.check("noisy")
    quiet = await limiter.check("quiet")

    assert quiet.allowed
    assert quiet.remaining == 1


async def test_headers_describe_the_budget(redis: Redis) -> None:
    limiter = RateLimiter(redis, limit=2, window_seconds=60)

    ok = await limiter.check("carol")
    await limiter.check("carol")
    throttled = await limiter.check("carol")

    assert ok.headers() == {
        "X-RateLimit-Limit": "2",
        "X-RateLimit-Remaining": "1",
        "X-RateLimit-Reset": ok.headers()["X-RateLimit-Reset"],
    }
    assert "Retry-After" not in ok.headers()
    assert throttled.headers()["Retry-After"] == str(throttled.reset_seconds)


async def test_retry_after_is_never_zero(redis: Redis) -> None:
    """A ``Retry-After: 0`` invites an immediate retry that is still inside the window."""
    limiter = RateLimiter(redis, limit=1, window_seconds=1)

    await limiter.check("dave")
    # Land as late in the window as possible, where the naive floor would produce 0.
    await asyncio.sleep(max(0.0, 1 - (time.time() % 1) - 0.01))
    throttled = await limiter.check("dave")

    if not throttled.allowed:
        assert throttled.reset_seconds >= 1


async def test_concurrent_requests_cannot_exceed_the_limit(redis: Redis) -> None:
    """Check-and-increment is one Lua call, so 50 racing requests cannot all read '0'.

    A read-then-write limiter in Python passes the sequential tests above and fails this one.
    """
    limiter = RateLimiter(redis, limit=10, window_seconds=60)

    results = await asyncio.gather(*(limiter.check("swarm") for _ in range(50)))

    assert sum(r.allowed for r in results) == 10


@pytest.mark.parametrize("window", [4])
async def test_fixed_window_boundary_burst_is_rejected(redis: Redis, window: int) -> None:
    """The failure this limiter exists to prevent.

    A fixed window lets a client spend its whole budget at the end of one window and its
    whole budget again immediately after the boundary -- 2x the limit in a moment. The
    weighted counter carries the previous window's count forward, so the second burst is
    refused.
    """
    limit = 10
    limiter = RateLimiter(redis, limit=limit, window_seconds=window)

    # Sit ~60% into the current window, then spend the entire budget.
    while not (0.55 <= (time.time() % window) / window <= 0.65):
        await asyncio.sleep(0.01)
    before = sum([(await limiter.check("burst")).allowed for _ in range(limit)])

    # Cross into the next window and immediately try to spend it again.
    while (time.time() % window) / window > 0.05:
        await asyncio.sleep(0.005)
    after = sum([(await limiter.check("burst")).allowed for _ in range(limit)])

    assert before == limit
    assert after == 0, "the previous window's count must carry across the boundary"
    assert before + after == limit, "a fixed window would have allowed 2x the limit here"
