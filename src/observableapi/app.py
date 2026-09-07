"""The FastAPI application.

Everything the app needs -- settings, Redis, the warehouse, the metric registry -- is
injected through ``create_app`` rather than reached for as a module global. That is what
lets the test suite run the real middleware stack, against a real Redis, with a small
in-memory warehouse and a fresh registry per test.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware

from .cache import ResponseCache, cache_key
from .config import Settings, get_settings
from .logging_setup import configure_logging
from .metrics import Metrics
from .middleware import (
    ObservabilityMiddleware,
    RateLimitMiddleware,
    metrics_response,
    route_template,
)
from .ratelimit import RateLimiter
from .warehouse import Warehouse

log = structlog.get_logger(__name__)


async def _measured(app: FastAPI, name: str, fn, *args: Any) -> Any:
    """Run a warehouse query in the threadpool, timed separately from HTTP overhead.

    DuckDB is synchronous and CPU-bound; calling it directly on the event loop would block
    every other in-flight request for the duration of the aggregation. Timing it on its own
    histogram is what makes "the database got slower" distinguishable from "we lost the
    cache" in the latency graphs.
    """
    metrics: Metrics = app.state.metrics
    with metrics.warehouse_query_duration.labels(name).time():
        return await run_in_threadpool(fn, *args)


def create_app(
    settings: Settings | None = None,
    warehouse: Warehouse | None = None,
    redis: Redis | None = None,
    registry: CollectorRegistry | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    metrics = Metrics(registry or CollectorRegistry())

    owns_redis = redis is None
    owns_warehouse = warehouse is None
    redis = redis or Redis.from_url(settings.redis_url, decode_responses=True)

    limiter = RateLimiter(redis, settings.rate_limit_requests, settings.rate_limit_window_seconds)
    cache = ResponseCache(redis, settings.cache_ttl_seconds, enabled=settings.cache_enabled)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.warehouse = warehouse or Warehouse.open(settings.warehouse_path)
        log.info(
            "startup",
            rate_limit=f"{settings.rate_limit_requests}/{settings.rate_limit_window_seconds}s",
            rate_limit_enabled=settings.rate_limit_enabled,
            cache_enabled=settings.cache_enabled,
            cache_ttl_seconds=settings.cache_ttl_seconds,
        )
        try:
            yield
        finally:
            if owns_warehouse:
                app.state.warehouse.close()
            if owns_redis:
                await redis.aclose()
            log.info("shutdown")

    middleware = [
        Middleware(ObservabilityMiddleware, metrics=metrics),
    ]
    if settings.rate_limit_enabled:
        middleware.append(Middleware(RateLimitMiddleware, limiter=limiter, metrics=metrics))

    app = FastAPI(
        title="Observable API",
        version="0.1.0",
        summary="Cohort retention and funnel metrics, served under a rate limit and a cache.",
        lifespan=lifespan,
        middleware=middleware,
    )
    app.state.metrics = metrics
    app.state.settings = settings
    app.state.cache = cache
    app.state.redis = redis

    async def cached(request: Request, route: str, params: dict[str, Any], compute) -> Any:
        """Serve ``compute`` through the cache and record the outcome on the request."""
        value, outcome = await request.app.state.cache.get_or_compute(
            cache_key(route, params), compute
        )
        request.state.cache_result = outcome
        request.app.state.metrics.cache_operations.labels(route, outcome).inc()
        return value

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request) -> dict[str, Any]:
        """Liveness *and* dependency check.

        Redis being down is reported as ``degraded``, not as a failure: the API keeps
        serving because the cache and limiter both fail open, and a health check that
        returns 503 here would have a load balancer pull a still-working instance.
        """
        try:
            await request.app.state.redis.ping()
            redis_ok = True
        except RedisError:
            redis_ok = False
        return {
            "status": "ok" if redis_ok else "degraded",
            "redis": "up" if redis_ok else "down",
            "cohorts": len(
                await _measured(request.app, "cohorts", request.app.state.warehouse.cohorts)
            ),
        }

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    async def prometheus_metrics(request: Request) -> Response:
        return metrics_response(request.app.state.metrics)

    @app.get("/cohorts", tags=["cohorts"])
    async def list_cohorts(request: Request) -> dict[str, Any]:
        """Every cohort in the warehouse, with its size."""
        route = route_template(request)

        async def compute() -> dict[str, Any]:
            rows = await _measured(request.app, "cohorts", request.app.state.warehouse.cohorts)
            return {"cohorts": rows}

        return await cached(request, route, {}, compute)

    @app.get("/cohorts/{cohort_week}/retention", tags=["cohorts"])
    async def cohort_retention(request: Request, cohort_week: dt.date) -> dict[str, Any]:
        """Weekly retention curve for one cohort. The hot path -- see README section 5."""
        route = route_template(request)

        async def compute() -> dict[str, Any] | None:
            return await _measured(
                request.app, "retention", request.app.state.warehouse.retention, cohort_week
            )

        result = await cached(request, route, {"cohort_week": cohort_week}, compute)
        if result is None:
            raise HTTPException(status_code=404, detail=f"No cohort starting {cohort_week}.")
        return result

    @app.get("/cohorts/{cohort_week}/funnel", tags=["cohorts"])
    async def cohort_funnel(
        request: Request,
        cohort_week: dt.date,
        channel: str | None = Query(default=None, max_length=32),
    ) -> dict[str, Any]:
        """Signup to subscription conversion for one cohort, optionally by channel."""
        route = route_template(request)

        async def compute() -> dict[str, Any] | None:
            return await _measured(
                request.app,
                "funnel",
                request.app.state.warehouse.funnel,
                cohort_week,
                channel,
            )

        result = await cached(
            request, route, {"cohort_week": cohort_week, "channel": channel}, compute
        )
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"No cohort starting {cohort_week}"
                + (f" on channel {channel}." if channel else "."),
            )
        return result

    return app


def build_default_app() -> FastAPI:
    """Entry point for ``uvicorn observableapi.app:build_default_app --factory``."""
    return create_app()
