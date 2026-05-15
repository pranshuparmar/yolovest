#!/bin/sh
# cert-heal-loop.sh
#
# Runs inside the cert-heal sidecar. Calls heal-cert-symlinks.sh in a
# tight loop so missing top-level symlinks are restored within seconds.
#
# Why a loop instead of a one-shot at boot: acme-companion removes the
# top-level <domain>.crt / <domain>.key symlinks as part of its renewal
# attempt (to prevent serving a stale cert mid-reissue). If the ACME
# challenge fails (e.g. port 80 unreachable from Let's Encrypt), the
# symlinks aren't restored — leaving the per-domain directory intact
# but the site dark. The loop catches this and re-creates the symlinks.

set -e

INTERVAL_SEC=${INTERVAL_SEC:-15}

while true; do
    /heal-cert-symlinks.sh >/dev/null || true
    sleep "$INTERVAL_SEC"
done
