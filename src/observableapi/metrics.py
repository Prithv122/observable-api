"""Prometheus collectors.

These are hand-rolled rather than pulled from an off-the-shelf FastAPI instrumentator, for
two reasons that matter in practice:

1. **Cardinality.** Generic instrumentators label by raw URL path, so
   ``/cohorts/2026-01-05/retention`` and ``/cohorts/2026-01-12/retention`` become two
   different time series. With 24 cohorts and growing, that is an unbounded label. Every
   metric here is labelled by the *route template*, which is a fixed, small set.
2. **The labels I actually want.** ``cache`` on the latency histogram is the whole point of
   this project -- it lets one query separate cache-hit latency from cache-miss latency,
   which is the difference between "p95 got worse" and "the hit rate dropped".

Bucket boundaries are set from measured data (see README section 5), not from the library
default, which spans 5ms to 10s and puts almost every observation here in one bucket.
"""

from __future__ import annotations

import os

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, multiprocess

# The two ends of the range that matter here: a cache hit is a Redis round trip
# (sub-millisecond to a few ms), a miss is a DuckDB aggregation (tens of ms). The buckets
# are dense across both so p95 is not interpolated across a 10x-wide bucket.
LATENCY_BUCKETS = (
    0.001,
    0.0025,
    0.005,
    0.0075,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    float("inf"),
)


class Metrics:
    """All collectors, bound to one registry.

    Holding a registry explicitly (rather than using the process-global default) is what
    makes the test suite able to assert on metric values without state leaking between
    tests.
    """

    def __init__(self, registry: CollectorRegistry) -> None:
        self.registry = registry

        self.requests = Counter(
            "http_requests_total",
            "HTTP requests, by route template and outcome.",
            ["method", "route", "status"],
            registry=registry,
        )
        self.request_duration = Histogram(
            "http_request_duration_seconds",
            "End-to-end request latency, split by cache outcome.",
            ["method", "route", "cache"],
            buckets=LATENCY_BUCKETS,
            registry=registry,
        )
        self.requests_in_flight = Gauge(
            "http_requests_in_flight",
            "Requests currently being served.",
            registry=registry,
            # Under multiple workers a gauge has one value per process. 'livesum' adds the
            # live workers' values together, which is the only reading that means anything
            # for "requests in flight across the service".
            multiprocess_mode="livesum",
        )
        self.cache_operations = Counter(
            "cache_operations_total",
            "Cache lookups, by outcome (hit, miss, bypass, error).",
            ["route", "result"],
            registry=registry,
        )
        self.rate_limit_decisions = Counter(
            "rate_limit_decisions_total",
            "Rate-limiter decisions (allowed or throttled).",
            ["route", "decision"],
            registry=registry,
        )
        self.warehouse_query_duration = Histogram(
            "warehouse_query_duration_seconds",
            "Time spent inside DuckDB, excluding cache and HTTP overhead.",
            ["query"],
            buckets=LATENCY_BUCKETS,
            registry=registry,
        )


def exposition_registry(registry: CollectorRegistry) -> CollectorRegistry:
    """The registry to render at ``/metrics``.

    With more than one worker process, each worker holds its own counters and a scrape is
    answered by whichever worker the OS happens to hand the connection to -- so ``/metrics``
    reports roughly 1/N of the traffic, and a different fraction every time. Measured on the
    4-worker stack, consecutive scrapes returned 97 and then 74 for the same counter.

    ``prometheus_client``'s multiprocess mode fixes this: each worker writes its samples to
    shared mmap files under ``PROMETHEUS_MULTIPROC_DIR``, and exposition aggregates across
    all of them. The directory must be wiped at container start, or counters from a previous
    run are added to this one's -- see ``docker-entrypoint.sh``.

    When the variable is unset (tests, a single worker) the in-process registry is already
    correct and is returned untouched.
    """
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not multiproc_dir:
        return registry
    aggregated = CollectorRegistry()
    multiprocess.MultiProcessCollector(aggregated, path=multiproc_dir)
    return aggregated
