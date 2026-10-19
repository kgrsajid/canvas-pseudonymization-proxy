"""Transparent Canvas passthrough: GET/POST /api/v1/** (design §5.1)."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..auth import AuthError, verify_request
from ..canvas_client import UpstreamError
from ..config import get_settings
from ..main import get_app_state
from ..transforms.reads import pseudonymize_body
from ..transforms.requests import (
    MalformedSubject,
    UnknownSubject,
    rewrite_body,
    rewrite_path,
    rewrite_query,
)

router = APIRouter()

# Headers passed through untouched (rate limiting only).
#
# "link" is deliberately NOT passed through: Canvas pagination Link headers
# embed base64 bookmark cursors like ["StudentEnrollment","Тестова, Алия",10]
# — a REAL NAME plus REAL user id leak that we cannot rewrite safely. The app
# paginates with ?page=N params (Canvas supports both), so stripping breaks
# nothing downstream.
_PASSTHROUGH_HEADERS = {
    "x-rate-limit-remaining", "x-rate-limit-limit", "x-request-cost",
    "x-request-quota-remaining", "x-request-quota-max", "x-request-quota-requests-slot-count",
}
# Body keys whose values are P-id arrays on POST (Canvas array params arrive
# either as query params — handled in rewrite_query — or JSON lists).
_BODY_P_ID_KEYS = frozenset({"recipients", "user_ids", "user_id", "recipient"})
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": [{"message": message, "error_code": code}]})


@router.api_route("/api/v1/{rest_of_path:path}", methods=_METHODS)
async def passthrough(rest_of_path: str, request: Request) -> Response:
    settings = get_settings()
    state = get_app_state()
    body = await request.body()

    try:
        verify_request(request, settings, body)
    except AuthError as e:
        return _error(e.status, e.code, str(e))

    path = f"/api/v1/{rest_of_path}"

    # Rewrite P-xxxx -> real ids (path, query, body). Fail closed on unknown/malformed.
    try:
        real_path = await rewrite_path(path, state.vault)
        real_query = await rewrite_query(_multi_to_dict(request), state.vault)
        json_body = None
        if body:
            import json as _json

            try:
                json_body = _json.loads(body)
            except _json.JSONDecodeError:
                return _error(400, "bad_request", "request body must be JSON")
            json_body = await rewrite_body(json_body, state.vault, resolve_keys=_BODY_P_ID_KEYS)
    except UnknownSubject as e:
        await state.vault.audit("dead_letter", e.pseudonym, {"reason": "unknown_subject", "path": path})
        state.stats.count("unknown_subject")
        return _error(404, "unknown_subject", f"no subject mapped for {e.pseudonym}")
    except MalformedSubject as e:
        state.stats.count("malformed_subject")
        return _error(400, "malformed_subject", str(e))

    # Forward upstream.
    try:
        upstream_resp = await state.canvas.request(
            request.method,
            real_path,
            params=real_query or None,
            json_body=json_body,
        )
    except UpstreamError as e:
        state.stats.count("upstream_error")
        return _error(502, "upstream_error", str(e))

    # Known limitation: NON-JSON upstream responses (CSV/gradebook exports,
    # file downloads) are NOT passed through. They would leak raw names/ids —
    # we cannot pseudonymize CSV/binary safely. Fail closed with 501.
    upstream_ct = upstream_resp.headers.get("content-type", "")
    if "application/json" not in upstream_ct:
        state.stats.count("non_json_rejected")
        return _error(
            501,
            "non_json_unsupported",
            "Non-JSON responses not supported by proxy (PII risk)",
        )

    # Pseudonymize response body when JSON.
    try:
        data = upstream_resp.json()
    except ValueError:
        # Upstream lied about Content-Type (declared JSON, body is not).
        # Same PII risk as raw passthrough -> fail closed.
        state.stats.count("non_json_rejected")
        return _error(
            501,
            "non_json_unsupported",
            "Non-JSON responses not supported by proxy (PII risk)",
        )
    data = await pseudonymize_body(data, state.vault)
    import json as _json

    content = _json.dumps(data, ensure_ascii=False).encode("utf-8")
    media_type = "application/json"

    headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() in _PASSTHROUGH_HEADERS}
    state.stats.count("requests")
    state.stats.count("pseudonymized")
    return Response(content=content, status_code=upstream_resp.status_code, media_type=media_type, headers=headers)


def _multi_to_dict(request: Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        out.setdefault(key, []).append(value)
    return out
