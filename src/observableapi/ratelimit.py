"""Per-client rate limiting: a weighted sliding-window counter, in Redis, in one Lua call.

**Why not a fixed window.** A fixed 60-request/minute window lets a client send 60 requests
at 11:59:59 and 60 more at 12:00:00 -- 120 requests in one second, twice the limit, at every
window boundary. ``tests/test_ratelimit.py::test_fixed_window_boundary_burst_is_rejected``
demonstrates that failure against this limiter, rather than asserting it in a comment.

**Why not a sliding window log.** Exact, but it stores one sorted-set member per request:
memory grows with traffic, and a client being throttled hardest costs the most to store.
The weighted counter keeps two integers per client regardless of volume, at the cost of
assuming traffic was evenly distributed within the previous window.

**Why the clock comes from Redis.** ``redis.call('TIME')`` inside the script means every
API worker shares one clock. With the timestamp computed in Python instead, two workers
whose clocks differ by a second disagree about which bucket a request belongs to, and the
limit silently becomes wrong under exactly the multi-worker deployment that makes a shared
limiter necessary in the first place.

**Why the hash tag.** Both bucket keys are derived inside the script, which is normally
unsafe under Redis Cluster (the client cannot see which slots will be touched). Wrapping
the client id in a hash tag -- ``rl:{<client>}:<bucket>`` -- forces both keys into the same
slot, so the script stays single-slot and cluster-safe.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

# KEYS[1] = key prefix, already hash-tagged. ARGV[1] = limit, ARGV[2] = window seconds.
# Returns {allowed, remaining, reset_ms, limit}.
SLIDING_WINDOW_LUA = """
local prefix = KEYS[1]
local limit  = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local window_ms = window * 1000

local t = redis.call('TIME')
local now_ms = (tonumber(t[1]) * 1000) + math.floor(tonumber(t[2]) / 1000)
local bucket = math.floor(now_ms / window_ms)
local elapsed = (now_ms % window_ms) / window_ms

local cur_key  = prefix .. ':' .. bucket
local prev_key = prefix .. ':' .. (bucket - 1)

local cur  = tonumber(redis.call('GET', cur_key)  or '0')
local prev = tonumber(redis.call('GET', prev_key) or '0')

local estimate = (prev * (1 - elapsed)) + cur
local reset_ms = ((bucket + 1) * window_ms) - now_ms

if (estimate + 1) > limit then
  return {0, 0, reset_ms, limit}
end

cur = redis.call('INCR', cur_key)
redis.call('PEXPIRE', cur_key, window_ms * 2)

estimate = (prev * (1 - elapsed)) + cur
local remaining = math.max(limit - estimate, 0)
return {1, math.floor(remaining), reset_ms, limit}
"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int

    def headers(self) -> dict[str, str]:
        """The headers every response carries, throttled or not.

        Clients can only back off politely if they are told the budget *before* they run
        out, so these go on successful responses too, not just on the 429.
        """
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_seconds),
        }
        if not self.allowed:
            headers["Retry-After"] = str(self.reset_seconds)
        return headers


class RateLimiter:
    """Weighted sliding-window limiter backed by Redis."""

    def __init__(self, redis: Redis, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._limit = limit
        self._window = window_seconds
        self._script = redis.register_script(SLIDING_WINDOW_LUA)

    async def check(self, client_id: str) -> RateLimitResult:
        allowed, remaining, reset_ms, limit = await self._script(
            keys=[f"rl:{{{client_id}}}"],
            args=[self._limit, self._window],
        )
        return RateLimitResult(
            allowed=bool(allowed),
            limit=int(limit),
            remaining=int(remaining),
            # Rounded up: a Retry-After of 0 invites an immediate retry that is still
            # inside the window and gets thrown away.
            reset_seconds=max(1, -(-int(reset_ms) // 1000)),
        )
