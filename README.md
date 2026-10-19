# Canvas Pseudonymization Proxy

**Institution-boundary pseudonymization proxy for the Canvas LMS API** — research apps see only `P-xxxx` pseudonyms, never names, emails, SIS ids, or real user ids.

Part of research project **AP26196023** (Narxoz University, Almaty, Kazakhstan) · [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## What is this?

A self-hosted, fail-closed HTTP proxy that sits between your **Canvas LMS** instance and a downstream application (e.g. a learning-analytics or research app). Every Canvas API response passes through a pseudonymization layer: real user ids are replaced with deterministic pseudonyms (`P-xxxx`) and all direct identifiers — names, emails, login ids, SIS ids, UUIDs, avatars, bios, pronouns — are stripped everywhere they appear, including deeply nested objects. Behavioral data (grades, submissions, enrollments, activity timestamps) passes through untouched.

It exists so institutions can grant third-party apps access to Canvas data while satisfying **data-protection-by-design obligations** (GDPR Art. 25/32; Kazakhstan Law №94-V "On Personal Data", Art. 17). The app behind the proxy never needs a Canvas API token and never sees PII; re-identification is possible only at the proxy, under the institution's own keys, with full audit logging.

**Who it's for:** university IT/security teams deploying research or analytics integrations on top of Canvas LMS.

## Architecture

```
                          ┌─────────────────────────────────────┐
                          │  Institution network (your infra)   │
                          │                                     │
 Student browser ──► Canvas LMS ──► [ THIS PROXY ] ──► Research App
                        │              │  ▲                    │
                        │              ▼  │                    │
                   (real names,      P-xxxx pseudonyms    (P-xxxx + behavioral
                    emails, SIS)     + behavioral data      data only — no PII)
                          │                                     │
                          │   proxy holds the ONLY Canvas       │
                          │   API token; vault stores only      │
                          │   pseudonym ↔ user-id mapping       │
                          └─────────────────────────────────────┘
```

- The **research app never sees PII** — it receives pseudonyms and behavioral data only.
- The **proxy holds the only Canvas API token**; the app authenticates to the proxy via mTLS and/or HMAC request signing.
- The **vault** (PostgreSQL in production, SQLite for dev) stores *only* the pseudonym ↔ `canvas_user_id` mapping and an append-only audit log — never names.

## How pseudonymization works

- **HMAC-SHA256** over a namespaced input: `"canvas-user:" + <canvas_user_id>`, keyed by `HMAC_KEY`.
- **Deterministic:** the same user id always yields the same pseudonym, on every request and across restarts — so joins and aggregations in the downstream app stay consistent.
- **Encoding:** `base32(HMAC)[:10]`, lowercased, prefixed `P-` (e.g. `P-a3f9k2m7x1`).
- **Stripped everywhere they appear** (including nested `enrollment[].user`, `submissions[].user`, conversations, etc.): `name`, `full_name`, `sortable_name`, `short_name`, `first_name`, `last_name`, `middle_name`, `preferred_name`, `email`, `login_id`, `sis_user_id`, `sis_login_id`, `avatar_url`, `bio`, `pronouns`, `uuid`.
- **Passes through untouched:** grades, scores, `workflow_state`, submissions, enrollments, page views, `last_activity_at`, `total_activity_time`, timestamps, time zone, locale, course/assignment names and ids.
- **Pagination cursors** (`Link` headers) are stripped — Canvas embeds base64 bookmarks containing real names + real ids. The app paginates with `?page=N` instead.
- **Non-JSON upstream responses** (CSV/gradebook exports) are refused with `501` — they cannot be safely rewritten.
- **Fail-closed:** if `HMAC_KEY` is missing, or no client-auth layer (mTLS / HMAC signing) is configured, the proxy **refuses to start**.

## Quick start (Docker)

```bash
git clone https://github.com/kgrsajid/canvas-pseudonymization-proxy.git
cd canvas-pseudonymization-proxy
cp .env.example .env
# Edit .env: set UPSTREAM_URL, CANVAS_API_TOKEN, HMAC_KEY
# For dev only: uncomment DEV_ALLOW_NO_AUTH=true
docker compose up -d
curl http://localhost:8080/proxy/v1/health
make selftest
```

Compose binds to `127.0.0.1:8080` and persists the SQLite vault in `./data/`. Terminate TLS in front (nginx/ingress) for anything beyond localhost.

## Quick start (without Docker)

```bash
# Requires Python 3.13 + uv
pip install uv
uv sync
cp .env.example .env
# Edit .env
uvicorn app.main:app --port 8080
```

## Environment variables

All configuration is env-driven (see [.env.example](.env.example) and [app/config.py](app/config.py)).

### Required

| Name | Required | Default | Description |
|---|---|---|---|
| `UPSTREAM_URL` | yes | `http://localhost:3000` | Base URL of your Canvas instance. |
| `CANVAS_API_TOKEN` | yes | — | Canvas API token held **only** by this proxy. Minimal scopes: users, courses, enrollments, submissions (read), conversations (write). |
| `HMAC_KEY` | yes | — | Pseudonymization key (base64 `b64:...` or raw). Generate on-site: `openssl rand -base64 48`. Never share. |
| `DATABASE_URL` | one of `DATABASE_URL` / `VAULT_SQLITE` | — | Production vault backend: `postgresql+asyncpg://...` |
| `VAULT_SQLITE` | one of `DATABASE_URL` / `VAULT_SQLITE` | `/srv/data/vault.db` (Docker) | Dev/demo vault backend: path to SQLite file. |

### Security

| Name | Required | Default | Description |
|---|---|---|---|
| `MTLS_CLIENT_CA` | one of `MTLS_CLIENT_CA` / `AUTH_HMAC_SECRET` | — | Path to CA bundle for client certs. TLS terminator must verify client certs and forward `X-SSL-Client-Verify` / `X-SSL-Client-S-DN`. |
| `ALLOWED_PEER` | no | — | Restrict to a single client-cert CN (empty = any cert signed by the CA). |
| `AUTH_HMAC_SECRET` | one of `MTLS_CLIENT_CA` / `AUTH_HMAC_SECRET` | — | Enable HMAC request signing: `X-Proxy-Auth: HMAC:<ts>.<hex>`. |
| `AUTH_REPLAY_WINDOW_S` | no | `60` | Replay window (±seconds) for HMAC timestamps. |
| `ALLOWED_IPS` | no | — | Optional comma-separated IP allowlist. |
| `DEV_ALLOW_NO_AUTH` | dev only | `false` | Escape hatch allowing startup with **no** auth layer — local selftest/dev only, never in production. |

### Tunables

| Name | Required | Default | Description |
|---|---|---|---|
| `NAMELESS_MODE` | no | `false` | Strip `{{placeholders}}` instead of live name substitution (outbound conversations). |
| `RESOLVE_RATE_LIMIT_PER_MIN` | no | `30` | Per-session rate limit for `/proxy/v1/resolve`. |
| `RESOLVE_DISPLAY_TTL_S` | no | `60` | Display-map TTL semantics for re-identification (documented for the app). |
| `UPSTREAM_RATE_LIMIT_PER_MIN` | no | `300` | Rate limit on calls forwarded to Canvas. |
| `UPSTREAM_TIMEOUT_S` | no | `30` | Upstream read timeout. |
| `UPSTREAM_CONNECT_TIMEOUT_S` | no | `10` | Upstream connect timeout. |
| `UPSTREAM_MAX_RETRIES` | no | `2` | Retries on transient upstream errors. |

## API endpoints

| Method + path | Description | Auth | Key behavior |
|---|---|---|---|
| `GET /proxy/v1/health` | Liveness + upstream/vault status | no | Reports `upstream_ok`, vault entry count, HMAC key fingerprint, backend type. |
| `GET /proxy/v1/stats` | Request counters | no | In-memory counters per event type. |
| `* /api/v1/{path}` (`GET/POST/PUT/PATCH/DELETE`) | Transparent Canvas passthrough | yes | User ids → `P-xxxx`; PII fields stripped everywhere; `Link` pagination headers removed; unknown pseudonyms in requests are rejected (fail closed). |
| `POST /api/v1/conversations` | Pseudonym-respecting message send | yes | Recipients arrive as `P-xxxx`; names fetched **live** from Canvas for substitution (`{{first_name}}`/`{{full_name}}`), never persisted; unknown recipients → dead-letter, never a fallback. |
| `POST /proxy/v1/resolve` | Display-time re-identification | yes + role gate | Instructor/TA role independently verified against Canvas; returns short-lived display map; rate-limited; fully audited; no name persisted or logged. |
| `POST /proxy/v1/map` | Real `canvas_user_id` → pseudonym | yes | Used at LTI launch so the app can key sessions on pseudonyms; mapping created lazily via deterministic HMAC; audited. |
| `POST /proxy/v1/sync-roster` | Bulk vault population for a course | yes | Pre-populates the pseudonym vault for all enrollments of a course; audited. |

All authenticated endpoints accept mTLS and/or HMAC request signing (whichever layers are configured).

## Testing

```bash
make selftest              # full suite (77 tests) + PASS/FAIL banner
uv run pytest tests/ -v    # verbose
```

The suite covers: the PII scanner, round-trip identity (`P-xxxx` → id → `P-xxxx`), determinism, fail-closed startup drills, placeholder leak detection, golden fixtures for response rewriting, request rewriting, and the vault on **both** backends (PostgreSQL and SQLite).

## Deployment

- **Docker:** `docker compose up -d` (see Quick start above).
- **Dokku:** see [DEPLOY.md](DEPLOY.md) — includes the mTLS-hardened `nginx.conf.sigil` template.
- **Any server (bare metal / systemd):** see [INSTALL.md](INSTALL.md), the full institution runbook — includes **both English and Russian** versions, secret generation, auth-layer setup, and operations.

Building a handoff tarball for an institution: `make dist` (runs the self-test, then packages everything needed to self-host).

## Security model

- **mTLS + HMAC request signing** — layered client authentication; the proxy refuses to start with neither configured (unless the dev-only escape hatch is set).
- **Vault stores only the pseudonym ↔ `canvas_user_id` mapping** — a pure two-column map; names are never stored.
- **Append-only audit log** — every resolve/map/roster event is recorded with caller identity and context.
- **No names ever stored, cached, or logged** — route code only ever logs pseudonyms and errors; names exist transiently in memory during outbound message substitution and display-time resolve.
- **Fail-closed everywhere** — missing key, missing auth layer, unknown pseudonym, or unparseable upstream payload → hard error, never a fallback to real data.

## Project context

Part of research project **AP26196023**: «Применение алгоритмов гибридного роевого интеллекта при разработке проактивных мультиагентных систем в цифровой образовательной среде» — *Application of hybrid swarm-intelligence algorithms in the development of proactive multi-agent systems for the digital educational environment.*

**Narxoz University, Almaty, Kazakhstan.**

## License

[MIT](LICENSE)
