#!/bin/sh
# heal-cert-symlinks.sh
#
# Ensure the flat-file cert symlinks nginx-proxy looks for exist for every
# certificate directory created by acme-companion. Runs as a defensive
# pre-start step inside nginx-proxy: if a previous container lifecycle
# left /etc/nginx/certs/<domain>/ in place but the top-level
# <domain>.crt / <domain>.key symlinks missing, nginx-proxy falls back to
# `ssl_reject_handshake on;` and serves a TLS alert "unrecognized name"
# to every client. Healing the symlinks before nginx boots prevents
# that failure mode from surviving a `docker compose down && up`.
#
# Idempotent: only writes symlinks that are missing or point to the
# wrong target. Safe to run repeatedly.

set -e

CERTS_DIR=${CERTS_DIR:-/etc/nginx/certs}

if [ ! -d "$CERTS_DIR" ]; then
    echo "[cert-heal] $CERTS_DIR does not exist; nothing to heal" >&2
    exit 0
fi

cd "$CERTS_DIR"

heal_link() {
    domain=$1
    src=$2
    dst=$3
    [ -f "$domain/$src" ] || return 0
    if [ ! -L "$dst" ] || [ "$(readlink "$dst")" != "$domain/$src" ]; then
        ln -sf "$domain/$src" "$dst"
        echo "[cert-heal] linked $dst -> $domain/$src"
    fi
}

for dir in */; do
    [ -d "$dir" ] || continue
    domain=${dir%/}
    [ -f "$dir/fullchain.pem" ] || continue

    heal_link "$domain" fullchain.pem  "$domain.crt"
    heal_link "$domain" key.pem        "$domain.key"
    heal_link "$domain" chain.pem      "$domain.chain.pem"
    heal_link "$domain" dhparam.pem    "$domain.dhparam.pem"
done

echo "[cert-heal] done"
