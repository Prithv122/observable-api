"""Run the load-test scenarios that produce README section 5, and print them as a table.

Every number in the README comes from this script. Run it yourself:

    uv run python scripts/benchmark.py

It recreates the API container per scenario (so the config change actually takes effect),
flushes Redis between runs so no scenario inherits the previous one's warm cache, waits for
the health check, then drives Locust headless and reads the aggregated CSV it writes.

Results land in ``loadtest/`` -- raw Locust CSVs, kept so the summary can be re-derived.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "loadtest"
BASE_URL = "http://localhost:8000"

USERS = 60
SPAWN_RATE = 20
# 120s rather than 60s: a shorter run on a laptop swings by ~25% between repetitions,
# mostly Docker Desktop scheduling noise. Longer runs make the comparison legible.
RUN_TIME = "120s"


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    env: dict[str, str] = field(default_factory=dict)


SCENARIOS = [
    Scenario(
        "no_cache",
        "Cache off (baseline)",
        {"CACHE_ENABLED": "false", "RATE_LIMIT_ENABLED": "false"},
    ),
    Scenario(
        "cache",
        "Cache on, 30s TTL",
        {"CACHE_ENABLED": "true", "CACHE_TTL_SECONDS": "30", "RATE_LIMIT_ENABLED": "false"},
    ),
    Scenario(
        "cache_and_limit",
        "Cache on + rate limit 60/min",
        {
            "CACHE_ENABLED": "true",
            "CACHE_TTL_SECONDS": "30",
            "RATE_LIMIT_ENABLED": "true",
            "RATE_LIMIT_REQUESTS": "60",
            "RATE_LIMIT_WINDOW_SECONDS": "60",
        },
    ),
]


def compose(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, env=full_env, check=True, capture_output=True
    )


def wait_for_health(timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    raise RuntimeError("API did not become healthy in time")


def scrape_metrics() -> dict[str, float]:
    """Pull the counters that describe what the run actually did."""
    with urllib.request.urlopen(f"{BASE_URL}/metrics", timeout=5) as response:
        body = response.read().decode()

    totals: dict[str, float] = {}
    for line in body.splitlines():
        if line.startswith("cache_operations_total{"):
            result = line.split('result="', 1)[1].split('"', 1)[0]
            totals[result] = totals.get(result, 0.0) + float(line.rsplit(" ", 1)[1])
        elif line.startswith("rate_limit_decisions_total{"):
            decision = line.split('decision="', 1)[1].split('"', 1)[0]
            totals[decision] = totals.get(decision, 0.0) + float(line.rsplit(" ", 1)[1])
    return totals


def run_locust(scenario: Scenario) -> dict[str, str]:
    prefix = OUT_DIR / scenario.key
    subprocess.run(
        [
            sys.executable,
            "-m",
            "locust",
            "-f",
            str(ROOT / "locustfile.py"),
            "--host",
            BASE_URL,
            "--headless",
            "--users",
            str(USERS),
            "--spawn-rate",
            str(SPAWN_RATE),
            "--run-time",
            RUN_TIME,
            "--csv",
            str(prefix),
            "--only-summary",
            # A throttled request is a failed request to Locust. In the rate-limit scenario
            # that is the expected outcome, not a broken run, so it must not fail the script.
            "--exit-code-on-error",
            "0",
        ],
        cwd=ROOT,
        check=True,
    )
    with (prefix.parent / f"{prefix.name}_stats.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return next(row for row in rows if row["Name"] == "Aggregated")


def measure_single_request_latency(samples: int = 30) -> dict[str, list[float]]:
    """Cold vs warm latency for one request at a time, with nothing else running.

    The load-test numbers are latency *under contention*, which mixes the cache's effect
    with queueing. This isolates the cache itself: the same URL, once with an empty cache and
    once with a warm one, on an idle server.
    """
    url = f"{BASE_URL}/cohorts/2026-01-05/retention"
    timings: dict[str, list[float]] = {"cold": [], "warm": []}

    for _ in range(samples):
        compose("exec", "-T", "redis", "redis-cli", "FLUSHALL")
        started = time.perf_counter()
        urllib.request.urlopen(url, timeout=10).read()
        timings["cold"].append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        urllib.request.urlopen(url, timeout=10).read()
        timings["warm"].append((time.perf_counter() - started) * 1000)

    return timings


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    results = []

    for scenario in SCENARIOS:
        print(f"\n=== {scenario.label} ===", flush=True)
        compose("up", "-d", "--force-recreate", "api", env=scenario.env)
        compose("exec", "-T", "redis", "redis-cli", "FLUSHALL")
        wait_for_health()

        aggregated = run_locust(scenario)
        metrics = scrape_metrics()
        results.append({"scenario": scenario, "stats": aggregated, "metrics": metrics})

    print("\n\n| Scenario | RPS | p50 | p95 | p99 | max | Requests | Cache hit rate | 429s |")
    print("|---|---|---|---|---|---|---|---|---|")
    for entry in results:
        stats, metrics = entry["stats"], entry["metrics"]
        served = metrics.get("hit", 0) + metrics.get("coalesced", 0)
        lookups = served + metrics.get("miss", 0) + metrics.get("bypass", 0)
        hit_rate = f"{100 * served / lookups:.1f}%" if lookups else "n/a"
        throttled = int(metrics.get("throttled", 0))
        print(
            f"| {entry['scenario'].label} "
            f"| {float(stats['Requests/s']):.0f} "
            f"| {stats['50%']} ms | {stats['95%']} ms | {stats['99%']} ms "
            f"| {stats['Max Response Time'].split('.')[0]} ms "
            f"| {int(stats['Request Count']):,} | {hit_rate} | {throttled:,} |"
        )

    print("\n=== Single-request latency, idle server ===", flush=True)
    compose(
        "up",
        "-d",
        "--force-recreate",
        "api",
        env={"CACHE_ENABLED": "true", "RATE_LIMIT_ENABLED": "false"},
    )
    wait_for_health()
    timings = measure_single_request_latency()
    for label, values in timings.items():
        ordered = sorted(values)
        median = ordered[len(ordered) // 2]
        print(
            f"  {label:>4}: median {median:6.1f} ms   "
            f"min {ordered[0]:6.1f}   max {ordered[-1]:6.1f}   (n={len(ordered)})"
        )

    summary = [
        {
            "scenario": entry["scenario"].key,
            "label": entry["scenario"].label,
            "stats": entry["stats"],
            "metrics": entry["metrics"],
        }
        for entry in results
    ]
    summary.append({"scenario": "single_request_latency_ms", "timings": timings})
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nRaw Locust CSVs and summary.json in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
