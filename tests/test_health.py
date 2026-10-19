"""Health & stats endpoints + app-level guarantees."""

import respx
from httpx import ASGITransport, AsyncClient, Response

import app.main as main


async def test_health_and_stats(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSTREAM_URL", "http://canvas.test")
    monkeypatch.setenv("CANVAS_API_TOKEN", "t")
    monkeypatch.setenv("HMAC_KEY", "h" * 48)
    monkeypatch.setenv("VAULT_SQLITE", str(tmp_path / "v.db"))
    monkeypatch.setenv("DEV_ALLOW_NO_AUTH", "true")  # no auth layer in this test
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MTLS_CLIENT_CA", raising=False)
    monkeypatch.delenv("AUTH_HMAC_SECRET", raising=False)
    main.get_settings.cache_clear()
    await main._init_state()
    try:
        with respx.mock:
            respx.get("http://canvas.test/api/v1/users/self").mock(
                return_value=Response(200, json={"id": 1})
            )
            transport = ASGITransport(app=main.app)
            async with AsyncClient(transport=transport, base_url="http://t") as c:
                h = (await c.get("/proxy/v1/health")).json()
                assert h["status"] == "ok"
                assert h["upstream_ok"] is True
                assert h["backend"] == "sqlite"
                assert len(h["key_fingerprint"]) == 8
                s = (await c.get("/proxy/v1/stats")).json()
                assert "counters" in s
    finally:
        await main._state.vault.close()
        await main._state.canvas.close()
        main._state = None
        main.get_settings.cache_clear()


async def test_app_requires_hmac_key(monkeypatch, tmp_path):
    monkeypatch.delenv("HMAC_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import os

    os.environ.pop("HMAC_KEY", None)
    main.get_settings.cache_clear()
    try:
        await main._init_state()
        assert False, "must fail closed without HMAC_KEY"
    except RuntimeError:
        pass
    finally:
        main.get_settings.cache_clear()


async def test_app_refuses_to_start_without_auth_layer(monkeypatch, tmp_path):
    """Fail-closed startup: no mTLS and no HMAC auth configured -> refuse to
    start, unless the DEV_ALLOW_NO_AUTH escape hatch is explicitly set."""
    import os

    for var in ("HMAC_KEY", "MTLS_CLIENT_CA", "AUTH_HMAC_SECRET", "DATABASE_URL",
                "DEV_ALLOW_NO_AUTH"):
        os.environ.pop(var, None)
    monkeypatch.delenv("HMAC_KEY", raising=False)
    monkeypatch.delenv("MTLS_CLIENT_CA", raising=False)
    monkeypatch.delenv("AUTH_HMAC_SECRET", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DEV_ALLOW_NO_AUTH", raising=False)
    os.environ["HMAC_KEY"] = "h" * 48
    os.environ["VAULT_SQLITE"] = str(tmp_path / "v.db")
    main.get_settings.cache_clear()
    try:
        await main._init_state()
        assert False, "must refuse to start with no auth layer configured"
    except RuntimeError as e:
        assert "refusing to start" in str(e)
    finally:
        main._state = None
        main.get_settings.cache_clear()

    # With the explicit dev escape hatch, startup proceeds.
    monkeypatch.setenv("DEV_ALLOW_NO_AUTH", "true")
    main.get_settings.cache_clear()
    try:
        await main._init_state()
        assert main._state is not None
    finally:
        await main._state.vault.close()
        await main._state.canvas.close()
        main._state = None
        main.get_settings.cache_clear()


async def test_app_starts_with_hmac_auth_configured(monkeypatch, tmp_path):
    """An auth layer alone (HMAC, no mTLS) satisfies the fail-closed check."""
    import os

    for var in ("MTLS_CLIENT_CA", "AUTH_HMAC_SECRET", "DATABASE_URL",
                "DEV_ALLOW_NO_AUTH"):
        os.environ.pop(var, None)
    os.environ["HMAC_KEY"] = "h" * 48
    os.environ["VAULT_SQLITE"] = str(tmp_path / "v.db")
    os.environ["AUTH_HMAC_SECRET"] = "s" * 48
    main.get_settings.cache_clear()
    try:
        await main._init_state()
        assert main._state is not None
    finally:
        await main._state.vault.close()
        await main._state.canvas.close()
        main._state = None
        main.get_settings.cache_clear()
