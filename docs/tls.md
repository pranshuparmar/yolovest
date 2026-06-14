> Detail extracted from the project overview (`CLAUDE.md`), which links here. Refer to it when you need this depth.

## TLS / nginx-proxy Reliability

The stack hosts the dashboard behind `nginxproxy/nginx-proxy` + `nginxproxy/acme-companion` (pinned versions in `docker-compose.yml`). Three defensive layers protect against the well-known cert-symlink failure mode where acme-companion deletes top-level `<domain>.crt` / `<domain>.key` symlinks during a failed renewal attempt:

1. **Pinned image versions** prevent silent upstream behaviour drift.
2. **`nginx/heal-cert-symlinks.sh`** runs as the nginx-proxy entrypoint before nginx boots, recreating any missing symlinks.
3. **`nginx/tls-healthcheck.sh`** marks the container unhealthy when `ssl_reject_handshake on;` is present in generated config or when a per-domain dir exists without its top-level symlinks — Docker's `restart: always` then re-runs the entrypoint heal. Worst-case dashboard downtime after a botched mid-life renewal is ~3 min (healthcheck interval 60s × 3 retries) which is fine for a single-user app.

Details and manual recovery steps in `docs/tls-recovery.md`.

The frontend nginx config (`frontend/nginx.conf`) uses Docker's embedded DNS (`resolver 127.0.0.11`) and a `proxy_pass` variable so that backend container restarts (which assign a new IP) don't strand cached DNS in the frontend's nginx and cause 502s.

