# syntax=docker/dockerfile:1
FROM python:3.13-slim

# uv resolves from the committed lockfile, so the image gets the exact versions CI tested.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

WORKDIR /app

# Dependencies before source: editing a handler must not invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    WAREHOUSE_PATH=/app/data/warehouse.duckdb \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus

# The warehouse is generated at build time, not at boot: it is deterministic from a seed, so
# baking it in keeps container startup instant and every replica byte-identical.
RUN observable-api build

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=2).status == 200 else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["observable-api", "serve", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
