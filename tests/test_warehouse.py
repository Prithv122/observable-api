"""The read layer.

The retention test recomputes the answer from the raw rows in plain Python rather than
comparing the SQL to a frozen fixture. A fixture captured from the same query it is meant to
check only proves the query has not changed -- not that it was ever right.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from observableapi.warehouse import (
    GeneratorSpec,
    Warehouse,
    _generate_rows,
    build,
    build_in_memory,
    cohort_weeks,
)

SMALL = GeneratorSpec(weeks=3, users_per_week=80, max_followup_weeks=4)


def test_generation_is_deterministic() -> None:
    assert _generate_rows(SMALL) == _generate_rows(SMALL)


def test_build_writes_a_file_and_reports_row_count(tmp_path) -> None:
    path = tmp_path / "nested" / "warehouse.duckdb"
    rows = build(path, SMALL)

    assert path.exists()
    assert rows == len(_generate_rows(SMALL))

    wh = Warehouse.open(path)
    try:
        assert len(wh.cohorts()) == SMALL.weeks
    finally:
        wh.close()


def test_open_missing_warehouse_explains_how_to_fix_it(tmp_path) -> None:
    try:
        Warehouse.open(tmp_path / "absent.duckdb")
    except FileNotFoundError as exc:
        assert "observable-api build" in str(exc)
    else:  # pragma: no cover - the call above must raise
        raise AssertionError("expected FileNotFoundError")


def test_cohorts_lists_every_week_with_its_size(warehouse: Warehouse, spec) -> None:
    cohorts = warehouse.cohorts()

    assert [c["cohort_week"] for c in cohorts] == [d.isoformat() for d in cohort_weeks(spec)]
    assert all(c["users"] == spec.users_per_week for c in cohorts)
    assert all(c["events"] > c["users"] for c in cohorts)


def test_retention_matches_an_independent_python_computation(spec) -> None:
    """Recompute the curve from the generator's own rows, without touching SQL."""
    cohort = cohort_weeks(spec)[1]
    rows = [r for r in _generate_rows(spec) if r[1] == cohort]

    by_offset: dict[int, set[int]] = defaultdict(set)
    for user_id, cohort_week, event_ts, _name, _channel in rows:
        offset = (event_ts.date() - cohort_week).days // 7
        if offset >= 0:
            by_offset[offset].add(user_id)
    size = len({r[0] for r in rows})

    wh = Warehouse(build_in_memory(spec))
    try:
        result = wh.retention(cohort)
    finally:
        wh.close()

    assert result is not None
    assert result["cohort_size"] == size
    assert {p["week_offset"]: p["active_users"] for p in result["curve"]} == {
        offset: len(users) for offset, users in by_offset.items()
    }


def test_retention_starts_at_full_cohort_and_decays(warehouse: Warehouse, spec) -> None:
    result = warehouse.retention(cohort_weeks(spec)[0])

    assert result is not None
    curve = result["curve"]
    assert curve[0]["week_offset"] == 0
    assert curve[0]["retention_pct"] == 100.0
    assert curve[-1]["active_users"] < curve[0]["active_users"]


def test_retention_of_an_unknown_cohort_is_none(warehouse: Warehouse) -> None:
    assert warehouse.retention(dt.date(2001, 1, 1)) is None


def test_funnel_is_not_strictly_nested(warehouse: Warehouse, spec) -> None:
    """Some users subscribe without ever activating -- planted by the generator.

    A funnel implemented as a chain of nested subsets would report fewer subscribers than
    actually exist. This asserts the planted behaviour is present, so the test would fail
    loudly if the generator stopped producing it.
    """
    cohort = cohort_weeks(spec)[0]
    rows = [r for r in _generate_rows(spec) if r[1] == cohort]
    activated = {r[0] for r in rows if r[3] == "activated"}
    subscribed = {r[0] for r in rows if r[3] == "subscribed"}

    assert subscribed - activated, "generator should plant subscribers who never activated"

    result = warehouse.funnel(cohort)
    assert result is not None
    steps = {s["step"]: s["users"] for s in result["steps"]}
    assert steps["subscribed"] == len(subscribed)
    assert steps["signup"] == spec.users_per_week
    assert steps["activated"] == len(activated)


def test_funnel_channel_filter_narrows_the_cohort(warehouse: Warehouse, spec) -> None:
    cohort = cohort_weeks(spec)[0]

    everyone = warehouse.funnel(cohort)
    organic = warehouse.funnel(cohort, channel="organic")

    assert everyone is not None and organic is not None
    assert organic["channel"] == "organic"
    assert organic["steps"][0]["users"] < everyone["steps"][0]["users"]


def test_funnel_of_an_unknown_cohort_or_channel_is_none(warehouse: Warehouse, spec) -> None:
    assert warehouse.funnel(dt.date(2001, 1, 1)) is None
    assert warehouse.funnel(cohort_weeks(spec)[0], channel="carrier_pigeon") is None


def test_percentages_are_relative_to_signups(warehouse: Warehouse, spec) -> None:
    result = warehouse.funnel(cohort_weeks(spec)[0])

    assert result is not None
    signups = result["steps"][0]["users"]
    for step in result["steps"]:
        assert step["pct_of_signups"] == round(100 * step["users"] / signups, 2)
