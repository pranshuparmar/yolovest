# TLS / HTTPS Reliability

This document covers the failure mode where the dashboard becomes
unreachable over HTTPS even though all containers report healthy, plus
the layered preventive measures and recovery procedure.

## Symptom

- Browser shows `ERR_SSL_PROTOCOL_ERROR` or similar.
- `curl -vk https://<domain>` returns `TLS alert, unrecognized name`.
- Generated nginx config inside the proxy contains
  `ssl_reject_handshake on;`.
- `docker logs nginx-proxy` shows no upstream errors.

## Root cause

nginx-proxy expects flat-file cert symlinks at the top of
`/etc/nginx/certs/`:

```
/etc/nginx/certs/<domain>.crt   -> <domain>/fullchain.pem
/etc/nginx/certs/<domain>.key   -> <domain>/key.pem
```

acme-companion creates these symlinks alongside the per-domain
directory it issues into. If those top-level symlinks go missing
while the per-domain directory survives — most commonly across a
`docker compose down && up` cycle — nginx-proxy can't find
certificates by its expected name pattern and falls back to a config
containing `ssl_reject_handshake on;`. Every TLS connection is then
refused with "unrecognized name" and the site is dark.

## Preventive measures (in place)

The defense is layered rather than reliant on any one mechanism.

### 1. Pinned image versions

`docker-compose.yml` pins both images to specific tags rather than
implicit `:latest`:

```yaml
nginxproxy/nginx-proxy:1.10.1
nginxproxy/acme-companion:2.6.3
```

This prevents silent upstream behaviour changes between deploys. Bump
the tags deliberately after testing.

### 2. Healthcheck that fails on the broken state

`nginx/tls-healthcheck.sh` runs as the nginx-proxy container's
healthcheck. It marks the container unhealthy when either:

- the generated config contains `ssl_reject_handshake on;`, or
- a per-domain cert directory exists without its expected top-level
  `<domain>.crt` / `<domain>.key` symlinks.

An unhealthy state triggers Docker's restart policy, which in turn
runs the heal step on the next start.

### 3. Defensive cert-symlink heal on container start

`nginx/heal-cert-symlinks.sh` is invoked as the nginx-proxy
`entrypoint` before nginx boots. For every per-domain directory it
ensures the top-level symlinks exist, creating any that are missing.
Idempotent and safe to re-run.

Combined with the healthcheck above, this is the full recovery
loop: a botched mid-life renewal that leaves the symlinks missing
flips nginx-proxy unhealthy within ~3 minutes (60s interval × 3
retries), Docker's `restart: always` restarts the container, and
the entrypoint heal recreates the symlinks before nginx boots.

If you ever see `Verification error... Timeout during connect` in
the letsencrypt container's logs, that's the trigger for this
failure mode — check that port 80 is reachable from the public
internet so ACME challenges can complete.

If you ever lose the `certs` named volume (rare — would need an
explicit `docker volume rm` or disk corruption), acme-companion
re-issues from Let's Encrypt automatically on next boot. The
default rate limit (50 certs per registered domain per week) is
well above what a single-domain deploy could ever burn through.

## Manual recovery

### Recover from a missing-symlink state (no volume restore needed)

```sh
docker exec -it nginx-proxy sh
cd /etc/nginx/certs
for d in */; do
    domain=${d%/}
    [ -f "$d/fullchain.pem" ] || continue
    ln -sf "$d/fullchain.pem" "$domain.crt"
    ln -sf "$d/key.pem"       "$domain.key"
    [ -f "$d/chain.pem" ] && ln -sf "$d/chain.pem" "$domain.chain.pem"
done
nginx -s reload
exit

docker restart nginx-proxy
```

### Re-issue from Let's Encrypt (after volume loss)

If the `certs` named volume was deleted or corrupted:

```sh
docker compose up -d
```

acme-companion notices there's no cert for `LETSENCRYPT_HOST` and
runs the ACME challenge to issue a fresh one. Typical end-to-end
time is under a minute. The only prerequisite is that port 80 is
reachable from the public internet so the HTTP-01 challenge can
complete.

If you want a one-off snapshot before risky maintenance:

```sh
docker run --rm \
    -v yolovest_certs:/source:ro \
    -v "$PWD/backups/certs":/backup \
    alpine:3 \
    sh -c 'cd /source && tar -czf "/backup/certs-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" .'
```

Verify HTTPS works:

```sh
curl -vk https://<domain> 2>&1 | head -20
```

## Considered, deferred

### Migrating off nginx-proxy + acme-companion

A move to Caddy or Traefik would eliminate the failure mode entirely
because both manage TLS via a single in-process state machine rather
than coordinating two separate containers through a shared volume of
symlinks. Trade-off: a one-time configuration migration plus
learning a different proxy DSL.

Tracked separately as a P3 item; the layered measures above are
sufficient for current scale.
