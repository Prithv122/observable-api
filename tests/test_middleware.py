"""Middleware behaviour that the endpoint tests cannot reach: failures and edge labels."""

from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry, generate_latest
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request

from conftest import client_for, make_settings
from observableapi.app import create_app
from observableapi.logging_setup import configure_logging
from observableapi.metrics import Metrics, exposition_registry
from observableapi.middleware import ObservabilityMiddleware, client_id, route_template
from observableapi.warehouse import Warehouse


def make_request(path: str, headers: dict[str, str] | None = None, client=("1.2.3.4", 1234)):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": Headers(headers or {}).raw,
            "client": client,
            "app": FastAPI(),
        }
    )


def test_client_id_prefers_an_api_key() -> None:
    assert client_id(make_request("/x", {"x-api-key": "abc"})) == "key:abc"


def test_client_id_falls_back_to_the_source_address() -> None:
    assert client_id(make_request("/x")) == "ip:1.2.3.4"


def test_client_id_survives_a_missing_peer() -> None:
    """ASGI does not guarantee a client address; the limiter still needs a bucket."""
    assert client_id(make_request("/x", client=None)) == "ip:unknown"


def test_route_template_of_an_unknown_path_is_a_constant() -> None:
    """404s on random URLs must collapse to one series, not one per probed path."""
    assert route_template(make_request("/nothing/here")) == "unmatched"


async def test_unhandled_exceptions_are_counted_logged_and_re_raised(
    redis, warehouse: Warehouse
) -> None:
    """A 500 must still appear in the metrics -- that is the 'E' in RED.

    The exception is re-raised rather than swallowed, so the ASGI server's own error
    handling still runs; the middleware only observes it on the way past.
    """
    buffer = io.StringIO()
    metrics = Metrics(CollectorRegistry())
    app = FastAPI(middleware=[Middleware(ObservabilityMiddleware, metrics=metrics)])
    app.state.metrics = metrics

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("kaboom")

    configure_logging("INFO", json_output=True, stream=buffer)
    try:
        transport = ASGITransport(app=app, raise_app_exceptions=True)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            with pytest.raises(RuntimeError, match="kaboom"):
                await client.get("/boom")
    finally:
        configure_logging("INFO", json_output=True)

    samples = {
        (s.labels.get("status"), s.name)
        for metric in metrics.registry.collect()
        for s in metric.samples
    }
    assert ("500", "http_requests_total") in samples

    events = [json.loads(line) for line in buffer.getvalue().splitlines() if line.startswith("{")]
    failures = [e for e in events if e["event"] == "request_failed"]
    assert failures and failures[0]["path"] == "/boom"
    assert "kaboom" in failures[0]["exception"]

    # The in-flight gauge must come back down even when the request explodes; a leaked
    # gauge reads as permanent load and can trip an autoscaler.
    in_flight = [
        s.value
        for metric in metrics.registry.collect()
        for s in metric.samples
        if s.name == "http_requests_in_flight"
    ]
    assert in_flight == [0.0]


async def test_scrapes_do_not_count_themselves(redis, warehouse: Warehouse) -> None:
    """``/metrics`` is excluded from request metrics, or every panel gains a constant."""
    app = create_app(
        settings=make_settings(),
        warehouse=warehouse,
        redis=redis,
        registry=CollectorRegistry(),
    )

    async with client_for(app) as client:
        for _ in range(3):
            await client.get("/metrics")
        body = (await client.get("/metrics")).text

    assert 'route="/metrics"' not in body


def test_exposition_registry_is_the_local_one_without_multiprocess_mode(monkeypatch) -> None:
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    registry = CollectorRegistry()

    assert exposition_registry(registry) is registry


def test_exposition_registry_aggregates_across_workers_in_multiprocess_mode(
    tmp_path, monkeypatch
) -> None:
    """With several workers, /metrics must read every worker's files, not just its own.

    A single worker's registry answering the scrape is what made consecutive scrapes of the
    4-worker stack disagree (97, then 74, for the same counter). Here the returned registry
    must be a different, aggregating one -- and must render without error.
    """
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    registry = CollectorRegistry()

    aggregated = exposition_registry(registry)

    assert aggregated is not registry
    assert generate_latest(aggregated) == b""  # empty dir, but a working collector
