#!/bin/sh
# Fix ownership of the Fly volume (mounted root-owned at /data on first boot),
# then drop to the unprivileged app user before exec'ing the server. If we're
# already non-root (e.g. local `docker run --user`), just exec the command.
set -e

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$(dirname "${DB_PATH:-/data/events.db}")"
  chown -R 1000:1000 /data 2>/dev/null || true
  exec gosu 1000:1000 "$@"
fi

exec "$@"
