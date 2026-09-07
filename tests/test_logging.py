"""Structured logging, and the property that makes it worth having.

The claim being tested is not "we emit JSON". It is that a log line written by a module
which knows nothing about HTTP still carries the id of the request that caused it -- because
that is the difference between grepping one request's story out of a busy log and not being
able to.
"""

from __future__ import annotations

import io
import json

import pytest
import structlog
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis

from conftest import client_for, make_settings
from observableapi.app import create_app
from observableapi.logging_setup import configure_logging
from observableapi.warehouse import Warehouse


@pytest.fixture
def log_stream() -> io.StringIO:
    """Point structlog at a buffer for the duration of one test, then put it back."""
    buffer = io.StringIO()
    configure_logging("INFO", json_output=True, stream=buffer)
    yield buffer
    configure_logging("INFO", json_output=True)


def read_events(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.startswith("{")]


def test_logs_are_json_with_level_and_timestamp(log_stream: io.StringIO) -> None:
    structlog.get_logger("t").info("something_happened", answer=42)

    (event,) = read_events(log_stream)
    assert event["event"] == "something_happened"
    assert event["answer"] == 42
    assert event["level"] == "info"
    assert event["timestamp"].endswith("Z")


def test_context_is_not_leaked_between_requests(log_stream: io.StringIO) -> None:
    structlog.contextvars.bind_contextvars(request_id="first")
    structlog.get_logger("t").info("one")
    structlog.contextvars.clear_contextvars()
    structlog.get_logger("t").info("two")

    first, second = read_events(log_stream)
    assert first["request_id"] == "first"
    assert "request_id" not in second


async def test_request_lines_carry_the_request_id_and_timing(
    log_stream: io.StringIO, redis: Redis, warehouse: Warehouse
) -> None:
    app = create_app(
        settings=make_settings(),
        warehouse=warehouse,
        redis=redis,
        registry=CollectorRegistry(),
    )
    # create_app reconfigures logging to stdout; point it back at the buffer.
    configure_logging("INFO", json_output=True, stream=log_stream)

    async with client_for(app) as client:
        await client.get("/cohorts", headers={"X-Request-ID": "req-abc"})

    completed = [e for e in read_events(log_stream) if e["event"] == "request_completed"]
    assert len(completed) == 1
    assert completed[0]["request_id"] == "req-abc"
    assert completed[0]["route"] == "/cohorts"
    assert completed[0]["status"] == 200
    assert completed[0]["cache"] in {"hit", "miss", "coalesced"}
    assert isinstance(completed[0]["duration_ms"], float)
    assert completed[0]["method"] == "GET"


async def test_a_nested_layer_logs_with_the_same_request_id(
    log_stream: io.StringIO, warehouse: Warehouse
) -> None:
    """``cache.py`` never sees the Request object, yet its warning is still correlated.

    This is the contextvars claim, tested through a real failure rather than a stub: with
    Redis unreachable the cache logs ``cache_unavailable`` from deep inside the call stack,
    and that line must carry the same request id as the access line for the request that
    triggered it.
    """
    dead = Redis.from_url(
        "redis://127.0.0.1:6399/0", decode_responses=True, socket_connect_timeout=0.2
    )
    app = create_app(
        settings=make_settings(rate_limit_enabled=False),
        warehouse=warehouse,
        redis=dead,
        registry=CollectorRegistry(),
    )
    configure_logging("INFO", json_output=True, stream=log_stream)

    async with client_for(app) as client:
        response = await client.get("/cohorts", headers={"X-Request-ID": "trace-42"})

    assert response.status_code == 200
    events = read_events(log_stream)
    from_cache = [e for e in events if e["event"] == "cache_unavailable"]
    from_middleware = [e for e in events if e["event"] == "request_completed"]

    assert from_cache, "the cache layer should have reported that Redis was unreachable"
    assert from_cache[0]["request_id"] == "trace-42"
    assert from_middleware[0]["request_id"] == "trace-42"
    await dead.aclose()


async def test_throttled_requests_are_logged(
    log_stream: io.StringIO, redis: Redis, warehouse: Warehouse
) -> None:
    app = create_app(
        settings=make_settings(rate_limit_requests=1, rate_limit_window_seconds=60),
        warehouse=warehouse,
        redis=redis,
        registry=CollectorRegistry(),
    )
    configure_logging("INFO", json_output=True, stream=log_stream)

    async with client_for(app) as client:
        await client.get("/cohorts")
        await client.get("/cohorts")

    throttled = [e for e in read_events(log_stream) if e["event"] == "rate_limited"]
    assert len(throttled) == 1
    assert throttled[0]["route"] == "/cohorts"
    assert throttled[0]["retry_after"] >= 1
    assert "request_id" in throttled[0]
