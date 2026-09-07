# Observable API — C5

**Tier:** 3 🔥 · **Category:** C — Python web · **Wave:** 3 — Production tier

Root rules in `../CLAUDE.md` apply. This file is project-specific only — keep it under 40 lines.

## What this is

A FastAPI service built to be *operated*, not just served: per-client rate limiting, a
response cache, structured JSON logs with request correlation, Prometheus `/metrics`, and a
Locust load test that produces the README's numbers.

Tier 3 means the framing, the load-test methodology, and the before/after performance
analysis must be mine — a generic "add prometheus-fastapi-instrumentator" wrapper does not
clear the bar.

## Stack

FastAPI + Redis (rate-limit counters + cache) + structlog (JSON logs, request-id
contextvars) + prometheus-client (custom RED metrics, not just an off-the-shelf exporter) +
Locust (load generation) + pytest/httpx + Docker Compose.

## Acceptance criteria

- [ ] Rate limiting with correct headers (`X-RateLimit-*`, `Retry-After`) and `429` behaviour
- [ ] Response caching with measured hit-rate and latency effect
- [ ] `structlog` JSON logs carrying a request id through every log line of a request
- [ ] Prometheus `/metrics` exposing request rate, error rate, latency histogram
- [ ] Locust load test, run for real, with results (p50/p95/p99, RPS) in README §5
- [ ] Ship gate passes (`/ship`)

## Project-specific notes

- **Needs Redis** (Docker). Rate-limit state and cache must be shared across workers — an
  in-process dict is the wrong answer and should be called out as such in README §4.
- Every latency/throughput number in the README comes from a committed Locust run against
  the real stack. Never estimate them.
