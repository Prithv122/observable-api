"""Redis-backed response cache with single-flight.

**Why single-flight.** The naive cache -- check, miss, compute, store -- is fine until the
entry for a hot key expires while 50 requests are in flight for it. All 50 miss, all 50 run
the same DuckDB aggregation, and the p99 spike lands precisely on the most popular key.
That is a cache stampede, and it gets worse the more traffic you have. Here the first
request to miss takes a short Redis lock and computes; the others wait for its result
instead of duplicating the work.

**Why it fails open.** Every Redis failure path in this module falls through to computing
the value directly. A cache is an optimisation; if it is also a hard dependency, adding it
has *lowered* availability. The ``error`` label on ``cache_operations_total`` is what makes
that degradation visible instead of silent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger(__name__)

# Release the lock only if we still hold it: a compare-and-delete, so a slow computation
# whose lock has already expired cannot delete the lock a *different* request now holds.
RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

HIT = "hit"
MISS = "miss"
COALESCED = "coalesced"  # waited on another request's computation instead of duplicating it
BYPASS = "bypass"
ERROR = "error"


def cache_key(route: str, params: dict[str, Any]) -> str:
    """Stable key for a route plus its parameters.

    Params are sorted before hashing so ``?a=1&b=2`` and ``?b=2&a=1`` are one cache entry,
    not two. Hashing keeps key length bounded regardless of query-string size.
    """
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"cache:{route}:{digest}"


class ResponseCache:
    def __init__(
        self,
        redis: Redis,
        ttl_seconds: int,
        enabled: bool = True,
        lock_timeout_ms: int = 5000,
        poll_interval_seconds: float = 0.01,
    ) -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._enabled = enabled
        self._lock_timeout_ms = lock_timeout_ms
        self._poll = poll_interval_seconds
        self._release = redis.register_script(RELEASE_LUA)

    async def get_or_compute(
        self, key: str, compute: Callable[[], Awaitable[Any]]
    ) -> tuple[Any, str]:
        """Return ``(value, outcome)`` where outcome is one of the module-level constants."""
        if not self._enabled:
            return await compute(), BYPASS

        try:
            cached = await self._redis.get(key)
            if cached is not None:
                return json.loads(cached), HIT

            lock_key = f"{key}:lock"
            # A fresh token per attempt. Anything derived from the callable (its id, say)
            # can repeat once the object is collected, and the compare-and-delete below
            # would then release a lock a different request is holding.
            token = uuid.uuid4().hex
            acquired = await self._redis.set(lock_key, token, nx=True, px=self._lock_timeout_ms)

            if not acquired:
                waited = await self._wait_for_value(key)
                if waited is not None:
                    return waited, COALESCED
                # The holder died, or took longer than the lock's lifetime. Compute rather
                # than fail: a slow answer beats no answer.
                return await compute(), MISS

            try:
                value = await compute()
                await self._redis.set(key, json.dumps(value, default=str), ex=self._ttl)
                return value, MISS
            finally:
                await self._release(keys=[lock_key], args=[token])

        except RedisError as exc:
            log.warning("cache_unavailable", error=str(exc), key=key)
            return await compute(), ERROR

    async def _wait_for_value(self, key: str) -> Any | None:
        """Poll briefly for the lock holder's result. Returns None if it never lands.

        Polling rather than pub/sub keeps this to one Redis connection and one code path;
        the wait is bounded by the lock TTL, so a crashed holder costs one lock timeout at
        worst, not an indefinite hang.
        """
        deadline = asyncio.get_running_loop().time() + (self._lock_timeout_ms / 1000)
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(self._poll)
            cached = await self._redis.get(key)
            if cached is not None:
                return json.loads(cached)
        return None
