"""Request-scoped observability, and the rate limiter's HTTP surface.

Two middlewares, deliberately separate and deliberately ordered (see ``app.py``):

``ObservabilityMiddleware`` is outermost, so a throttled request is still counted, still
timed, and still logged with its request id. A limiter that sits *outside* the telemetry
makes 429s invisible in exactly the incident where you need to see them.

``RateLimitMiddleware`` is inside it, and runs before any handler touches Redis or DuckDB --
the point of a limiter is to reject work before paying for it.
"""

from __future__ import annotations

import time
import uuid

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Match

from .metrics import Metrics
from .ratelimit import RateLimiter

log = structlog.get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
# Scrapes are not user traffic; counting them would put a constant, meaningless component
# into every request-rate panel built on these metrics.
UNMEASURED_PATHS = frozenset({"/metrics"})
UNLIMITED_PATHS = frozenset({"/metrics", "/healthz"})


def route_template(request: Request) -> str:
    """The route *pattern*, not the concrete path.

    ``/cohorts/{cohort_week}/retention`` rather than ``/cohorts/2026-01-05/retention``.
    Without this every distinct cohort becomes its own Prometheus time series -- the label
    cardinality explosion that turns a metrics bill into an incident of its own.

    After routing, Starlette has already put the matched route in the scope. Before routing
    -- which is where the rate limiter sits -- it has not, so the routing table is matched
    directly. Rejected requests are exactly the ones you most want broken down by route, and
    labelling them ``unmatched`` would throw that away.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    for candidate in request.app.routes:
        match, _ = candidate.matches(request.scope)
        if match is Match.FULL:
            return getattr(candidate, "path", "unmatched")
    return "unmatched"


def client_id(request: Request) -> str:
    """Who to rate limit.

    An API key when one is presented, otherwise the source address. Behind a proxy this
    should read a forwarded header *that the proxy is trusted to set* -- taking
    ``X-Forwarded-For`` from the client directly would let anyone reset their own budget by
    spoofing it, so it is not read here.
    """
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, metrics: Metrics) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        request.state.cache_result = "none"

        # Binding here is what lets the cache and warehouse layers log with a request id
        # without either of them knowing that HTTP exists.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client=client_id(request),
        )

        measured = request.url.path not in UNMEASURED_PATHS
        if measured:
            self._metrics.requests_in_flight.inc()
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - started
            log.exception("request_failed", duration_ms=round(duration * 1000, 2))
            if measured:
                self._metrics.requests.labels(request.method, route_template(request), "500").inc()
                self._metrics.request_duration.labels(
                    request.method, route_template(request), "error"
                ).observe(duration)
            raise
        finally:
            if measured:
                self._metrics.requests_in_flight.dec()

        duration = time.perf_counter() - started
        route = route_template(request)
        cache_result = getattr(request.state, "cache_result", "none")
        response.headers[REQUEST_ID_HEADER] = request_id

        if measured:
            self._metrics.requests.labels(request.method, route, str(response.status_code)).inc()
            self._metrics.request_duration.labels(request.method, route, cache_result).observe(
                duration
            )
            log.info(
                "request_completed",
                route=route,
                status=response.status_code,
                cache=cache_result,
                duration_ms=round(duration * 1000, 2),
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: RateLimiter, metrics: Metrics) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._metrics = metrics

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in UNLIMITED_PATHS:
            return await call_next(request)

        route = route_template(request)
        result = await self._limiter.check(client_id(request))
        self._metrics.rate_limit_decisions.labels(
            route, "allowed" if result.allowed else "throttled"
        ).inc()

        if not result.allowed:
            log.info("rate_limited", route=route, retry_after=result.reset_seconds)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded.",
                    "retry_after_seconds": result.reset_seconds,
                },
                headers=result.headers(),
            )

        response = await call_next(request)
        # Budget headers go on successful responses too: a client can only slow down
        # *before* it is throttled if it is told how much budget is left.
        response.headers.update(result.headers())
        return response


def metrics_response(metrics: Metrics) -> Response:
    return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)
