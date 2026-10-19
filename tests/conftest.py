"""Shared test fixtures: SQLite-backed settings, vault, and app state."""

from __future__ import annotations

import base64
import os
import secrets

import pytest

# Deterministic-per-run test key (base64 of 48 random bytes, b64: workflow)
TEST_HMAC_KEY = "b64:" + base64.b64encode(secrets.token_bytes(48)).decode()


@pytest.fixture
def settings_sqlite(tmp_path):
    import app.config as cfg

    for var in ("DATABASE_URL", "VAULT_SQLITE", "UPSTREAM_URL", "CANVAS_API_TOKEN",
                "HMAC_KEY", "NAMELESS_MODE", "MTLS_CLIENT_CA", "AUTH_HMAC_SECRET",
                "ALLOWED_IPS", "ALLOWED_PEER", "DEV_ALLOW_NO_AUTH"):
        os.environ.pop(var, None)
    os.environ["VAULT_SQLITE"] = str(tmp_path / "vault.db")
    os.environ["UPSTREAM_URL"] = "http://canvas.test"
    os.environ["CANVAS_API_TOKEN"] = "test-token"
    os.environ["HMAC_KEY"] = TEST_HMAC_KEY
    # Tests exercise transform logic without standing up an auth layer;
    # production startup refuses to run unauthenticated without this flag.
    os.environ["DEV_ALLOW_NO_AUTH"] = "true"
    cfg.get_settings.cache_clear()
    yield cfg.get_settings()
    cfg.get_settings.cache_clear()


@pytest.fixture
async def vault_sqlite(settings_sqlite):
    from app.vault import build_vault

    v = build_vault(settings_sqlite)
    await v.init_schema()
    yield v
    await v.close()


@pytest.fixture
async def app_state(vault_sqlite, settings_sqlite):
    from app.canvas_client import CanvasClient
    from app.main import AppState
    import app.main as main

    st = AppState(vault=vault_sqlite, canvas=CanvasClient(settings_sqlite))
    main._state = st
    yield st
    main._state = None
    await st.canvas.close()
