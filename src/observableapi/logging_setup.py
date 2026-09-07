"""structlog configuration.

The point of this module is the request id. A log line that cannot be tied back to the
request that produced it is nearly useless during an incident, so ``request_id`` is bound
into a contextvar by the middleware and picked up automatically by *every* log call for the
rest of that request -- including calls made deep in the cache or warehouse layer, which
know nothing about HTTP and should not have to.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

import structlog


def configure_logging(
    level: str = "INFO", json_output: bool = True, stream: TextIO | None = None
) -> None:
    """Configure structlog and route the stdlib root logger through it.

    Uvicorn and DuckDB log through the stdlib; without the ProcessorFormatter below their
    lines would come out in a different format from ours, which defeats the purpose of
    structured logging the moment you try to grep a production log.

    ``stream`` exists so the tests can read back what was actually emitted. Logging that is
    never asserted on is logging you find out is broken during an incident.
    """
    stream = stream if stream is not None else sys.stdout
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        # structlog's usual advice is to cache the bound logger on first use. Not here: a
        # cached logger keeps the processor chain and output stream it was built with, and
        # ignores every later call to this function. Module-level loggers (``cache.py``,
        # ``middleware.py``) are bound the first time anything logs, which in the test suite
        # is before the test that wants to read their output -- so caching silently sends
        # those lines to a stream nobody is looking at. Rebinding per call costs a
        # dictionary copy against a request that already spends milliseconds in Redis and
        # DuckDB.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Uvicorn installs its own handlers; make them defer to the root handler above.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        stdlib_logger = logging.getLogger(name)
        stdlib_logger.handlers = []
        stdlib_logger.propagate = True
