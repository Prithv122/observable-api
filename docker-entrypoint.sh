#!/bin/sh
# Prometheus multiprocess mode accumulates samples in mmap files on disk. They outlive the
# process that wrote them, so a restart with a dirty directory reports the previous run's
# counters added to this one's. Wipe it before the workers start.
set -e

if [ -n "$PROMETHEUS_MULTIPROC_DIR" ]; then
    rm -rf "$PROMETHEUS_MULTIPROC_DIR"
    mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
fi

exec "$@"
