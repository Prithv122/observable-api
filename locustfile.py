"""Load profile.

The traffic shape is the point. Cohort popularity here follows a Zipf-like curve: measured
over 20,000 draws, the newest cohort takes ~30% of requests and the oldest twelve share ~15%
between them. That is how dashboards actually read this kind of API, and a cache benchmarked
against uniformly random keys is a benchmark of nothing -- every key is cold, the hit rate is
whatever the key space and TTL happen to make it, and the number tells you nothing about
production.

The same distribution is described in ``warehouse.py``. If one changes, change both.

Run it:

    uv run locust -f locustfile.py --host http://localhost:8000 \\
        --headless --users 50 --spawn-rate 10 --run-time 60s

Requests carry a per-user ``X-API-Key`` so the rate limiter sees many clients rather than one
very busy IP -- otherwise a load test against a rate-limited API measures only the limiter.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid
from collections import Counter

from locust import HttpUser, constant, events, task

FIRST_COHORT = dt.date(2026, 1, 5)
COHORT_COUNT = 24
ZIPF_EXPONENT = 1.1

COHORTS = [(FIRST_COHORT + dt.timedelta(weeks=i)).isoformat() for i in range(COHORT_COUNT)]
# Rank 1 is the newest cohort. Weight falls off as 1/rank**s.
WEIGHTS = [1 / (rank**ZIPF_EXPONENT) for rank in range(1, COHORT_COUNT + 1)]
RANKED_COHORTS = list(reversed(COHORTS))
CHANNELS = [None, "organic", "paid_search", "referral", "email"]


def pick_cohort(rng: random.Random) -> str:
    return rng.choices(RANKED_COHORTS, weights=WEIGHTS, k=1)[0]


class DashboardUser(HttpUser):
    """One analytics dashboard, polling the endpoints a real one would."""

    # No think time. With a fixed number of concurrent clients issuing requests back to
    # back, the measured RPS is the service's capacity at that concurrency. Adding think
    # time would instead pin throughput to (users / think time) -- every scenario would
    # report the same RPS and the cache's effect on capacity would be invisible.
    wait_time = constant(0)

    def on_start(self) -> None:
        self.rng = random.Random()
        self.client.headers["X-API-Key"] = f"loadtest-{uuid.uuid4().hex[:8]}"

    @task(10)
    def retention(self) -> None:
        cohort = pick_cohort(self.rng)
        # Grouped so every cohort lands in one Locust statistic rather than 24, matching how
        # the API's own Prometheus labels work.
        self.client.get(
            f"/cohorts/{cohort}/retention",
            name="/cohorts/{cohort_week}/retention",
        )

    @task(4)
    def funnel(self) -> None:
        cohort = pick_cohort(self.rng)
        channel = self.rng.choice(CHANNELS)
        params = {"channel": channel} if channel else {}
        self.client.get(
            f"/cohorts/{cohort}/funnel",
            params=params,
            name="/cohorts/{cohort_week}/funnel",
        )

    @task(1)
    def cohort_list(self) -> None:
        self.client.get("/cohorts", name="/cohorts")


STATUS_COUNTS: Counter[int] = Counter()


@events.request.add_listener
def count_status(response=None, exception=None, **_kwargs) -> None:
    # Counted even when Locust flags the request as a failure: a 429 *is* a failure from the
    # client's point of view, and it is the exact case this counter exists to surface.
    status = getattr(response, "status_code", None)
    if status is not None:
        STATUS_COUNTS[status] += 1


@events.quitting.add_listener
def report_status_codes(environment, **_kwargs) -> None:
    """Print the status-code split at the end of a run.

    Locust treats a 429 as a successful request -- it is a valid HTTP response -- so without
    this a run that was mostly throttled looks identical in the summary to one that was fully
    served, and its "improved" latency is really just the cost of being turned away quickly.
    """
    total = sum(STATUS_COUNTS.values())
    print("\nStatus codes:")
    for status, count in sorted(STATUS_COUNTS.items()):
        share = (100 * count / total) if total else 0
        print(f"  {status}: {count:>7,}  ({share:5.1f}%)")
