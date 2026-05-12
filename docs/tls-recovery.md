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

`nginx/heal-cert-symlinks.sh` is invoked as the container's
`entrypoint` before nginx boots. For every per-domain directory it
ensures the top-level symlinks exist, creating any that are missing.
Idempotent and safe to re-run.

This is a workaround for the failure mode, not a root-cause fix. It
keeps the site up while upstream bugs in nginx-proxy or
acme-companion get sorted out.

### 4. Periodic volume backups

A `cert-backup` sidecar runs as part of the compose stack. It
snapshots the `certs` named volume into `./backups/certs/` as a
timestamped tar.gz every 24h and prunes snapshots older than the
retention window. No host cron required — the container handles
scheduling itself.

The first snapshot is written immediately on container start, so a
fresh `docker compose up` produces a backup within seconds.

Tunable via env in `docker-compose.yml`:

```yaml
cert-backup:
  environment:
    - INTERVAL_SEC=86400    # seconds between snapshots
    - RETENTION_DAYS=14     # prune older than this
```

Force a fresh snapshot:

```sh
docker compose restart cert-backup
```

Inspect what's been backed up:

```sh
ls -lh ./backups/certs/
```

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

### Restore a backed-up volume (after wipe or corruption)

```sh
# Stop the proxy and acme-companion so nothing is writing during restore.
docker compose stop nginx-proxy letsencrypt

# Locate the most recent snapshot.
LATEST=$(ls -t backups/certs/certs-*.tar.gz | head -1)

# Restore into the named volume.
docker run --rm \
    -v yolovest_certs:/target \
    -v "$(realpath "$LATEST")":/snapshot.tar.gz:ro \
    alpine:3 \
    sh -c 'cd /target && tar -xzf /snapshot.tar.gz'

docker compose start nginx-proxy letsencrypt
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
