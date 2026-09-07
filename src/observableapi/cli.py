"""Console entry point: build the warehouse, or serve the API."""

from __future__ import annotations

import argparse
import sys

from .config import get_settings
from .warehouse import GeneratorSpec, build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="observable-api", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build_cmd = sub.add_parser("build", help="Generate the DuckDB warehouse (deterministic).")
    build_cmd.add_argument("--weeks", type=int, default=GeneratorSpec.weeks)
    build_cmd.add_argument("--users-per-week", type=int, default=GeneratorSpec.users_per_week)

    serve_cmd = sub.add_parser("serve", help="Run the API with uvicorn.")
    serve_cmd.add_argument("--host", default="127.0.0.1")
    serve_cmd.add_argument("--port", type=int, default=8000)
    serve_cmd.add_argument("--workers", type=int, default=1)

    args = parser.parse_args(argv)
    settings = get_settings()

    if args.command == "build":
        spec = GeneratorSpec(weeks=args.weeks, users_per_week=args.users_per_week)
        rows = build(settings.warehouse_path, spec)
        size_mb = settings.warehouse_path.stat().st_size / 1_048_576
        print(
            f"Built {settings.warehouse_path} - {rows:,} events across {spec.weeks} cohorts "
            f"({size_mb:.1f} MB)"
        )
        return 0

    import uvicorn

    uvicorn.run(
        "observableapi.app:build_default_app",
        factory=True,
        host=args.host,
        port=args.port,
        workers=args.workers,
        # Logging is configured by the app itself (structlog, JSON); uvicorn's own dictConfig
        # would replace those handlers and split the output into two formats.
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
