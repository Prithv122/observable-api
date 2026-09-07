"""The read side: a synthetic event warehouse in DuckDB, and the queries the API serves.

Why synthetic: no public dataset publishes raw user-level signup/activation/subscription
events, and this project is about *serving* an analytical read workload under load, not
about the analysis itself. The generator is seeded, so the warehouse is byte-stable and
every latency number in the README is reproducible.

Why the traffic is skewed: cohort popularity follows a Zipf-like curve (recent cohorts are
looked at constantly, year-old ones almost never). A cache measured against uniformly
random keys is measuring nothing -- see ``locustfile.py``, which samples the same
distribution.

Threading note: DuckDB connections are not safe to share across threads, but ``.cursor()``
returns a cheap duplicate that is. Queries run in the threadpool with their own cursor;
see README section 4.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

SEED = 20260907
FIRST_COHORT = dt.date(2026, 1, 5)  # a Monday
CHANNELS = ("organic", "paid_search", "referral", "email")

SCHEMA = """
CREATE TABLE events (
    user_id      INTEGER   NOT NULL,
    cohort_week  DATE      NOT NULL,
    event_ts     TIMESTAMP NOT NULL,
    event_name   VARCHAR   NOT NULL,
    channel      VARCHAR   NOT NULL
);
"""


@dataclass(frozen=True)
class GeneratorSpec:
    """Size of the generated warehouse. The default is what the README's numbers use."""

    weeks: int = 24
    users_per_week: int = 900
    max_followup_weeks: int = 12


def cohort_weeks(spec: GeneratorSpec) -> list[dt.date]:
    return [FIRST_COHORT + dt.timedelta(weeks=i) for i in range(spec.weeks)]


def _generate_rows(spec: GeneratorSpec) -> list[tuple[Any, ...]]:
    """Deterministic event stream. Same seed, same rows, byte for byte."""
    rng = random.Random(SEED)
    rows: list[tuple[Any, ...]] = []
    user_id = 0

    for week_index, cohort in enumerate(cohort_weeks(spec)):
        # Paid-search share spikes in weeks 8-11, so the channel mix is not flat over time.
        campaign = 8 <= week_index <= 11
        weights = [0.30, 0.45, 0.10, 0.15] if campaign else [0.55, 0.15, 0.15, 0.15]

        for _ in range(spec.users_per_week):
            user_id += 1
            channel = rng.choices(CHANNELS, weights=weights, k=1)[0]
            signup_ts = dt.datetime.combine(
                cohort + dt.timedelta(days=rng.randrange(7)),
                dt.time(rng.randrange(24), rng.randrange(60)),
            )
            rows.append((user_id, cohort, signup_ts, "signup", channel))

            # Retention decays geometrically; paid-search users churn faster, which is the
            # kind of difference this API's consumers are looking for.
            decay = 0.62 if channel == "paid_search" else 0.78
            activated = rng.random() < (0.40 if channel == "paid_search" else 0.55)
            if activated:
                rows.append(
                    (
                        user_id,
                        cohort,
                        signup_ts + dt.timedelta(hours=rng.randrange(1, 72)),
                        "activated",
                        channel,
                    )
                )
                if rng.random() < 0.60:
                    rows.append(
                        (
                            user_id,
                            cohort,
                            signup_ts + dt.timedelta(days=rng.randrange(1, 10)),
                            "quiz_completed",
                            channel,
                        )
                    )
            # A minority subscribe without ever activating -- planted deliberately so the
            # funnel is not strictly nested, which is the case naive funnel SQL gets wrong.
            if rng.random() < (0.22 if activated else 0.03):
                rows.append(
                    (
                        user_id,
                        cohort,
                        signup_ts + dt.timedelta(days=rng.randrange(2, 21)),
                        "subscribed",
                        channel,
                    )
                )

            week = 1
            while week <= spec.max_followup_weeks and rng.random() < decay**week:
                for _ in range(rng.randrange(1, 4)):
                    rows.append(
                        (
                            user_id,
                            cohort,
                            signup_ts
                            + dt.timedelta(
                                weeks=week, days=rng.randrange(7), hours=rng.randrange(24)
                            ),
                            "session",
                            channel,
                        )
                    )
                week += 1

    return rows


def _populate(con: duckdb.DuckDBPyConnection, spec: GeneratorSpec) -> int:
    """Load the generated rows in bulk.

    Via a CSV and ``COPY``, not ``executemany``. DuckDB is columnar and treats each INSERT
    as its own transaction: measured on this machine, row-at-a-time insertion runs at about
    6.5 ms per row, so the default warehouse would take roughly 11.5 minutes to build. The
    same 105,638 rows through ``COPY`` land in 0.23 s. Handing an OLAP engine one bulk load
    instead of 100k single-row statements is the whole difference. See NOTES.md.
    """
    rows = _generate_rows(spec)
    con.execute(SCHEMA)
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = Path(tmpdir) / "events.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(rows)
        con.execute(
            "COPY events FROM ? (FORMAT CSV, HEADER false)",
            [str(csv_path)],
        )
    # The API only ever filters by cohort_week; the index keeps a single-cohort read from
    # scanning the whole table.
    con.execute("CREATE INDEX idx_events_cohort ON events (cohort_week)")
    return len(rows)


def build(path: Path, spec: GeneratorSpec | None = None) -> int:
    """(Re)build the warehouse file. Returns the row count written."""
    spec = spec or GeneratorSpec()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    try:
        return _populate(con, spec)
    finally:
        con.close()


def build_in_memory(spec: GeneratorSpec) -> duckdb.DuckDBPyConnection:
    """An in-memory warehouse, for tests that should not touch the filesystem."""
    con = duckdb.connect(":memory:")
    _populate(con, spec)
    return con


class Warehouse:
    """A read-only handle to the warehouse, safe to share across threads via cursors."""

    def __init__(self, connection: duckdb.DuckDBPyConnection) -> None:
        self._con = connection

    @classmethod
    def open(cls, path: Path) -> Warehouse:
        if not path.exists():
            raise FileNotFoundError(
                f"warehouse not found at {path} -- run `uv run observable-api build` first"
            )
        return cls(duckdb.connect(str(path), read_only=True))

    def close(self) -> None:
        self._con.close()

    def _query(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        cur = self._con.cursor()
        try:
            cur.execute(sql, params)
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
        finally:
            cur.close()

    def cohorts(self) -> list[dict[str, Any]]:
        rows = self._query(
            """
            SELECT cohort_week,
                   COUNT(DISTINCT user_id) AS users,
                   COUNT(*)                AS events
            FROM events
            GROUP BY cohort_week
            ORDER BY cohort_week
            """,
            [],
        )
        return [
            {
                "cohort_week": r["cohort_week"].isoformat(),
                "users": r["users"],
                "events": r["events"],
            }
            for r in rows
        ]

    def retention(self, cohort: dt.date) -> dict[str, Any] | None:
        rows = self._query(
            """
            WITH base AS (
                SELECT user_id, event_ts FROM events WHERE cohort_week = ?
            ),
            sized AS (SELECT COUNT(DISTINCT user_id) AS cohort_size FROM base)
            SELECT CAST(date_diff('week', ?, CAST(b.event_ts AS DATE)) AS INTEGER)
                       AS week_offset,
                   COUNT(DISTINCT b.user_id) AS active_users,
                   ANY_VALUE(s.cohort_size)  AS cohort_size
            FROM base b CROSS JOIN sized s
            GROUP BY week_offset
            HAVING week_offset >= 0
            ORDER BY week_offset
            """,
            [cohort, cohort],
        )
        if not rows:
            return None
        size = rows[0]["cohort_size"]
        return {
            "cohort_week": cohort.isoformat(),
            "cohort_size": size,
            "curve": [
                {
                    "week_offset": r["week_offset"],
                    "active_users": r["active_users"],
                    "retention_pct": round(100 * r["active_users"] / size, 2),
                }
                for r in rows
            ],
        }

    def funnel(self, cohort: dt.date, channel: str | None = None) -> dict[str, Any] | None:
        """Conversion through signup -> activated -> quiz_completed -> subscribed.

        Counted per *user reaching each step*, not as a strictly nested funnel: the
        generator plants users who subscribe without activating, and a nested funnel would
        silently drop them.
        """
        rows = self._query(
            """
            SELECT COUNT(DISTINCT user_id) AS signups,
                   COUNT(DISTINCT CASE WHEN event_name = 'activated'
                                       THEN user_id END) AS activated,
                   COUNT(DISTINCT CASE WHEN event_name = 'quiz_completed'
                                       THEN user_id END) AS quiz,
                   COUNT(DISTINCT CASE WHEN event_name = 'subscribed'
                                       THEN user_id END) AS subscribed
            FROM events
            WHERE cohort_week = ? AND (? IS NULL OR channel = ?)
            """,
            [cohort, channel, channel],
        )
        if not rows or not rows[0]["signups"]:
            return None
        r = rows[0]
        signups = r["signups"]
        steps = [
            ("signup", signups),
            ("activated", r["activated"]),
            ("quiz_completed", r["quiz"]),
            ("subscribed", r["subscribed"]),
        ]
        return {
            "cohort_week": cohort.isoformat(),
            "channel": channel,
            "steps": [
                {"step": name, "users": n, "pct_of_signups": round(100 * n / signups, 2)}
                for name, n in steps
            ],
        }
