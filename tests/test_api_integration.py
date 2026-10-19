"""End-to-end HTTP tests through the FastAPI app with a mocked upstream Canvas.

Covers the §12 review-gate essentials:
- round-trip recipient identity with a PERMUTED id set (lesson-48 pattern),
- determinism across 3 calls + simulated restart,
- fail-closed drills (unknown P-id 404, dead-letter no-send, resolve 403/401),
- placeholder leak checks (no {{...}} delivered, no names persisted/logged).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

import app.main as main
from app.pseudonym import compute_pseudonym

UPSTREAM = "http://canvas.test"
KEY = "e" * 48  # test HMAC key (settings-level)


def _hmac_header(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"HMAC:{ts}.{mac}"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSTREAM_URL", UPSTREAM)
    monkeypatch.setenv("CANVAS_API_TOKEN", "test-token")
    monkeypatch.setenv("HMAC_KEY", KEY)
    monkeypatch.setenv("VAULT_SQLITE", str(tmp_path / "vault.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NAMELESS_MODE", raising=False)
    monkeypatch.delenv("MTLS_CLIENT_CA", raising=False)
    monkeypatch.delenv("AUTH_HMAC_SECRET", raising=False)
    monkeypatch.setenv("DEV_ALLOW_NO_AUTH", "true")  # no auth layer in this test
    main.get_settings.cache_clear()
    await main._init_state()
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://proxy.test") as c:
        yield c
    if main._state:
        await main._state.vault.close()
        await main._state.canvas.close()
        main._state = None
    main.get_settings.cache_clear()


# Synthetic roster (all identities fictional): PERMUTED ids so a naive identity
# mapping fails. canvas id -> internal index is a permutation with exactly one
# fixed point (7), which must NOT mask a swap. Internal pseudonyms are for
# canvas ids; the trap is that if we mistakenly keyed by list position,
# recipients would swap.
ROSTER = {
    3: "Алия Тестова",
    4: "Берик Примеров",
    5: "Гаухар Демова",
    7: "Даулет Образцов",
    10: "Ерке Синтетик",
    2: "Иван Преподавателев",
}
# The permutation used by the round-trip test (canvas_id -> WRONG-but-plausible id):
# We assert the EXACT canvas id leaves the proxy; a swapped mapping would send
# id 10's nudge to canvas id 3, etc.
PERMUTATION = {10: 3, 3: 10, 4: 5, 5: 4, 7: 7}  # 7 is the coincidental fixed point


def _p(uid: int) -> str:
    return compute_pseudonym(uid, KEY)


def _user_json(uid: int) -> dict:
    return {"id": uid, "name": ROSTER[uid], "sortable_name": ROSTER[uid],
            "login_id": f"u{uid}@canvas.test", "email": f"u{uid}@canvas.test",
            "sis_user_id": f"SIS-{uid}", "uuid": f"uuid-{uid}"}


# ---------------------------------------------------------------------------
# Round-trip recipient identity (§12.2) — the lesson-48 drill
# ---------------------------------------------------------------------------

@respx.mock
async def test_roundtrip_recipient_exact_id(client):
    """internal pseudonym -> proxy -> upstream recipient must be the EXACT id.
    Permutation with one fixed point (7) must not mask a swap."""
    sent: list[dict] = []
    route = respx.post(f"{UPSTREAM}/api/v1/conversations").mock(
        side_effect=lambda request: (
            sent.append(json.loads(request.content)),
            Response(200, json=[{"id": 900, "subject": "s", "workflow_state": "active"}]),
        )[1]
    )
    # map each real id in the vault, then send to each pseudonym
    for uid in PERMUTATION:
        await main._state.vault.map_subject(uid)
        respx.get(f"{UPSTREAM}/api/v1/users/{uid}").mock(return_value=Response(200, json=_user_json(uid)))

    for uid, wrong in PERMUTATION.items():
        sent.clear()
        resp = await client.post("/api/v1/conversations", json={
            "recipients": [_p(uid)],
            "body": "Привет, {{first_name}}!",
            "subject": "check",
        })
        assert resp.status_code == 200, resp.text
        assert sent[0]["recipients"] == [str(uid)], (  # self-hosted Canvas requires string ids
            f"SWAP BUG: nudge for internal {uid} would reach canvas {sent[0]['recipients']}"
        )
        assert sent[0]["recipients"] != [wrong] or uid == wrong
        # placeholder substituted with the RIGHT person's first name
        assert "{{first_name}}" not in sent[0]["body"]
        assert sent[0]["body"] == f"Привет, {ROSTER[uid].split(' ')[0]}!"
    assert route.called


# ---------------------------------------------------------------------------
# Determinism (§12.3)
# ---------------------------------------------------------------------------

@respx.mock
async def test_determinism_three_calls_and_restart(client, tmp_path, monkeypatch):
    respx.get(f"{UPSTREAM}/api/v1/courses/1/users").mock(
        return_value=Response(200, json=[_user_json(3), _user_json(4)])
    )
    ids = set()
    for _ in range(3):
        resp = await client.get("/api/v1/courses/1/users")
        assert resp.status_code == 200
        for u in resp.json():
            ids.add(u["id"])
    p_before = ids.copy()

    # simulated restart: new state on the same key + vault file
    await main._state.vault.close()
    await main._state.canvas.close()
    await main._init_state()
    resp = await client.get("/api/v1/courses/1/users")
    p_after = {u["id"] for u in resp.json()}
    assert p_after == p_before
    assert all(p.startswith("P-") for p in p_after)
    assert len(p_after) == 2


# ---------------------------------------------------------------------------
# Fail-closed drills (§12.5)
# ---------------------------------------------------------------------------

async def test_unknown_pid_in_path_404(client):
    resp = await client.get("/api/v1/users/P-ZZZZZZZZZZ/page_views")
    assert resp.status_code == 404
    assert resp.json()["errors"][0]["error_code"] == "unknown_subject"
    # audited
    tail = await main._state.vault.audit_tail()
    assert any(r["event"] == "dead_letter" for r in tail)


@respx.mock
async def test_unresolvable_recipient_dead_letter_no_send(client):
    sends = respx.post(f"{UPSTREAM}/api/v1/conversations").mock(
        return_value=Response(200, json=[{"id": 1}])
    )
    resp = await client.post("/api/v1/conversations", json={
        "recipients": ["P-ZZZZZZZZZZ"], "body": "hi"
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["error_code"] == "dead_letter"
    assert not sends.called  # NOTHING was sent — no fallback guess
    tail = await main._state.vault.audit_tail()
    assert any(r["event"] == "dead_letter" for r in tail)


@respx.mock
async def test_malformed_recipient_rejected(client):
    sends = respx.post(f"{UPSTREAM}/api/v1/conversations").mock(return_value=Response(200, json=[]))
    resp = await client.post("/api/v1/conversations", json={"recipients": ["P-BAD"], "body": "x"})
    assert resp.status_code == 400
    assert not sends.called


@respx.mock
async def test_resolve_without_instructor_role_403(client):
    # requester pseudonym (id 3 = student) not staff -> deny
    await main._state.vault.map_subject(3)
    respx.get(f"{UPSTREAM}/api/v1/courses/1/enrollments").mock(return_value=Response(200, json=[
        {"user_id": 3, "type": "StudentEnrollment"},
        {"user_id": 2, "type": "TeacherEnrollment"},
    ]))
    resp = await client.post("/proxy/v1/resolve", json={
        "context": "course:1", "pseudonyms": [_p(3)], "session_token": _p(3),
    })
    assert resp.status_code == 403
    tail = await main._state.vault.audit_tail()
    assert any(r["event"] == "resolve_denied" for r in tail)


@respx.mock
async def test_resolve_with_instructor_role(client):
    await main._state.vault.map_subject(2)
    await main._state.vault.map_subject(3)
    respx.get(f"{UPSTREAM}/api/v1/courses/1/enrollments").mock(return_value=Response(200, json=[
        {"user_id": 3, "type": "StudentEnrollment"},
        {"user_id": 2, "type": "TeacherEnrollment"},
    ]))
    respx.get(f"{UPSTREAM}/api/v1/users/3").mock(return_value=Response(200, json=_user_json(3)))
    resp = await client.post("/proxy/v1/resolve", json={
        "context": "course:1", "pseudonyms": [_p(3)], "session_token": _p(2),
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_map"][_p(3)]["display_name"] == ROSTER[3]
    assert body["ttl_s"] == 60
    # audited, and no name persisted: audit detail only carries pseudonyms
    tail = await main._state.vault.audit_tail()
    resolve_rows = [r for r in tail if r["event"] == "resolve"]
    assert resolve_rows and all(ROSTER[3] not in (r["detail_json"] or "") for r in resolve_rows)


@respx.mock
async def test_resolve_upstream_unverifiable_fails_closed(client):
    await main._state.vault.map_subject(2)
    respx.get(f"{UPSTREAM}/api/v1/courses/1/enrollments").mock(return_value=Response(500))
    resp = await client.post("/proxy/v1/resolve", json={
        "context": "course:1", "pseudonyms": [_p(3)], "session_token": _p(2),
    })
    assert resp.status_code == 403  # cannot verify -> deny


@respx.mock
async def test_resolve_rate_limited(client):
    await main._state.vault.map_subject(2)
    respx.get(f"{UPSTREAM}/api/v1/courses/1/enrollments").mock(return_value=Response(200, json=[
        {"user_id": 2, "type": "TeacherEnrollment"}
    ]))
    respx.get(f"{UPSTREAM}/api/v1/users/2").mock(return_value=Response(200, json=_user_json(2)))
    codes = []
    for _ in range(35):
        r = await client.post("/proxy/v1/resolve", json={
            "context": "course:1", "pseudonyms": [_p(2)], "session_token": _p(2),
        })
        codes.append(r.status_code)
    assert 429 in codes


# ---------------------------------------------------------------------------
# Auth drills (X-Proxy-Auth HMAC, mTLS header) — §8.2
# ---------------------------------------------------------------------------

async def test_hmac_bad_signature_401(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("AUTH_HMAC_SECRET", "shared-secret")
    get_settings.cache_clear()
    main._state.settings = get_settings()
    try:
        body = json.dumps({"recipients": ["P-ZZZZZZZZZZ"]}).encode()
        resp = await client.post("/api/v1/conversations", content=body,
                                 headers={"X-Proxy-Auth": "HMAC:123.deadbeef"})
        assert resp.status_code == 401
    finally:
        monkeypatch.delenv("AUTH_HMAC_SECRET")
        get_settings.cache_clear()


async def test_hmac_stale_timestamp_401(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("AUTH_HMAC_SECRET", "shared-secret")
    get_settings.cache_clear()
    try:
        body = b"{}"
        old_ts = int(time.time()) - 120  # outside ±60s window
        resp = await client.post("/api/v1/conversations", content=body,
                                 headers={"X-Proxy-Auth": _hmac_header("shared-secret", body, ts=old_ts)})
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["error_code"] == "stale_auth"
    finally:
        monkeypatch.delenv("AUTH_HMAC_SECRET")
        get_settings.cache_clear()


async def test_hmac_valid_accepted(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("AUTH_HMAC_SECRET", "shared-secret")
    get_settings.cache_clear()
    main._state.settings = get_settings()
    try:
        body = json.dumps({"recipients": ["P-ZZZZZZZZZZ"]}).encode()
        resp = await client.post("/api/v1/conversations", content=body,
                                 headers={"X-Proxy-Auth": _hmac_header("shared-secret", body)})
        # auth passes; unknown recipient dead-letters (not 401)
        assert resp.status_code == 422
    finally:
        monkeypatch.delenv("AUTH_HMAC_SECRET")
        get_settings.cache_clear()


async def test_mtls_missing_cert_401(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("MTLS_CLIENT_CA", "/etc/ssl/test-ca.pem")
    get_settings.cache_clear()
    try:
        resp = await client.get("/api/v1/courses/1/users")
        assert resp.status_code == 401
        assert resp.json()["errors"][0]["error_code"] == "mTLS_required"
    finally:
        monkeypatch.delenv("MTLS_CLIENT_CA")
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Placeholder leak + no-name-persistence (§12.7)
# ---------------------------------------------------------------------------

@respx.mock
async def test_no_placeholder_leak_and_no_name_persisted(client, caplog):
    await main._state.vault.map_subject(3)
    respx.get(f"{UPSTREAM}/api/v1/users/3").mock(return_value=Response(200, json=_user_json(3)))
    respx.post(f"{UPSTREAM}/api/v1/conversations").mock(return_value=Response(200, json=[
        {"id": 1, "subject": "s", "workflow_state": "active", "participants": [_user_json(3)]}
    ]))
    with caplog.at_level(logging.DEBUG, logger="canvas_privacy_proxy"):
        resp = await client.post("/api/v1/conversations", json={
            "recipients": [_p(3)], "body": "Привет, {{first_name}}! Ты отстаёшь на 2 недели.",
        })
    assert resp.status_code == 200
    # no placeholder in what the proxy RETURNED or in any captured log line
    assert "{{first_name}}" not in resp.text
    for rec in caplog.records:
        assert "{{" not in rec.getMessage()
        assert "Алия" not in rec.getMessage()
    # vault contents contain no names (audit + subject_map)
    import sqlite3

    db = sqlite3.connect(main._state.vault.engine.url.database)
    dump = "\n".join(str(r) for r in db.execute(
        "SELECT pseudonym, canvas_user_id, created_at, last_seen_at FROM subject_map").fetchall())
    dump += "\n".join(str(r) for r in db.execute(
        "SELECT id, ts, event, pseudonym, detail_json FROM audit_log").fetchall())
    db.close()
    for name in ROSTER.values():
        assert name not in dump, "REAL NAME PERSISTED IN VAULT"
    for uid in ROSTER:
        assert f"u{uid}@canvas.test" not in dump


@respx.mock
async def test_nameless_mode_strips_placeholders(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("NAMELESS_MODE", "1")
    get_settings.cache_clear()
    sent: list[dict] = []
    respx.post(f"{UPSTREAM}/api/v1/conversations").mock(
        side_effect=lambda request: (sent.append(json.loads(request.content)),
                                     Response(200, json=[{"id": 1}]))[1]
    )
    await main._state.vault.map_subject(3)
    try:
        resp = await client.post("/api/v1/conversations", json={
            "recipients": [_p(3)], "body": "Привет, {{first_name}}! Скорее.",
        })
        assert resp.status_code == 200
        assert sent[0]["body"] == "Привет, ! Скорее."
        assert "{{" not in sent[0]["body"]
    finally:
        monkeypatch.delenv("NAMELESS_MODE")
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Passthrough behavior
# ---------------------------------------------------------------------------

@respx.mock
async def test_passthrough_headers_preserved(client):
    await main._state.vault.map_subject(3)
    respx.get(f"{UPSTREAM}/api/v1/courses/1/users").mock(return_value=Response(
        200,
        json=[_user_json(3)],
        headers={
            "Link": '<http://canvas.test/api/v1/courses/1/users?page=2>; rel="next"',
            "X-Rate-Limit-Remaining": "693",
            "X-Rate-Limit-Limit": "700",
        },
    ))
    resp = await client.get("/api/v1/courses/1/users")
    assert resp.status_code == 200
    # Fix 2 (privacy audit): Link headers are STRIPPED, never passed through —
    # upstream bookmark cursors base64-embed real names + real Canvas ids.
    # The app paginates with ?page=N params, so nothing downstream needs Link.
    assert "link" not in {k.lower() for k in resp.headers.keys()}
    assert resp.headers.get("x-rate-limit-remaining") == "693"
    u = resp.json()[0]
    assert u["id"] == _p(3)
    for forbidden in ("name", "email", "login_id", "sis_user_id", "uuid"):
        assert forbidden not in u


@respx.mock
async def test_upstream_error_surfaced_honestly(client):
    respx.get(f"{UPSTREAM}/api/v1/courses/9/users").mock(return_value=Response(401, json={"errors": []}))
    resp = await client.get("/api/v1/courses/9/users")
    # upstream 401 forwarded, not faked — our proxy adds its token so 401 = real config error
    assert resp.status_code in (401, 502)


@respx.mock
async def test_sync_roster_bulk(client):
    respx.get(f"{UPSTREAM}/api/v1/courses/1/enrollments").mock(return_value=Response(200, json=[
        {"user_id": 3, "type": "StudentEnrollment"},
        {"user_id": 4, "type": "StudentEnrollment"},
    ]))
    resp = await client.post("/proxy/v1/sync-roster", json={"course_id": 1})
    assert resp.status_code == 200
    assert resp.json()["mapped"] == 2
    assert await main._state.vault.entry_count() == 2
