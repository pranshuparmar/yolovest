#!/bin/sh
# tls-healthcheck.sh
#
# Marks the container unhealthy if the generated nginx config has
# regressed to the "ssl_reject_handshake on;" fallback (which causes
# all TLS handshakes to fail with "unrecognized name") OR if any
# per-domain cert directory exists without its expected top-level
# <domain>.crt / <domain>.key symlinks.
#
# Either condition triggers Docker's restart policy, giving the
# defensive heal step in the entrypoint another chance to run.

set -e

CONF_DIR=${CONF_DIR:-/etc/nginx/conf.d}
CERTS_DIR=${CERTS_DIR:-/etc/nginx/certs}

# Failure mode 1: generated config contains the no-cert fallback.
if grep -rq 'ssl_reject_handshake on' "$CONF_DIR" 2>/dev/null; then
    echo "unhealthy: ssl_reject_handshake fallback active" >&2
    exit 1
fi

# Failure mode 2: per-domain cert directory exists but top-level
# symlinks are missing.
if [ -d "$CERTS_DIR" ]; then
    for dir in "$CERTS_DIR"/*/; do
        [ -d "$dir" ] || continue
        domain=$(basename "$dir")
        if [ -f "$dir/fullchain.pem" ]; then
            for ext in crt key; do
                target="$CERTS_DIR/$domain.$ext"
                if [ ! -e "$target" ]; then
                    echo "unhealthy: missing $target" >&2
                    exit 1
                fi
            done
        fi
    done
fi

exit 0
