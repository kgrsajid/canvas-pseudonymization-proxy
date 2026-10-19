# INSTALL — Canvas Privacy Proxy (Institution Runbook)

Pseudonymization boundary for Canvas LMS. The research app behind this proxy
sees only pseudonyms (`P-xxxx`) and behavioral data. Names, emails, SIS ids,
and real Canvas user ids never leave this host. Re-identification is possible
only here, under your keys, with full audit logging.

## 1. Requirements

- Any Docker host that can reach your Canvas instance (ideally the Canvas host
  itself or the same VLAN).
- Either a PostgreSQL instance (production) or nothing extra (SQLite for
  dev/demo).
- Python 3.13 only if running without Docker.

## 2. Generate secrets (on-site — never share them)

```bash
# HMAC pseudonymization key (48 random bytes, base64):
echo "HMAC_KEY=b64:$(openssl rand -base64 48)"

# Optional X-Proxy-Auth signing secret:
echo "AUTH_HMAC_SECRET=$(openssl rand -hex 32)"
```

Create a **dedicated Canvas API token** with minimal scopes:
Canvas admin → user Settings → New Access Token (or a developer key with
scopes: `url:GET|/api/v1/users`, `url:GET|/api/v1/courses`, course
enrollments/submissions read, `url:POST|/api/v1/conversations`). Your security
team keeps custody; the token lives ONLY in this proxy's environment.

For mTLS, issue the calling application a client certificate from your CA (or
a dedicated proxy CA) and set `MTLS_CLIENT_CA` to the CA bundle.

## 3. Configure & start

The proxy **refuses to start without an auth layer** (mTLS or HMAC request
signing) — serving Canvas data unauthenticated would be a total privacy
breach. Pick ONE:

```bash
cp .env.example .env
$EDITOR .env          # set UPSTREAM_URL, CANVAS_API_TOKEN, HMAC_KEY, and ONE vault backend

# Option A — Docker compose DEV/demo only (no client auth on localhost):
#    uncomment DEV_ALLOW_NO_AUTH=true in .env  (see .env.example warning;
#    NEVER set this in production)

# Option B — PRODUCTION: enable HMAC request signing (in addition to / instead
#    of mTLS):
echo "AUTH_HMAC_SECRET=$(openssl rand -hex 32)" >> .env

docker compose up -d
curl -s http://localhost:8080/proxy/v1/health   # expect {"status":"ok", ...}
```

Note the `key_fingerprint` — record it (ops can verify which key is live at
any time via the health endpoint without ever exposing the key).

## 4. Built-in self-test

```bash
make selftest   # full test suite: PII scanner, round-trip recipient identity,
                # determinism, fail-closed drills, placeholder-leak checks
```

All tests must pass. The suite includes an automated scanner that asserts
ZERO forbidden fields (name/email/login/SIS/uuid/avatar/bio/pronouns) in proxy
output, and the scanner itself is tested against planted violations.

## 5. Point the app at the proxy

Hand the application team: the proxy URL + their mTLS client certificate.
Your token, HMAC key, and vault never cross the boundary.

## 6. Operations

- **Vault backup:** standard Postgres tooling (`pg_dump`), or copy the SQLite
  file while the proxy is stopped. Losing the vault is recoverable — forward
  mapping `id → P-xxxx` is pure HMAC; re-run `POST /proxy/v1/sync-roster`
  after restore. Losing the KEY is not — back it up separately (two-person
  rule where supported).
- **Key rotation:** only BETWEEN study periods. New key ⇒ all pseudonyms
  change atomically; re-sync the roster afterwards. Never rotate mid-semester.
- **Audit export:** `SELECT * FROM audit_log;` — every map/resolve/send/
  dead_letter/resolve_denied event, append-only. This is the artifact for
  your IRB/ethics committee.
- **Honest failure:** if upstream Canvas is down, the proxy returns 5xx — it
  never fabricates data.

---

# УСТАНОВКА — Прокси псевдонимизации Canvas (руководство для вуза)

Граница деперсонализации для Canvas LMS. Исследовательское приложение видит
только псевдонимы (`P-xxxx`) и поведенческие данные. ФИО, email, SIS-id и
реальные id пользователей Canvas никогда не покидают ваш сервер.
Реидентификация возможна только здесь, под вашими ключами, с полным
аудит-журналом (требование ст. 17 ЗРК №94-V о деперсонализации данных,
передаваемых для научных исследований).

## 1. Требования

- Любой Docker-хост с доступом к вашему Canvas (лучше — сам сервер Canvas
  или та же VLAN).
- PostgreSQL (прод) — или ничего дополнительно (SQLite для тестов/демо).

## 2. Генерация секретов (только на вашей стороне)

```bash
echo "HMAC_KEY=b64:$(openssl rand -base64 48)"
echo "AUTH_HMAC_SECRET=$(openssl rand -hex 32)"
```

Создайте **отдельный токен Canvas API** с минимальными правами (чтение
пользователей/курсов/зачислений/сдаваемых работ; отправка conversations).
Токен хранится ТОЛЬКО в окружении прокси.

Для mTLS выпишите приложению клиентский сертификат от вашего CA и укажите
`MTLS_CLIENT_CA`.

## 3. Настройка и запуск

Прокси **не стартует без слоя аутентификации** (mTLS или HMAC-подпись
запросов) — отдавать данные Canvas без аутентификации нельзя. Выберите ОДИН
вариант:

```bash
cp .env.example .env
$EDITOR .env        # UPSTREAM_URL, CANVAS_API_TOKEN, HMAC_KEY + один бэкенд хранилища

# Вариант А — DEV/демо через docker compose (localhost, без клиентской аутентификации):
#    раскомментируйте DEV_ALLOW_NO_AUTH=true в .env (предупреждение в .env.example;
#    в продакшене НИКОГДА не включать)
# Вариант B — ПРОДАКШЕН: HMAC-подпись запросов (в дополнение к / вместо mTLS):
echo "AUTH_HMAC_SECRET=$(openssl rand -hex 32)" >> .env

docker compose up -d
curl -s http://localhost:8080/proxy/v1/health
```

Запишите `key_fingerprint` — по нему ops всегда проверит, какой ключ активен.

## 4. Встроенный самотест

```bash
make selftest
```

Все тесты должны пройти. В комплекте автоматический сканер ПДн: ноль
запрещённых полей (ФИО/email/логин/SIS/uuid/аватар/био/местоимения) на выходе
прокси; сам сканер проверен на подставленных нарушениях.

## 5. Подключение приложения

Передайте команде приложения: URL прокси + их клиентский сертификат mTLS.
Ваш токен, ключ и хранилище границу не пересекают.

## 6. Эксплуатация

- **Резервное копирование хранилища:** штатный `pg_dump` (или копия
  SQLite-файла при остановленном прокси). Потеря хранилища восстановима
  (прямое отображение — чистый HMAC, перезапустите `sync-roster`). Потеря
  КЛЮЧА — нет: храните ключ отдельно.
- **Ротация ключа:** только МЕЖДУ периодами исследования; все псевдонимы
  меняются атомарно, после — повторный `sync-roster`. Внутри семестра не
  вращать.
- **Экспорт аудита:** `SELECT * FROM audit_log;` — для этического комитета.
- **Честные отказы:** при недоступности Canvas прокси возвращает 5xx и ничего
  не выдумывает.
