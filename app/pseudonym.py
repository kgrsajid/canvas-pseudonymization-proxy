"""Pseudonym computation per design §4.1.

    P-xxxx = "P-" + base32(HMAC-SHA256(HMAC_KEY, "canvas-user:" + str(id)))[:10]

Deterministic, non-reversible without the key, namespaced input.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

NAMESPACE = "canvas-user:"
PSEUDONYM_RE = re.compile(r"^P-[A-Z2-7]{10}$")


class PseudonymError(ValueError):
    """Malformed pseudonym or bad key — always an error, never a guess."""


def _load_key(hmac_key_b64_or_raw: str) -> bytes:
    """Key material handling — explicit, never guessed:

    - `b64:<base64>` prefix  -> base64-decoded (≥32 decoded bytes required)
    - otherwise              -> raw bytes of the string (≥32 bytes required)

    The documented workflow (`openssl rand -base64 48`) writes base64 with
    padding/newlines that is NOT a safe raw key, so INSTALL.md instructs
    `HMAC_KEY=b64:$(openssl rand -base64 48)`.
    """
    if not hmac_key_b64_or_raw:
        raise PseudonymError("HMAC_KEY not configured")
    if hmac_key_b64_or_raw.startswith("b64:"):
        import binascii

        try:
            key = base64.b64decode(hmac_key_b64_or_raw[4:], validate=True)
        except (ValueError, binascii.Error) as e:
            raise PseudonymError(f"HMAC_KEY base64 decode failed: {e}")
        if len(key) < 32:
            raise PseudonymError("HMAC_KEY must decode to >= 32 bytes")
        return key
    key = hmac_key_b64_or_raw.encode("utf-8")
    if len(key) < 32:
        raise PseudonymError("HMAC_KEY must be >= 32 bytes (raw, or use b64: prefix)")
    return key


def key_fingerprint(hmac_key: str) -> str:
    """SHA256(HMAC_KEY)[:8] — lets ops verify which key is live without exposing it."""
    return hashlib.sha256(hmac_key.encode("utf-8")).hexdigest()[:8]


def compute_pseudonym(canvas_user_id: int, hmac_key: str) -> str:
    """Forward mapping id -> P-xxxx. Deterministic for the same key."""
    if not isinstance(canvas_user_id, int) or isinstance(canvas_user_id, bool):
        raise PseudonymError(f"canvas_user_id must be int, got {type(canvas_user_id).__name__}")
    if canvas_user_id <= 0:
        raise PseudonymError(f"canvas_user_id must be positive, got {canvas_user_id}")
    key = _load_key(hmac_key)
    msg = (NAMESPACE + str(canvas_user_id)).encode("utf-8")
    digest = hmac.new(key, msg, hashlib.sha256).digest()
    b32 = base64.b32encode(digest).decode("ascii")  # RFC 4648 alphabet A-Z2-7
    return "P-" + b32[:10]


def is_valid_pseudonym(value: str) -> bool:
    return isinstance(value, str) and bool(PSEUDONYM_RE.match(value))
