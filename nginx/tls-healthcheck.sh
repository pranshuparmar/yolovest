#!/bin/sh
# tls-healthcheck.sh
#
# Marks the container unhealthy when a domain that HAS an issued
# certificate isn't actually being served over TLS:
#
#   1. its per-domain cert directory exists without the top-level
#      <domain>.crt / <domain>.key symlinks nginx-proxy looks for, or
#   2. the generated config has no `listen 443` server block for it, or
#      that block carries `ssl_reject_handshake on;` (handshakes fail
#      with "unrecognized name").
#
# `ssl_reject_handshake on;` in the catch-all `server_name _` block is
# NOT a failure: nginx-proxy always emits it there when no default.crt
# exists, so unknown hostnames are refused instead of being handed a
# certificate. Grepping the whole config for it flagged every healthy
# deploy as unhealthy.
#
# Either condition triggers Docker's restart policy, giving the
# defensive heal step in the entrypoint another chance to run.

set -e

CONF_DIR=${CONF_DIR:-/etc/nginx/conf.d}
CERTS_DIR=${CERTS_DIR:-/etc/nginx/certs}

# Print "ok" if some server block naming $1 listens on 443 without
# rejecting handshakes, "reject" if a 443 block for it rejects them,
# nothing if no 443 block for it exists.
tls_state_for() {
    cat "$CONF_DIR"/*.conf 2>/dev/null | awk -v want="$1" '
        function flush() {
            if (match_name && tls) { if (reject) bad = 1; else good = 1 }
        }
        /^[[:space:]]*server[[:space:]]*\{/ { flush(); match_name = 0; tls = 0; reject = 0; next }
        /^[[:space:]]*server_name[[:space:]]/ {
            for (i = 2; i <= NF; i++) { n = $i; sub(/;$/, "", n); if (n == want) match_name = 1 }
        }
        /^[[:space:]]*listen[[:space:]].*443/ { tls = 1 }
        /^[[:space:]]*ssl_reject_handshake[[:space:]]+on/ { reject = 1 }
        END { flush(); if (bad) print "reject"; else if (good) print "ok" }
    '
}

[ -d "$CERTS_DIR" ] || exit 0

for dir in "$CERTS_DIR"/*/; do
    [ -d "$dir" ] || continue
    [ -f "$dir/fullchain.pem" ] || continue
    domain=$(basename "$dir")

    for ext in crt key; do
        if [ ! -e "$CERTS_DIR/$domain.$ext" ]; then
            echo "unhealthy: missing $CERTS_DIR/$domain.$ext" >&2
            exit 1
        fi
    done

    state=$(tls_state_for "$domain")
    if [ "$state" != "ok" ]; then
        echo "unhealthy: $domain has a certificate but no working TLS server block (${state:-missing})" >&2
        exit 1
    fi
done

exit 0
