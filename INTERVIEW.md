# Interview Prep — Observable API

**Five questions, five answers.** An unanswered question means this project is not shipped.

---

### Q1. Walk me through the architecture in 90 seconds.

_A:_ It's a FastAPI service that serves cohort retention and funnel metrics out of an
embedded DuckDB warehouse — about 105,000 synthetic events across 24 weekly cohorts. The
interesting part isn't the endpoints, it's the two middlewares around them and the Redis
behind them.

Outermost is an observability middleware: it assigns a request id, binds it into a
contextvar so every log line from any layer carries it, times the request, and records
Prometheus metrics labelled by *route template* rather than URL. Inside that is the rate
limiter, which checks the client's budget in a single Redis Lua call and rejects before any
work happens. That order is deliberate — a throttled request is still counted, timed and
logged, so 429s stay visible in exactly the incident where you need to see them.

Handlers go through a Redis response cache with single-flight locking. On a hit it's a Redis
round trip; on a miss the first request takes a short lock and runs the DuckDB aggregation
in the threadpool while the others wait for its result instead of duplicating it. Everything
fails open: if Redis is down, the limiter and cache step aside and the API keeps serving,
degraded but up.

Four uvicorn workers, all sharing Redis for limiter and cache state, and sharing metrics
through prometheus_client's multiprocess mmap files. A Locust file with a Zipf traffic
profile produces the numbers in the README.

### Q2. Why the weighted sliding-window counter over a fixed window or a sliding log?

_A:_ Fixed windows have a boundary burst: a client can spend its full 60-request budget at
11:59:59 and its full budget again at 12:00:00, so it gets 120 requests in a moment against
a limit that says 60. That's not theoretical — I have a test that puts the client 60% into a
window, spends the whole budget, waits for the boundary, and tries again. With this limiter
the second burst is refused entirely: 10 allowed across the boundary instead of 20.

A sliding-window *log* — a sorted set with one member per request — is exact and fixes that,
but its memory grows with traffic, and the client hammering you hardest is the one costing
you the most to store. That's a bad property for a defensive mechanism.

The weighted counter keeps two integers per client regardless of volume: the current
window's count and the previous window's, weighted by how far into the current window you
are. The cost is an assumption — that the previous window's traffic was evenly distributed —
so it can be slightly wrong at the edges. For protecting a service that's a good trade;
for billing someone per request it would not be.

Two details I'd defend: the whole check-and-increment is one Lua script, so 50 concurrent
requests can't all read the same count and all decide they're under the limit (there's a
test for that too — it asserts exactly 10 of 50 racing requests get through). And the
timestamp comes from `redis.call('TIME')` inside the script, not from Python, so all four
workers share one clock. Otherwise a second of skew between workers puts requests in
different buckets and the limit quietly stops meaning what it says.

### Q3. What's the weakest part, and what breaks first under load?

_A:_ The threadpool, before the CPU. DuckDB is synchronous and CPU-bound, so every cache
miss occupies a worker thread for the length of an aggregation — about 24 ms of real work.
Cache hits don't touch it at all. So the failure mode isn't gradual: as long as the hit rate
is high everything is fine, and if the hit rate collapses — a deploy that flushes Redis, a
TTL expiry storm, someone iterating over all 24 cohorts — the pool saturates while the event
loop sits idle, and latency goes off a cliff rather than degrading smoothly. You can see the
shape of that in the baseline row of my results: with the cache off, p99 was 950 ms and the
max was 2.5 seconds, against 240 ms and 443 ms with it on.

The fix I'd make first is a bounded semaphore around warehouse queries with a fast 503 when
it's full — shed load deliberately instead of queueing until everything times out. Second
would be precomputing the hot aggregations on a schedule so the cache becomes a materialised
view rather than a side effect of traffic.

The genuinely weak part architecturally is that DuckDB is embedded, so every replica carries
its own copy of the file and the data can only change by redeploying. That's fine at this
size and wrong as a data platform — the next step is DuckDB reading Parquet from object
storage so data refresh is decoupled from deployment.

### Q4. How do you know it works? What did you measure, against what baseline?

_A:_ Three scenarios through the same Locust profile, 60 concurrent clients, no think time,
120 seconds each, against the real Docker stack: cache off, cache on, and cache on with the
rate limiter enabled. Redis flushed between runs so nothing inherits a warm cache.

Cache off was the baseline: 188 RPS, p95 660 ms. Cache on: 508 RPS, p95 160 ms — 2.7× the
throughput and p95 4.1× lower, at a 99% hit rate. I also isolated the cache from queueing
effects by measuring one request at a time on an idle server, alternating flushed and warm:
34 ms cold, 9.5 ms warm.

Three things I'm careful about when I present those. The absolute numbers are noisy on a
laptop — baseline throughput measured 162, 188 and 213 across three runs — so I quote the
ratios and say the run length was chosen to make them legible. The warm 9.5 ms is mostly
HTTP and Docker Desktop's Windows networking, not Redis, so the honest claim is the ~24 ms
*difference*, not the absolute. And the third scenario's latency isn't comparable to the
others at all, because 74% of its requests were 429s and rejecting is much cheaper than
serving — it's there to show the limiter working, not to win a latency comparison.

The traffic profile matters as much as the numbers: cohort popularity is Zipf-distributed,
newest cohort ~30% of requests. Benchmarking a cache against uniformly random keys would
have produced a hit rate that's an artifact of the key space and TTL, and meaningless.

Correctness separately: 70 tests, 100% statement coverage, run against a real Redis rather
than a mock — everything interesting about the limiter and the cache is Lua executing inside
Redis, and a mock would only test my own assumptions about it.

### Q5. Your metrics were wrong and you shipped the fix. What happened, and why didn't the tests catch it?

_A:_ After the first load test, Prometheus said 6,686 requests had been throttled and Locust
had independently counted about 23,000 429s. That's not a rounding difference, so I scraped
the same counter six times in a row and got 97, 97, 97, 97, 97, 74.

Four uvicorn workers, each with its own in-process `CollectorRegistry`. A scrape is answered
by whichever worker the OS hands the connection to, so `/metrics` was reporting one worker's
slice — roughly a quarter, and a different quarter each time. Everything looked plausible.
The counters went up, the graphs had the right shape, they were just wrong by a factor that
changed between scrapes.

The fix is prometheus_client's multiprocess mode: workers write samples into mmap files in a
shared directory and exposition builds a registry that aggregates across all of them. Two
details that bite: the in-flight gauge needs `multiprocess_mode="livesum"` or you get one
arbitrary worker's value, and the mmap files outlive the process — so the container
entrypoint wipes the directory at start, or a restart adds the previous run's counters to
this one's. After the fix, five consecutive scrapes agreed exactly, and Prometheus's
throttled count matched Locust's 429 count to the request. That agreement between two
independent counters is what I actually trust as proof.

Why the tests didn't catch it: they run in one process. There is no unit test that can
observe this, because the bug *is* the process boundary. That's the real lesson —
observability code has failure modes that only exist in the deployment topology, so I
verified it against the running 4-worker container, and I keep a Docker job in CI that
builds the image and curls the live endpoints for the same reason. What I could add is an
integration test that starts two workers and asserts a counter equals the number of requests
sent; I'd write that next.

---

## 30-second pitch

A cohort-analytics API built for operations rather than features: Redis-backed sliding-window
rate limiting, a single-flight response cache, request-correlated JSON logs, and Prometheus
RED metrics — all measured with a Locust profile that uses realistically skewed traffic.
Caching took it from 188 to 508 requests per second and cut p95 latency from 660 ms to
160 ms at a 99% hit rate. Along the way I found and fixed a bug where four uvicorn workers
each reported their own metrics, so `/metrics` was returning a random quarter of reality —
which is the kind of thing that's invisible until you go looking, and the reason the
project exists.
