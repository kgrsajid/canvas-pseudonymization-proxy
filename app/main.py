"""FastAPI app factory, health/stats endpoints, shared state container."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .canvas_client import CanvasClient, UpstreamError
from .config import get_settings
from .pseudonym import key_fingerprint
from .vault import Vault, build_vault

log = logging.getLogger("canvas_privacy_proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# NEVER log names/emails/real ids — route code only ever logs pseudonyms/errors.


@dataclass
class Stats:
    counters: dict[str, int] = field(default_factory=dict)

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n


@dataclass
class AppState:
    vault: Vault
    canvas: CanvasClient
    stats: Stats = field(default_factory=Stats)


_state: AppState | None = None


def get_app_state() -> AppState:
    assert _state is not None, "app state not initialized"
    return _state


async def _init_state() -> None:
    global _state
    settings = get_settings()
    if not settings.hmac_key:
        raise RuntimeError("HMAC_KEY must be configured (fail closed)")
    # Fail-closed startup: serving Canvas data with no client authentication
    # at all is a total privacy breach. Require mTLS or HMAC auth unless the
    # explicit dev escape hatch is set (local selftest only).
    if not settings.auth_required and not settings.dev_allow_no_auth:
        raise RuntimeError(
            "refusing to start: neither MTLS_CLIENT_CA nor AUTH_HMAC_SECRET is "
            "configured — the proxy would serve pseudonymized Canvas data to "
            "anyone. Configure an auth layer, or set DEV_ALLOW_NO_AUTH=true "
            "for local development/selftest only."
        )
    vault = build_vault(settings)
    await vault.init_schema()
    canvas = CanvasClient(settings)
    _state = AppState(vault=vault, canvas=canvas)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _init_state()
    yield
    if _state is not None:
        await _state.vault.close()
        await _state.canvas.close()


def create_app() -> FastAPI:
    app = FastAPI(title="canvas-privacy-proxy", version="0.1.0",
                  docs_url=None, redoc_url=None, lifespan=lifespan)

    from .routes import admin, conversations, map, passthrough, resolve

    # Order matters: specific routes MUST register before the /api/v1/** catch-all.
    app.include_router(conversations.router)
    app.include_router(resolve.router)
    app.include_router(admin.router)
    app.include_router(map.router)
    app.include_router(passthrough.router)

    @app.get("/proxy/v1/health")
    async def health() -> JSONResponse:
        settings = get_settings()
        state = get_app_state()
        upstream_ok: bool | None = None
        try:
            resp = await state.canvas.request("GET", "/api/v1/users/self")
            upstream_ok = resp.status_code < 500
        except UpstreamError:
            upstream_ok = False
        return JSONResponse(
            content={
                "status": "ok" if upstream_ok is not False else "degraded",
                "upstream_ok": upstream_ok,
                "vault_entries": await state.vault.entry_count(),
                "key_fingerprint": key_fingerprint(settings.hmac_key),
                "backend": state.vault._kind,
                "nameless_mode": settings.nameless_mode,
            }
        )

    @app.get("/proxy/v1/stats")
    async def stats() -> JSONResponse:
        state = get_app_state()
        return JSONResponse(content={"counters": state.stats.counters})

    return app


app = create_app()
