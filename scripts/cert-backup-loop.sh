#!/bin/sh
# cert-backup-loop.sh
#
# Runs inside the cert-backup sidecar. Snapshots /source (the certs
# volume, mounted read-only) into /backup as a timestamped tar.gz and
# prunes snapshots older than $RETENTION_DAYS. Backs up on container
# start and once every $INTERVAL_SEC seconds thereafter.
#
# Tunable via env in docker-compose.yml:
#   INTERVAL_SEC    seconds between backups (default 86400 = 24h)
#   RETENTION_DAYS  prune snapshots older than this (default 14)

set -e

SRC=${SRC:-/source}
DEST=${DEST:-/backup}
INTERVAL_SEC=${INTERVAL_SEC:-86400}
RETENTION_DAYS=${RETENTION_DAYS:-14}

mkdir -p "$DEST"

do_backup() {
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    out="$DEST/certs-${ts}.tar.gz"
    if [ -z "$(ls -A "$SRC" 2>/dev/null)" ]; then
        echo "[cert-backup] source is empty, skipping ($ts)"
        return
    fi
    tar -czf "$out" -C "$SRC" .
    size=$(du -h "$out" | cut -f1)
    echo "[cert-backup] wrote $out ($size)"
    find "$DEST" -name 'certs-*.tar.gz' -mtime "+$RETENTION_DAYS" -delete 2>/dev/null || true
}

while true; do
    do_backup
    sleep "$INTERVAL_SEC"
done
