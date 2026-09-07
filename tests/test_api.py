"""End-to-end HTTP behaviour, through the real middleware stack against a real Redis."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis

from conftest import TEST_SPEC, client_for, make_settings
from observableapi.app import create_app
from observableapi.warehouse import GeneratorSpec, Warehouse, cohort_weeks

FIRST_COHORT = cohort_weeks(TEST_SPEC)[0].isoformat()


async def test_list_cohorts(client: AsyncClient, spec: GeneratorSpec) -> None:
    response = await client.get("/cohorts")

    assert response.status_code == 200
    cohorts = response.json()["cohorts"]
    assert len(cohorts) == spec.weeks
    assert cohorts[0]["users"] == spec.users_per_week


async def test_retention_curve(client: AsyncClient, spec: GeneratorSpec) -> None:
    response = await client.get(f"/cohorts/{FIRST_COHORT}/retention")

    assert response.status_code == 200
    body = response.json()
    assert body["cohort_week"] == FIRST_COHORT
    assert body["cohort_size"] == spec.users_per_week
    assert body["curve"][0]["retention_pct"] == 100.0


async def test_funnel_with_and_without_a_channel(client: AsyncClient) -> None:
    everyone = await client.get(f"/cohorts/{FIRST_COHORT}/funnel")
    organic = await client.get(f"/cohorts/{FIRST_COHORT}/funnel", params={"channel": "organic"})

    assert everyone.status_code == organic.status_code == 200
    assert [s["step"] for s in everyone.json()["steps"]] == [
        "signup",
        "activated",
        "quiz_completed",
        "subscribed",
    ]
    assert organic.json()["channel"] == "organic"
    assert organic.json()["steps"][0]["users"] < everyone.json()["steps"][0]["users"]


async def test_unknown_cohort_is_404(client: AsyncClient) -> None:
    response = await client.get("/cohorts/2001-01-01/retention")

    assert response.status_code == 404
    assert "2001-01-01" in response.json()["detail"]


async def test_malformed_date_is_422_not_500(client: AsyncClient) -> None:
    response = await client.get("/cohorts/not-a-date/retention")

    assert response.status_code == 422


async def test_healthz_reports_dependencies(client: AsyncClient, spec: GeneratorSpec) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redis": "up", "cohorts": spec.weeks}


async def test_health_is_degraded_not_down_when_redis_is_gone(warehouse: Warehouse) -> None:
    """Redis being unreachable must not take the API's health check down with it."""
    dead = Redis.from_url(
        "redis://127.0.0.1:6399/0", decode_responses=True, socket_connect_timeout=0.2
    )
    app = create_app(
        settings=make_settings(rate_limit_enabled=False),
        warehouse=warehouse,
        redis=dead,
        registry=CollectorRegistry(),
    )

    async with client_for(app) as client:
        response = await client.get("/healthz")
        served = await client.get(f"/cohorts/{FIRST_COHORT}/retention")

    assert response.json()["status"] == "degraded"
    assert response.json()["redis"] == "down"
    assert served.status_code == 200, "the API must keep serving with the cache unavailable"
    await dead.aclose()


async def test_second_request_is_served_from_cache(client: AsyncClient) -> None:
    url = f"/cohorts/{FIRST_COHORT}/retention"

    first = await client.get(url)
    second = await client.get(url)
    metrics = (await client.get("/metrics")).text

    route = "/cohorts/{cohort_week}/retention"
    assert first.json() == second.json()
    assert f'cache_operations_total{{result="miss",route="{route}"}} 1.0' in metrics
    assert f'cache_operations_total{{result="hit",route="{route}"}} 1.0' in metrics


async def test_every_response_carries_a_request_id(client: AsyncClient) -> None:
    response = await client.get("/cohorts")

    assert len(response.headers["X-Request-ID"]) == 32


async def test_an_inbound_request_id_is_preserved(client: AsyncClient) -> None:
    """Correlation across services only works if an existing id is carried, not replaced."""
    response = await client.get("/cohorts", headers={"X-Request-ID": "trace-me-123"})

    assert response.headers["X-Request-ID"] == "trace-me-123"


async def test_rate_limit_headers_are_on_successful_responses(
    redis: Redis, warehouse: Warehouse
) -> None:
    app = create_app(
        settings=make_settings(rate_limit_requests=5, rate_limit_window_seconds=60),
        warehouse=warehouse,
        redis=redis,
        registry=CollectorRegistry(),
    )

    async with client_for(app) as client:
        response = await client.get("/cohorts")

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "4"
    assert int(response.headers["X-RateLimit-Reset"]) > 0
    assert "Retry-After" not in response.headers


async def test_exceeding_the_limit_returns_429_with_retry_after(
    redis: Redis, warehouse: Warehouse
) -> None:
    app = create_app(
        settings=make_settings(rate_limit_requests=3, rate_limit_window_seconds=60),
        warehouse=warehouse,
        redis=redis,
        registry=CollectorRegistry(),
    )

    async with client_for(app) as client:
        statuses = [(await client.get("/cohorts")).status_code for _ in range(5)]
        throttled = await client.get("/cohorts")
        metrics = (await client.get("/metrics")).text

    assert statuses == [200, 200, 200, 429, 429]
    assert throttled.json()["detail"] == "Rate limit exceeded."
    assert int(throttled.headers["Retry-After"]) >= 1
    assert throttled.headers["X-RateLimit-Remaining"] == "0"
    # Throttled requests are still counted, and against the route template -- not the raw
    # path, and not lumped into "unmatched".
    assert 'rate_limit_decisions_total{decision="throttled",route="/cohorts"}' in metrics
    assert 'http_requests_total{method="GET",route="/cohorts",status="429"}' in metrics


async def test_health_and_metrics_are_not_rate_limited(redis: Redis, warehouse: Warehouse) -> None:
    """Locking yourself out of your own telemetry during a traffic spike is the worst time."""
    app = create_app(
        settings=make_settings(rate_limit_requests=1, rate_limit_window_seconds=60),
        warehouse=warehouse,
        redis=redis,
        registry=CollectorRegistry(),
    )

    async with client_for(app) as client:
        health = [(await client.get("/healthz")).status_code for _ in range(5)]
        metrics = [(await client.get("/metrics")).status_code for _ in range(5)]

    assert health == [200] * 5
    assert metrics == [200] * 5


async def test_rate_limit_is_shared_across_app_instances(
    redis: Redis, warehouse: Warehouse
) -> None:
    """Two app instances on one Redis share a budget -- an in-process counter would not.

    This is the test that fails if the limiter is ever "simplified" to a local dict, which
    is the single most common way this feature is got wrong.
    """
    settings = make_settings(rate_limit_requests=4, rate_limit_window_seconds=60)
    apps = [
        create_app(
            settings=settings, warehouse=warehouse, redis=redis, registry=CollectorRegistry()
        )
        for _ in range(2)
    ]

    statuses = []
    async with client_for(apps[0]) as a, client_for(apps[1]) as b:
        for _ in range(3):
            statuses.append((await a.get("/cohorts")).status_code)
            statuses.append((await b.get("/cohorts")).status_code)

    assert statuses.count(200) == 4
    assert statuses.count(429) == 2


async def test_metrics_use_route_templates_not_concrete_paths(client: AsyncClient) -> None:
    """The cardinality guarantee: N cohorts must not create N time series."""
    for cohort in cohort_weeks(TEST_SPEC):
        await client.get(f"/cohorts/{cohort.isoformat()}/retention")

    metrics = (await client.get("/metrics")).text

    assert 'route="/cohorts/{cohort_week}/retention"' in metrics
    assert "2026-01-05" not in metrics
    assert metrics.count('http_requests_total{method="GET",route="/cohorts/{cohort_week}') == 1


async def test_metrics_expose_the_red_signals(client: AsyncClient) -> None:
    await client.get("/cohorts")
    await client.get("/cohorts/2001-01-01/retention")

    metrics = (await client.get("/metrics")).text

    assert "http_requests_total" in metrics  # rate and errors
    assert "http_request_duration_seconds_bucket" in metrics  # duration
    assert "warehouse_query_duration_seconds_bucket" in metrics
    assert "http_requests_in_flight" in metrics
    route = "/cohorts/{cohort_week}/retention"
    assert f'http_requests_total{{method="GET",route="{route}",status="404"}} 1.0' in metrics


async def test_latency_histogram_separates_hits_from_misses(client: AsyncClient) -> None:
    """The label that makes 'p95 got worse' answerable: was it slower, or did hits drop?"""
    url = f"/cohorts/{FIRST_COHORT}/funnel"
    await client.get(url)
    await client.get(url)

    metrics = (await client.get("/metrics")).text

    assert 'cache="miss"' in metrics
    assert 'cache="hit"' in metrics


async def test_concurrent_requests_for_one_cohort_query_the_warehouse_once(
    client: AsyncClient,
) -> None:
    """Single-flight, end to end: 12 simultaneous requests, one DuckDB aggregation."""
    url = f"/cohorts/{cohort_weeks(TEST_SPEC)[2].isoformat()}/retention"

    responses = await asyncio.gather(*(client.get(url) for _ in range(12)))
    metrics = (await client.get("/metrics")).text

    assert all(r.status_code == 200 for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)
    assert 'warehouse_query_duration_seconds_count{query="retention"} 1.0' in metrics


async def test_funnel_404_names_the_channel_when_one_was_given(client: AsyncClient) -> None:
    response = await client.get(
        f"/cohorts/{FIRST_COHORT}/funnel", params={"channel": "carrier_pigeon"}
    )

    assert response.status_code == 404
    assert "carrier_pigeon" in response.json()["detail"]


async def test_app_opens_and_closes_its_own_dependencies(tmp_path, monkeypatch) -> None:
    """The wiring used in production: no injected Redis, no injected warehouse.

    Every other test injects both, so without this the real startup and shutdown path --
    opening the DuckDB file, closing it, closing the Redis pool -- would never run.
    """
    from observableapi.config import get_settings
    from observableapi.warehouse import GeneratorSpec, build

    warehouse_path = tmp_path / "wh.duckdb"
    build(warehouse_path, GeneratorSpec(weeks=2, users_per_week=30, max_followup_weeks=2))
    monkeypatch.setenv("WAREHOUSE_PATH", str(warehouse_path))
    monkeypatch.setenv("REDIS_URL", make_settings().redis_url)
    get_settings.cache_clear()

    try:
        app = create_app(registry=CollectorRegistry())
        async with client_for(app) as client:
            response = await client.get("/cohorts")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    assert len(response.json()["cohorts"]) == 2


async def test_funnel_404_for_an_unknown_cohort(client: AsyncClient) -> None:
    response = await client.get("/cohorts/2001-01-01/funnel")

    assert response.status_code == 404
    assert response.json()["detail"] == "No cohort starting 2001-01-01."


def test_build_default_app_is_a_working_uvicorn_factory(monkeypatch) -> None:
    """``uvicorn observableapi.app:build_default_app --factory`` resolves this name.

    Nothing else calls it, so without this test the documented way to serve the app could
    break and every other test would still pass.
    """
    from observableapi.app import build_default_app
    from observableapi.config import get_settings

    monkeypatch.setenv("REDIS_URL", make_settings().redis_url)
    get_settings.cache_clear()
    try:
        app = build_default_app()
    finally:
        get_settings.cache_clear()

    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert "/cohorts/{cohort_week}/retention" in paths
    assert "/metrics" in paths
