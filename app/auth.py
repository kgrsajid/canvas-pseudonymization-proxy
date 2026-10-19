"""Layered app->proxy auth (design §8.2): mTLS client certs (primary) +
optional X-Proxy-Auth HMAC signing (±60 s replay window). Fail closed when
configured. IP allowlist is an optional additional layer.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Request

from .config import Settings


class AuthError(Exception):
    status = 401

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _client_ip(request: Request) -> str:
    # Trust only direct socket peer; proxies in front must handle XFF themselves.
    if request.client:
        return request.client.host
    return ""


def verify_ip_allowlist(request: Request, settings: Settings) -> None:
    if not settings.allowed_ips:
        return
    allowed = {a.strip() for a in settings.allowed_ips.split(",") if a.strip()}
    ip = _client_ip(request)
    if ip not in allowed:
        raise AuthError("forbidden_ip", "client address not in allowlist")


def verify_mtls(request: Request, settings: Settings) -> None:
    """Verify the TLS client certificate presented to THIS proxy.

    Requires the TLS terminator (nginx/dokku custom config) to set
    X-SSL-Client-Verify=SUCCESS and (optionally) X-SSL-Client-S-DN, and to
    REJECT the handshake itself when no cert is presented (ssl_verify_client
    on). We fail closed on anything other than an explicit SUCCESS.
    """
    if not settings.mtls_client_ca:
        return  # mTLS not configured — HMAC layer (if any) still applies
    verify = request.headers.get("x-ssl-client-verify", "")
    if verify.upper() != "SUCCESS":
        raise AuthError("mTLS_required", "valid client certificate required")
    if settings.allowed_peer:
        dn = request.headers.get("x-ssl-client-s-dn", "")
        if settings.allowed_peer not in dn:
            raise AuthError("peer_rejected", "client certificate CN not allowed")


def verify_proxy_auth_hmac(request: Request, settings: Settings, body: bytes) -> None:
    """X-Proxy-Auth: HMAC-SHA256(secret, timestamp + "." + body) — ±60 s window."""
    if not settings.auth_hmac_secret:
        return
    header = request.headers.get("x-proxy-auth", "")
    parts = header.split(":", 1)
    if len(parts) != 2 or parts[0] != "HMAC" or len(parts[1].split(".")) != 2:
        raise AuthError("bad_auth_header", "X-Proxy-Auth must be 'HMAC:<ts>.<hexmac>'")
    ts_hex, mac_hex = parts[1].split(".")
    try:
        ts = int(ts_hex)
        mac = bytes.fromhex(mac_hex)
    except ValueError:
        raise AuthError("bad_auth_header", "malformed timestamp or MAC")
    now = int(time.time())
    if abs(now - ts) > settings.auth_replay_window_s:
        raise AuthError("stale_auth", f"timestamp outside ±{settings.auth_replay_window_s}s replay window")
    expected = hmac.new(
        settings.auth_hmac_secret.encode("utf-8"),
        str(ts).encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(mac, expected):
        raise AuthError("bad_signature", "HMAC signature mismatch")


def verify_request(request: Request, settings: Settings, body: bytes) -> None:
    """Full layered check — call at the top of every route."""
    verify_ip_allowlist(request, settings)
    verify_mtls(request, settings)
    verify_proxy_auth_hmac(request, settings, body)
