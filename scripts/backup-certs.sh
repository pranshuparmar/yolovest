#!/usr/bin/env bash
# backup-certs.sh — snapshot the nginx-proxy `certs` named volume.
#
# Writes a timestamped tar.gz to $BACKUP_DIR and prunes snapshots older
# than $RETENTION_DAYS. Designed to be run from host cron (e.g. daily)
# so a corrupted or wiped certs volume can be restored without
# re-issuing certificates from Let's Encrypt (which has rate limits
# per cert name per week).
#
# Restore procedure: see docs/tls-recovery.md.

set -euo pipefail

VOLUME=${VOLUME:-yolovest_certs}
BACKUP_DIR=${BACKUP_DIR:-./backups/certs}
RETENTION_DAYS=${RETENTION_DAYS:-14}

mkdir -p "$BACKUP_DIR"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$BACKUP_DIR/certs-${TS}.tar.gz"

# Run tar inside a throwaway alpine container with the volume mounted
# read-only so we don't depend on host-side permissions on the volume.
docker run --rm \
    -v "$VOLUME":/source:ro \
    -v "$(realpath "$BACKUP_DIR")":/backup \
    alpine:3 \
    tar -czf "/backup/$(basename "$OUT")" -C /source .

# Prune snapshots older than retention window.
find "$BACKUP_DIR" -name 'certs-*.tar.gz' -mtime "+$RETENTION_DAYS" -delete

echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))"
