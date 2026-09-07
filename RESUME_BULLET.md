# Resume Bullets — Observable API

Form: **action → technical specifics → measured outcome.** Numbers or it doesn't go on the resume.

---

## Bullets

- Built a read-heavy analytics API (FastAPI, DuckDB, Redis, 4 uvicorn workers) with a
  Redis-backed single-flight response cache; measured with a Locust profile using
  Zipf-distributed traffic, caching raised sustained throughput from **188 to 508 req/s
  (2.7×)** and cut **p95 latency from 660 ms to 160 ms** at a 99.0% hit rate.

- Implemented per-client rate limiting as a weighted sliding-window counter in a single
  Redis Lua script — atomic check-and-increment, Redis-sourced clock for a shared budget
  across workers, and correct `X-RateLimit-*` / `Retry-After` headers — eliminating the
  fixed-window boundary burst that allows 2× the configured limit, proven by a test that
  reproduces the burst and asserts it is refused.

- Diagnosed and fixed silently wrong Prometheus metrics in a multi-worker deployment, where
  each uvicorn worker exposed its own registry and consecutive scrapes of the same counter
  returned **97 then 74**; moved to `prometheus_client` multiprocess mode with mmap-backed
  aggregation, after which Prometheus's throttled count matched an independent load-test
  counter **exactly (26,271)**.

- Instrumented the service with hand-written RED metrics labelled by route template rather
  than URL path (bounding cardinality at 24 cohorts and growing) and a latency histogram
  split by cache outcome, making "requests got slower" distinguishable from "the hit rate
  dropped"; paired with structlog JSON logs that carry a request id through every layer via
  contextvars.

- Cut warehouse build time from a projected ~11.5 minutes to **0.23 s for 105,638 rows** by
  profiling DuckDB's row-at-a-time `executemany` to 6.5 ms/row and replacing it with a bulk
  CSV `COPY`; the warehouse test module went from 42.1 s to 1.2 s.

- Shipped with **68 tests at 100% statement coverage** run against a real Redis (never
  mocked, since the limiter and cache logic is Lua executing inside Redis) plus a CI job
  that builds the production image and exercises the live endpoints.

## Which roles this supports

- [ ] Data Scientist / ML
- [x] AI Engineer (LLM/NLP/CV) — *weakly; only as backend/serving evidence*
- [x] Data Engineer
- [x] Data Analyst / Python Developer

Strongest for **backend/platform-leaning Data Engineer and Python Developer** roles. The
performance engineering and observability work is the differentiator; the cohort/funnel SQL
is secondary but does show analytical modelling.

## Keywords this project earns

FastAPI · asyncio · Redis · Lua scripting · rate limiting (sliding window) · cache
stampede / single-flight · Prometheus · prometheus_client multiprocess mode · RED metrics ·
histogram buckets · cardinality control · structlog · structured logging · request tracing /
correlation IDs · DuckDB · OLAP bulk loading · Locust · load testing · p95/p99 latency ·
Docker · Docker Compose · GitHub Actions · pytest · pytest-asyncio · graceful degradation /
fail-open

---

### Bad vs good

❌ "Added caching and monitoring to a FastAPI service using Redis and Prometheus."
✅ "Added a Redis single-flight response cache to a FastAPI analytics service, raising
sustained throughput 2.7× (188→508 req/s) and cutting p95 latency from 660 ms to 160 ms
under Zipf-distributed load."

The first invites "so what did that achieve?" and has no answer. The second states the
measurement, the traffic model it was measured under, and can be defended line by line.
