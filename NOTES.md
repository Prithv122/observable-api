# Build Notes — Observable API

Working notes: what broke, what I tried, why X over Y.
Not for recruiters — for me, six months from now, in an interview.

---

## Log

### 2026-09-07 — DuckDB `executemany` is ~2.9 ms per row

- **Tried:** the obvious loader — `con.executemany("INSERT INTO events VALUES (?,?,?,?,?)", rows)`.
- **Broke:** the warehouse test suite took **42 seconds** for a 2,412-row fixture. Profiled
  it expecting the Python generator to be the cost. It wasn't: generation was 0.02 s,
  `executemany` was **15.8 s**. At that rate the default 105k-row warehouse would take
  roughly five minutes to build.
- **Tried next:** wrapping it in an explicit `BEGIN`/`COMMIT` (18.1 s — *worse*), then
  chunked multi-row `VALUES` (12.0 s — better, still awful). Reduced it to a minimal case:
  even a two-column `INSERT` of 2,412 trivial rows took 6.9 s. So it is per-statement
  overhead, not type conversion.
- **Fixed by:** writing the rows to a temporary CSV and using `COPY events FROM …`.
  **105,638 rows in 0.23 s.** Test suite went 42 s → 1.3 s.
- **Learned:** DuckDB is columnar and treats each INSERT as its own transaction. Row-at-a-
  time insertion into an OLAP engine is its pathological case, and the fix is not a faster
  loop — it's handing it one bulk load. Worth remembering the *shape* of the mistake:
  I reached for the API that looked like the SQLite one.

### 2026-09-07 — `/metrics` was returning a different number every scrape

- **Broke:** after the first load-test run, Prometheus reported 6,686 throttled requests
  while Locust independently counted ~23,000 429s. Scraping the same counter six times in a
  row returned `97, 97, 97, 97, 97, 74`.
- **Cause:** 4 uvicorn workers, each with its own in-process `CollectorRegistry`. A scrape
  is answered by whichever worker the OS hands the connection to, so `/metrics` reports one
  worker's slice — roughly 1/N, and a different N-th every time.
- **Fixed by:** `prometheus_client` multiprocess mode. Workers write samples to mmap files
  under `PROMETHEUS_MULTIPROC_DIR`; exposition builds a fresh registry with
  `MultiProcessCollector` that aggregates across all of them. The in-flight `Gauge` needs
  `multiprocess_mode="livesum"` or it reports one arbitrary worker's value.
- **Also needed:** an entrypoint that wipes the directory at start. The mmap files outlive
  the process, so a restart with a dirty directory adds the previous run's counters to this
  one's. Found that while reasoning about it, not by being bitten — but it *would* have
  quietly corrupted every subsequent benchmark.
- **Verified:** after the fix, five consecutive scrapes agreed exactly, and Prometheus's
  throttled count (26,271) matched Locust's independent 429 count to the request.
- **Learned:** this is the single most likely thing to be wrong in any multi-worker Python
  service with Prometheus, and it fails *silently* — the numbers look plausible, they're
  just a fraction of reality. Nothing in the test suite would have caught it, because the
  tests run one process. The container was the only place it could show up.

### 2026-09-07 — my own test harness was tearing the app down mid-test

- **Broke:** the one test that lets the app open its own DuckDB connection failed with
  `ConnectionException: Connection already closed!`. Every other test passed.
- **Cause:** my hand-rolled `lifespan_app` put *both* `lifespan.startup` and
  `lifespan.shutdown` in the receive queue up front. The app completed startup, immediately
  received shutdown, and tore itself down while the test was still making requests. Every
  other test injects its Redis and warehouse, so `owns_*` was False and shutdown did
  nothing — the bug was invisible in ~60 tests.
- **Fixed by:** only queueing the shutdown message when the context manager exits, and
  waiting on an `Event` set by `lifespan.startup.complete`.
- **Learned:** a test harness that is subtly wrong in a way most tests don't exercise is
  worse than no harness. The tell was that the *first* test to use the real wiring failed —
  that should always prompt "is the harness wrong?" before "is the code wrong?".

### 2026-09-07 — Locust wouldn't start: `'charmap' codec can't decode byte 0x8f`

- **Broke:** `locust` exited 2 before running anything, complaining it couldn't parse
  `pyproject.toml`.
- **Cause:** Locust reads `pyproject.toml` for a `[tool.locust]` section using the platform
  default encoding, which on Windows is cp1252. The file had a `⚠️` and an em-dash in
  comments inherited from my project template.
- **Fixed by:** making `pyproject.toml` pure ASCII, with an assertion in the edit script that
  no non-ASCII survived.
- **Learned:** config files that third-party tools might read should stay ASCII on Windows.
  The file was valid UTF-8 TOML; the reader was the problem, and I don't control the reader.

### 2026-09-07 — the first load test measured the client, not the server

- **Broke:** the initial profile used `wait_time = between(0.1, 0.5)`. With 60 users that
  caps throughput at ~200 RPS regardless of how fast the server is, so cache-on and
  cache-off would have reported nearly the same RPS and the cache's effect on *capacity*
  would have been invisible.
- **Fixed by:** `wait_time = constant(0)` — fixed concurrency, back-to-back requests, so the
  measured RPS is the service's capacity at that concurrency.
- **Also fixed:** Locust counts a 429 as a failed request and exits non-zero, which killed
  the benchmark script on the rate-limit scenario. Added `--exit-code-on-error 0`, since
  being throttled is that scenario's expected outcome.
- **Learned:** closed-loop with think time answers "what latency do N users see at this
  arrival rate"; closed-loop without it answers "what can this service do at concurrency N".
  I wanted the second and had written the first.

### 2026-09-07 — structlog's logger cache defeated the logging tests

- **Broke:** two logging tests failed because module-level loggers in `cache.py` and
  `middleware.py` kept writing to an earlier test's buffer.
- **Cause:** `cache_logger_on_first_use=True` freezes the processor chain and output stream
  at first use; later `structlog.configure()` calls are ignored by an already-bound logger.
- **Fixed by:** setting it to `False`, with the reasoning written next to it. The per-call
  cost is a dictionary copy on requests that already spend milliseconds in Redis and DuckDB.
- **Learned:** the "obviously correct" performance default made a correctness property
  (logging is actually configured the way you think) untestable. Given the cost here, the
  trade was easy — but it was a trade, not a free win.

### 2026-09-07 — cardinality bug I wrote and then caught

- Wrote a docstring in `metrics.py` arguing at length that labelling by raw URL path is an
  unbounded-cardinality mistake, and then labelled `rate_limit_decisions_total` with
  `request.url.path`, because the limiter runs *before* routing and the route template isn't
  in the scope yet.
- **Fixed by:** `route_template()` falling back to matching the routing table directly
  (`route.matches(scope)`) when `scope["route"]` is absent. Throttled requests are exactly
  the ones you want broken down by route, so labelling them `unmatched` was not acceptable
  either.
- **Learned:** worth re-reading your own stated principle against the code that came after
  it. Writing the rule down is what made the violation obvious.

---

## Rejected approaches

| Approach | Why rejected |
|---|---|
| `prometheus-fastapi-instrumentator` | Labels by raw path (cardinality), and can't know about this app's cache — the `cache` label on the latency histogram is the point of the whole exercise. |
| Sliding-window *log* limiter (sorted set per client) | Exact, but memory grows with request volume and the worst-behaved client costs the most to store. Weighted counter is 2 integers per client at any rate. |
| Fixed-window limiter | Allows 2× the limit across a window boundary. There's a test that demonstrates it. |
| In-process rate-limit counter | Each of 4 workers gets its own budget → real limit is 4× configured. |
| Timestamps from Python for limiter buckets | Clock skew between workers puts requests in different buckets. `redis.call('TIME')` gives one clock; it works on Redis 8 (effects replication makes writes-after-TIME legal). |
| Cache lock released with a plain `DEL` | A computation slower than the lock TTL would delete a lock a *different* request now holds. Compare-and-delete in Lua instead, with a per-attempt uuid token. |
| `id(compute)` as the lock token | CPython reuses ids after collection. Same class of bug as above, harder to see. Swapped for `uuid4().hex`. |
| Deploying to a free tier | The deliverable is behaviour under sustained load; a host that sleeps after 15 min of idle and throttles CPU would misrepresent every number in §5. Docker Compose locally is the honest demo. |
| Committing `data/warehouse.duckdb` | 2.5 MB of binary that's byte-reproducible from a seed. `observable-api build` is one documented step, and the image builds it at build time. |

## Open questions

- [ ] The `coalesced` waiters poll every 10 ms. Redis pub/sub would wake them immediately,
      at the cost of a second connection and a subscriber task per worker. At 76 coalesced
      requests out of 60k it isn't worth it *here* — but where's the crossover?
- [ ] `X-RateLimit-Remaining` is computed from the weighted estimate, so it can be
      non-monotonic near a window boundary (the previous window's contribution decays as
      time passes, which can make `remaining` tick *up*). Correct per the algorithm,
      possibly surprising to a client author. Is there a standard for what to report here?
- [ ] The health check reports `degraded` when Redis is down but still returns 200. Right
      call for a load balancer, but it means an alert has to be built on the body, not the
      status code. Should there be a separate `/readyz` that *does* fail?
