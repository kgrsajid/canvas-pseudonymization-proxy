# DEPLOY — canvas-privacy-proxy on Dokku (commands only)

Run on YOUR Dokku host (`ssh <dokku-user>@<dokku-host>`, prefix dokku
commands with `sudo`). Replace `<dokku-host>`, `<dokku-user>`, and
`proxy.example.edu` below with your own infrastructure. Assumes the repo ships `nginx.conf.sigil`
(mTLS-hardened Dokku template) at the app root.

## 1. Create the app and the vault database

```bash
sudo dokku apps:create canvas-proxy
sudo dokku postgres:create canvas-proxy-vault
sudo dokku postgres:link canvas-proxy-vault canvas-proxy
```

## 2. Domains + TLS (letsencrypt)

```bash
sudo dokku domains:add canvas-proxy proxy.example.edu
sudo dokku letsencrypt:set canvas-proxy email <admin-email>
sudo dokku letsencrypt:enable canvas-proxy
sudo dokku letsencrypt:cron-job add
```

## 3. mTLS: install the client CA + custom nginx template

The repo's `nginx.conf.sigil` sets `ssl_verify_client on` against
`/var/lib/dokku/data/canvas-proxy/client-ca.pem` and forwards
`X-SSL-Client-Verify`/`X-SSL-Client-S-DN` to the app (which fail-closes on
anything but `SUCCESS`).

```bash
# On your workstation: create the proxy CA + issue the app's client cert
mkdir -p mtls && cd mtls
openssl genrsa -out client-ca.key 4096
openssl req -x509 -new -nodes -key client-ca.key -sha256 -days 1825 \
  -out client-ca.pem -subj "/CN=canvas-proxy-client-ca"
openssl genrsa -out app-client.key 2048
openssl req -new -key app-client.key -out app-client.csr \
  -subj "/CN=app-client"
openssl x509 -req -in app-client.csr -CA client-ca.pem -CAkey client-ca.key \
  -CAcreateserial -out app-client.crt -days 825 -sha256

# Copy the CA to the Dokku host
scp client-ca.pem <dokku-user>@<dokku-host>:/tmp/client-ca.pem
ssh <dokku-user>@<dokku-host>
sudo mkdir -p /var/lib/dokku/data/canvas-proxy
sudo mv /tmp/client-ca.pem /var/lib/dokku/data/canvas-proxy/client-ca.pem
sudo chown dokku:dokku /var/lib/dokku/data/canvas-proxy/client-ca.pem
sudo chmod 600 /var/lib/dokku/data/canvas-proxy/client-ca.pem
exit
```

Keep `app-client.crt`/`app-client.key` — the calling app presents them on
every request.

## 4. Configure environment

```bash
HMAC_KEY="b64:$(openssl rand -base64 48)"
sudo dokku config:set canvas-proxy \
  UPSTREAM_URL=http://canvas.example.internal \
  CANVAS_API_TOKEN="$(sudo dokku config:get <app-name> CANVAS_API_TOKEN)" \
  HMAC_KEY="$HMAC_KEY" \
  MTLS_CLIENT_CA=/var/lib/dokku/data/canvas-proxy/client-ca.pem \
  ALLOWED_PEER=<app-name>
# Record the fingerprint for ops (verify later via /proxy/v1/health):
echo -n "$HMAC_KEY" | shasum -a 256 | cut -c1-8
```

NOTE: `CANVAS_API_TOKEN` is copied live from the existing app at
deploy time — no real token is embedded in any file. After P2 cutover, remove
it from the app: `sudo dokku config:unset <app-name> CANVAS_API_TOKEN`.

`DATABASE_URL` is set automatically by `postgres:link` (the app accepts both
`postgresql://` and `postgresql+asyncpg://` forms). Do NOT set `VAULT_SQLITE` in
production.

## 5. Deploy

```bash
cd <path-to>/canvas-privacy-proxy   # this repo
tar --exclude='.venv' --exclude='data' --exclude='.env' \
    -czf /tmp/canvas-proxy.tar.gz .
scp /tmp/canvas-proxy.tar.gz <dokku-user>@<dokku-host>:/tmp/
ssh <dokku-user>@<dokku-host>
cat /tmp/canvas-proxy.tar.gz | sudo dokku git:from-archive canvas-proxy --   # first deploy: use git:load or see below
```

For the FIRST deploy `git:from-archive` requires the app to exist with a tar
containing the app at repo ROOT (not a subdirectory) — the tarball above
already satisfies that (created from inside the directory).

```bash
# Port mapping FIRST (Dokku defaults to 5000, app listens on $PORT/uvicorn):
sudo dokku ports:set canvas-proxy http:80:8000 https:443:8000

cd <path-to>/canvas-privacy-proxy
tar --exclude='.venv' --exclude='data' --exclude='.env' -czf /tmp/canvas-proxy.tar.gz .
scp /tmp/canvas-proxy.tar.gz <dokku-user>@<dokku-host>:/tmp/
ssh <dokku-user>@<dokku-host> 'cat /tmp/canvas-proxy.tar.gz | sudo dokku git:from-archive canvas-proxy --'
```

## 6. Verify nginx config & health

```bash
sudo dokku nginx:show-config canvas-proxy        # must show ssl_verify_client on
sudo nginx -t && sudo systemctl reload nginx
curl -s https://proxy.example.edu/proxy/v1/health      # without client cert -> TLS failure (expected)
curl -s --cert mtls/app-client.crt --key mtls/app-client.key \
  https://proxy.example.edu/proxy/v1/health
# expect: {"status":"ok","upstream_ok":true,"vault_entries":N,"key_fingerprint":"<8 hex>",...}
```

## 7. Selftest against the demo Canvas (http://canvas.example.internal)

```bash
# from a machine with the client cert:
BASE=https://proxy.example.edu
CERT="--cert mtls/app-client.crt --key mtls/app-client.key"

# a) health + upstream reachable
curl -s $CERT $BASE/proxy/v1/health | jq .

# b) roster sync (course 1) — populates the vault
curl -s $CERT -X POST $BASE/proxy/v1/sync-roster -H 'content-type: application/json' \
  -d '{"course_id": 1}' | jq .

# c) pseudonymized read: users list must contain P-xxxx ids and NO name/email fields
curl -s $CERT "$BASE/api/v1/courses/1/users?per_page=5" | jq '.[0]'

# d) fail-closed: unknown pseudonym -> 404 unknown_subject
curl -s -o /dev/null -w '%{http_code}\n' $CERT "$BASE/api/v1/users/P-ZZZZZZZZZZ/page_views"
# expect: 404

# e) no client cert -> connection rejected at TLS (mTLS enforced by nginx)
curl -s -o /dev/null -w '%{http_code}\n' $BASE/proxy/v1/health || echo "TLS handshake rejected (expected)"
```

All checks green => P1 parallel-run ready. Point the swarm app at the proxy
only at P2 cutover.

## 8. Ops (reference)

```bash
sudo dokku logs canvas-proxy -t                          # app logs (pseudonyms only, never names)
sudo dokku postgres:export canvas-proxy-vault > vault-backup.sql   # vault backup
sudo dokku postgres:connect canvas-proxy-vault            # audit export:
#   SELECT * FROM audit_log ORDER BY id DESC LIMIT 50;
sudo dokku letsencrypt:rotate canvas-proxy                # TLS cert renewal (or rely on cron-job)
```
