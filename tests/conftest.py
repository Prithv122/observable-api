"""Shared fixtures.

Two deliberate choices here:

**Redis is real, never mocked.** Every interesting property of the limiter and the cache --
atomicity of check-and-increment, the lock's compare-and-delete, ``TIME`` inside a script,
TTL behaviour -- lives in Lua running inside Redis. A fake would test the mock's opinion of
Redis, which is exactly the part that has bugs. CI runs a Redis service container for this.

**The lifespan is driven by hand.** ``httpx.ASGITransport`` does not run lifespan events, so
without ``lifespan_app`` below the tests would exercise an app whose startup path never ran
-- and startup is where the warehouse is opened.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis

from observableapi.app import create_app
from observableapi.config import Settings
from observableapi.warehouse import GeneratorSpec, Warehouse, build_in_memory

# Port 6380 keeps the test Redis clear of a default local install; CI overrides this.
REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6380/15")

# Small enough that a full rebuild per session is instant, large enough that the retention
# curve has several non-empty weeks to assert on.
TEST_SPEC = GeneratorSpec(weeks=4, users_per_week=120, max_followup_weeks=5)


@pytest.fixture(scope="session")
def spec() -> GeneratorSpec:
    return TEST_SPEC


@pytest.fixture(scope="session")
def warehouse(spec: GeneratorSpec) -> Warehouse:
    wh = Warehouse(build_in_memory(spec))
    yield wh
    wh.close()


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - infrastructure failure, not a test path
        pytest.fail(
            f"Redis is required for these tests and is not reachable at {REDIS_URL} ({exc}). "
            "Start one with: docker run -d --rm -p 6380:6379 redis:8-alpine"
        )
    # Flushed before *and* after: a test that fails midway must not poison the next one.
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@asynccontextmanager
async def lifespan_app(app: FastAPI) -> AsyncIterator[None]:
    """Drive the ASGI lifespan protocol the way a real server does.

    The shutdown message is only queued when the body of the ``with`` is finished. Queueing
    both messages up front instead makes the app's ``receive()`` return shutdown the instant
    startup completes, so the app tears itself down while the test is still using it -- an
    app that injects all its dependencies never notices, and one that opens its own DuckDB
    connection fails with "Connection already closed". See NOTES.md.
    """
    messages: asyncio.Queue[dict] = asyncio.Queue()
    await messages.put({"type": "lifespan.startup"})
    started: asyncio.Event = asyncio.Event()

    async def receive() -> dict:
        return await messages.get()

    async def send(message: dict) -> None:
        if message["type"] in {"lifespan.startup.complete", "lifespan.startup.failed"}:
            started.set()
        if message["type"].endswith(".failed"):  # pragma: no cover - startup failure path
            raise RuntimeError(f"lifespan failed: {message}")

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    # Wait for startup, but never outlive a lifespan that crashed before reporting.
    await asyncio.wait(
        [asyncio.ensure_future(started.wait()), task], return_when=asyncio.FIRST_COMPLETED
    )
    try:
        yield
    finally:
        await messages.put({"type": "lifespan.shutdown"})
        await task


def make_settings(redis_url: str = REDIS_URL, **overrides) -> Settings:
    defaults = {
        "redis_url": redis_url,
        "rate_limit_requests": 1000,
        "rate_limit_window_seconds": 60,
        "cache_ttl_seconds": 30,
        "log_json": True,
    }
    return Settings(**{**defaults, **overrides})


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        lifespan_app(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        yield client


@pytest_asyncio.fixture
async def app(redis: Redis, warehouse: Warehouse) -> FastAPI:
    """The real app -- real middleware stack, real Redis, in-memory warehouse."""
    return create_app(
        settings=make_settings(),
        warehouse=warehouse,
        redis=redis,
        registry=CollectorRegistry(),
    )


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with client_for(app) as c:
        yield c
