#!/bin/sh
# Starts as root only long enough to make /data writable, then drops to PUID:PGID
# (568:568 = TrueNAS "apps" user by default). If the container is already started
# as a non-root user (TrueNAS "Custom User"), it just runs.
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R "${PUID}:${PGID}" "$DATA_DIR" 2>/dev/null || echo "warning: could not chown $DATA_DIR"
    exec setpriv --reuid="${PUID}" --regid="${PGID}" --clear-groups "$@"
fi

if [ ! -w "$DATA_DIR" ]; then
    echo "ERROR: $DATA_DIR is not writable by uid $(id -u). Fix the dataset permissions or run the container as root." >&2
    exit 1
fi
exec "$@"
