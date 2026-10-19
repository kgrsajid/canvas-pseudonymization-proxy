"""Proxy configuration — every knob env-driven, design-doc values as defaults."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Upstream Canvas instance
    upstream_url: str = "http://localhost:3000"
    canvas_api_token: str = ""  # required for any upstream call; empty = fail closed

    # Pseudonymization key (base64 or raw). Required.
    hmac_key: str = ""

    # Vault backend: DATABASE_URL (Postgres, production default) OR VAULT_SQLITE (dev).
    database_url: str = ""  # postgresql+asyncpg://...
    vault_sqlite: str = ""  # e.g. ./data/vault.db

    # Security
    mtls_client_ca: str = ""  # path to CA bundle; if set, client-cert verification is REQUIRED
    allowed_peer: str = ""  # optional single allowed client-cert CN (empty = any cert signed by CA)
    auth_hmac_secret: str = ""  # if set, X-Proxy-Auth HMAC signing is required
    auth_replay_window_s: int = 60  # ±60 s per design §8.2

    # Behaviour
    nameless_mode: bool = False  # strip {{placeholders}} instead of live name substitution

    # Rate limits
    resolve_rate_limit_per_min: int = 30  # design §5.3
    resolve_display_ttl_s: int = 60  # design §5.3 — display map TTL (app-side semantics; documented)
    upstream_rate_limit_per_min: int = 300
    upstream_timeout_s: float = 30.0
    upstream_connect_timeout_s: float = 10.0
    upstream_max_retries: int = 2

    # IP allowlist (comma-separated, optional)
    allowed_ips: str = ""

    # NEVER set in production. When true, the proxy may start with no mTLS
    # and no HMAC auth configured (local selftest/dev only). Enforced in
    # main._init_state — the proxy refuses to start unauthenticated without it.
    dev_allow_no_auth: bool = False

    @property
    def auth_required(self) -> bool:
        return bool(self.mtls_client_ca or self.auth_hmac_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
