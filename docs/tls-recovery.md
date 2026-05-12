# TLS / HTTPS Recovery

## Symptom

The dashboard hostname becomes unreachable over HTTPS even though DNS,
firewall rules, and all containers are healthy.

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
directory it issues into:

```
/etc/nginx/certs/<domain>/cert.pem
/etc/nginx/certs/<domain>/chain.pem
/etc/nginx/certs/<domain>/fullchain.pem
/etc/nginx/certs/<domain>/key.pem
```

If the symlinks ever go missing while the per-domain directory
survives — most commonly across `docker compose down && up` cycles —
nginx-proxy can't find certificates by its expected name pattern and
falls back to a config containing `ssl_reject_handshake on;`.

## Preventive measure (already in place)

`nginx/heal-cert-symlinks.sh` runs as the nginx-proxy container
entrypoint before nginx boots. It iterates every
`/etc/nginx/certs/<domain>/` directory and ensures the expected
top-level symlinks exist, creating any that are missing. Idempotent
and safe to re-run.

The wiring lives in `docker-compose.yml` under the `nginx-proxy`
service's `entrypoint:` override.

## Manual recovery (if the preventive measure is bypassed or fails)

Enter the proxy container and recreate the symlinks by hand:

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
```

Verify HTTPS works again:

```sh
curl -vk https://<domain> 2>&1 | head -20
```

If TLS handshake still fails, restart nginx-proxy to force config
regeneration:

```sh
docker restart nginx-proxy
```

## Related

- nginx-proxy issue tracker:
  <https://github.com/nginx-proxy/nginx-proxy/issues>
- acme-companion docs:
  <https://github.com/nginx-proxy/acme-companion>
