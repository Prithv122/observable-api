"""The console entry point.

``build`` is tested for real -- it writes a file and the file is queryable afterwards.
``serve`` is tested by intercepting ``uvicorn.run``: starting a server in a unit test would
prove nothing except that a port was free.
"""

from __future__ import annotations

import pytest

from observableapi import cli
from observableapi.config import get_settings
from observableapi.warehouse import Warehouse


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point the cached settings at a temp warehouse for one test."""
    monkeypatch.setenv("WAREHOUSE_PATH", str(tmp_path / "wh.duckdb"))
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6380/15")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_build_creates_a_queryable_warehouse(isolated_settings, capsys) -> None:
    exit_code = cli.main(["build", "--weeks", "2", "--users-per-week", "50"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "events across 2 cohorts" in output

    warehouse = Warehouse.open(isolated_settings.warehouse_path)
    try:
        cohorts = warehouse.cohorts()
    finally:
        warehouse.close()
    assert len(cohorts) == 2
    assert cohorts[0]["users"] == 50


def test_build_is_idempotent(isolated_settings) -> None:
    """Rebuilding replaces the warehouse rather than appending to it."""
    cli.main(["build", "--weeks", "2", "--users-per-week", "20"])
    first = isolated_settings.warehouse_path.stat().st_size
    cli.main(["build", "--weeks", "2", "--users-per-week", "20"])

    assert isolated_settings.warehouse_path.stat().st_size == first


def test_serve_passes_host_port_and_workers_to_uvicorn(isolated_settings, monkeypatch) -> None:
    captured = {}

    def fake_run(target, **kwargs):
        captured["target"] = target
        captured.update(kwargs)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)

    exit_code = cli.main(["serve", "--host", "0.0.0.0", "--port", "9001", "--workers", "3"])

    assert exit_code == 0
    assert captured["target"] == "observableapi.app:build_default_app"
    assert captured["factory"] is True
    assert (captured["host"], captured["port"], captured["workers"]) == ("0.0.0.0", 9001, 3)
    # The app configures structlog itself; letting uvicorn install its dictConfig would
    # split the output into two different formats.
    assert captured["log_config"] is None


def test_no_subcommand_is_an_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])

    assert exc.value.code == 2
