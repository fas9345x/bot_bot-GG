#!/bin/sh
set -e

if [ "$(id -u)" = "0" ]; then
    DATA_DIR="/app/data"
    mkdir -p "$DATA_DIR"
    chown -R 1000:1000 "$DATA_DIR" || true
    exec gosu 1000:1000 "$@"
fi

exec "$@"