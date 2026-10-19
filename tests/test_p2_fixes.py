"""P2 remediation regression tests (privacy audit fixes 2, 6, 7 + /proxy/v1/map).

- Link headers from upstream are STRIPPED (they embed base64 bookmark cursors
  containing real names + real Canvas ids — unrewritable, and the app uses
  ?page=N pagination, so nothing downstream needs them).
- html_url rewriting covers /grades/<id> and /submissions/<id>, not just
  /users/<id>.
- POST /proxy/v1/map resolves a real Canvas user id to its pseudonym
  (deterministic, lazily created, authenticated, audited).
- Startup is fail-closed: no auth layer configured -> refuse to start
  (covered in test_health.py).
"""

from __future__ import annotations

import base64
import hashlib
import hmac as hmac_lib
import json
import time

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

import app.main as main
from app.pseudonym import compute_pseudonym, is_valid_pseudonym

UPSTREAM = "http://canvas.test"
KEY = "m" * 48  # test HMAC key (settings-level; distinct from request-auth secret)


def _request_hmac_header(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac_lib.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"HMAC:{ts}.{mac}"


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
    monkeypatch.setenv("DEV_ALLOW_NO_AUTH", "true")  # auth tested separately
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


# ---------------------------------------------------------------------------
# Fix 2 — Link headers stripped
# ---------------------------------------------------------------------------

@respx.mock
async def test_link_header_stripped_from_passthrough(client):
    """Upstream Link header leaks base64 bookmark cursors with REAL names and
    REAL ids (e.g. [\"StudentEnrollment\",\"Демова, Образец\",10]) — it must
    never reach the caller."""
    # Build the exact cursor shape Canvas sends: ["StudentEnrollment","<name>",10]
    cursor = base64.b64encode(
        json.dumps(["StudentEnrollment", "Демова, Образец", 10]).encode("utf-8")
    ).decode("ascii")
    link_value = (
        f'<{UPSTREAM}/api/v1/courses/1/enrollments?page=first&per_page=2>; rel="current",'
        f'<{UPSTREAM}/api/v1/courses/1/enrollments?page=bookmark:{cursor}&per_page=2>; rel="next"'
    )
    respx.get(f"{UPSTREAM}/api/v1/courses/1/enrollments").mock(
        return_value=Response(
            200,
            json=[{"id": 101, "user_id": 9, "type": "StudentEnrollment",
                   "html_url": f"{UPSTREAM}/courses/1/users/9"}],
            headers={"Link": link_value, "X-Rate-Limit-Remaining": "599"},
        )
    )
    resp = await client.get("/api/v1/courses/1/enrollments?per_page=2")
    assert resp.status_code == 200
    assert "link" not in {k.lower() for k in resp.headers.keys()}, (
        "Link header leaked upstream pagination cursor (real name + real id)"
    )
    # rate-limit headers still pass through
    assert resp.headers.get("x-rate-limit-remaining") == "599"
    # body pseudonymized as usual
    assert is_valid_pseudonym(resp.json()[0]["user_id"])
    # and the link cursor's real name/id appear NOWHERE in the response
    raw = resp.text
    assert "Демова" not in raw and "bookmark:" not in raw


# ---------------------------------------------------------------------------
# Fix 7 — grades/<id> and submissions/<id> URL rewriting
# ---------------------------------------------------------------------------

async def test_grades_and_submissions_html_url_pseudonymized():
    from app.transforms.reads import pseudonymize_body

    class V:
        async def map_subject(self, u):
            return compute_pseudonym(u, KEY)

    payload = [
        # enrollment with grades include — the audited leak
        {"id": 201, "user_id": 9, "type": "StudentEnrollment",
         "grades": {"html_url": f"{UPSTREAM}/courses/1/grades/9",
                    "current_score": 71.5},
         "html_url": f"{UPSTREAM}/courses/1/users/9"},
        # submission object
        {"id": 301, "assignment_id": 55, "user_id": 10, "score": 80,
         "html_url": f"{UPSTREAM}/courses/1/assignments/55/submissions/10"},
        # non-user urls untouched
        {"course": {"id": 1, "html_url": f"{UPSTREAM}/courses/1"}},
        {"file": {"id": 77, "url": f"{UPSTREAM}/courses/1/files/77"}},
        # already-pseudonymized url untouched (idempotent)
        {"html_url": f"{UPSTREAM}/courses/1/grades/{compute_pseudonym(9, KEY)}"},
    ]
    out = await pseudonymize_body(payload, V())
    g = out[0]["grades"]["html_url"]
    assert g == f"{UPSTREAM}/courses/1/grades/{compute_pseudonym(9, KEY)}"
    assert "/grades/9" not in g
    assert out[0]["html_url"].endswith(f"/users/{compute_pseudonym(9, KEY)}")
    s = out[1]["html_url"]
    assert s == f"{UPSTREAM}/courses/1/assignments/55/submissions/{compute_pseudonym(10, KEY)}"
    assert "/submissions/10" not in s
    assert out[2]["course"]["html_url"] == f"{UPSTREAM}/courses/1"
    assert out[3]["file"]["url"] == f"{UPSTREAM}/courses/1/files/77"
    assert out[4]["html_url"] == f"{UPSTREAM}/courses/1/grades/{compute_pseudonym(9, KEY)}"
    # behavioral data untouched
    assert out[0]["grades"]["current_score"] == 71.5
    assert out[1]["score"] == 80


# ---------------------------------------------------------------------------
# Fix 5 (proxy side) — POST /proxy/v1/map
# ---------------------------------------------------------------------------

@respx.mock
async def test_map_endpoint_resolves_and_creates(client):
    body = json.dumps({"canvas_user_id": 9}).encode("utf-8")
    resp = await client.post(
        "/proxy/v1/map",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Proxy-Auth": _request_hmac_header("req-secret", body),
        },
    )
    # No request-auth secret configured in this fixture -> the verify layer
    # is a no-op (DEV_ALLOW_NO_AUTH); the route itself must still work.
    assert resp.status_code == 200, resp.text
    pseudo = resp.json()["pseudonym"]
    assert pseudo == compute_pseudonym(9, KEY)
    assert is_valid_pseudonym(pseudo)

    # deterministic + idempotent: same id -> same pseudonym, also matches the
    # one every other endpoint returns for user 9
    resp2 = await client.post("/proxy/v1/map", json={"canvas_user_id": 9})
    assert resp2.json()["pseudonym"] == pseudo
    assert await main._state.vault.reverse_lookup(pseudo) == 9

    # distinct id -> distinct pseudonym
    resp3 = await client.post("/proxy/v1/map", json={"canvas_user_id": 10})
    assert resp3.json()["pseudonym"] != pseudo

    # audited
    tail = await main._state.vault.audit_tail()
    assert any(r["event"] == "map" and r["pseudonym"] == pseudo for r in tail)


async def test_map_endpoint_validates_input(client):
    resp = await client.post("/proxy/v1/map", json={"canvas_user_id": -5})
    assert resp.status_code == 422
    resp = await client.post("/proxy/v1/map", json={"canvas_user_id": "nine"})
    assert resp.status_code == 422


@respx.mock
async def test_map_endpoint_requires_auth_when_configured(client, monkeypatch):
    """With an HMAC auth layer configured, /proxy/v1/map rejects bad signatures."""
    secret = "req-secret-" + "x" * 32
    monkeypatch.setenv("AUTH_HMAC_SECRET", secret)
    monkeypatch.delenv("DEV_ALLOW_NO_AUTH", raising=False)
    main.get_settings.cache_clear()

    body = json.dumps({"canvas_user_id": 9}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    ts = int(time.time())
    good = _request_hmac_header(secret, body, ts)
    bad = _request_hmac_header("wrong-secret", body, ts)

    assert (await client.post("/proxy/v1/map", content=body,
                              headers={**headers, "X-Proxy-Auth": bad})).status_code == 401
    assert (await client.post("/proxy/v1/map", content=body,
                              headers={**headers, "X-Proxy-Auth": good})).status_code == 200
    # no auth header at all -> rejected
    assert (await client.post("/proxy/v1/map", content=body, headers=headers)).status_code == 401
