#!/bin/sh
# Adjust the runtime user to PUID/PGID and drop privileges when started as root.
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" != "0" ]; then
    # Already running unprivileged; nothing to adjust.
    exec "$@"
fi

if getent group appuser >/dev/null 2>&1; then
    groupmod -o -g "$PGID" appuser
else
    groupadd -o -g "$PGID" appuser
fi
usermod -o -u "$PUID" -g "$PGID" appuser

mkdir -p "${DATA_DIR:-/data}/logs" "${DATA_DIR:-/data}/home" \
    "${DATA_DIR:-/data}/cache" "${DOWNLOAD_DIR:-/downloads}"

# Only fix ownership on the mount roots; recursing through a large music
# library on every start would be slow.
chown -R "$PUID:$PGID" "${DATA_DIR:-/data}" 2>/dev/null || true
chown "$PUID:$PGID" "${DOWNLOAD_DIR:-/downloads}" 2>/dev/null || true

exec setpriv --reuid "$PUID" --regid "$PGID" --init-groups "$@"
