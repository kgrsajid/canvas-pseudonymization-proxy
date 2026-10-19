"""POST /api/v1/conversations — pseudonym-respecting send (design §5.2).

Recipients arrive as P-xxxx; proxy resolves via vault (fail closed ->
dead-letter, never a fallback), fetches recipient names LIVE from Canvas,
substitutes {{first_name}}/{{full_name}}, forwards, re-pseudonymizes response.
NAMELESS_MODE strips placeholders instead. Names exist only transiently in
memory — never persisted, never logged.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth import AuthError, verify_request
from ..canvas_client import UpstreamError
from ..config import get_settings
from ..main import get_app_state
from ..pseudonym import is_valid_pseudonym
from ..transforms.reads import pseudonymize_body
from ..transforms.requests import UnknownSubject, _resolve_value

router = APIRouter()

_FIRST_NAME_RE = re.compile(r"\{\{first_name\}\}")
_FULL_NAME_RE = re.compile(r"\{\{full_name\}\}")
_ALL_PLACEHOLDER_RE = re.compile(r"\{\{(?:first_name|full_name)\}\}")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": [{"message": message, "error_code": code}]})


async def _resolve_recipients(payload: dict, state) -> tuple[list[str], list[int]] | None:
    """Resolve recipients via vault. Returns None after dead-lettering on failure."""
    pseudonyms: list[str] = []
    try:
        raw = payload["recipients"]
        recipients = raw if isinstance(raw, list) else [raw]
        for p in recipients:
            if not (isinstance(p, str) and is_valid_pseudonym(p)):
                return _bad(state, 400, "malformed_subject", f"recipient {p!r} is not a valid pseudonym", pseudonyms)
            pseudonyms.append(p)
        real_ids = [await _resolve_value(p, state.vault) for p in pseudonyms]
        return pseudonyms, real_ids
    except UnknownSubject as e:
        await state.vault.audit("dead_letter", e.pseudonym, {"reason": "unresolvable_recipient"})
        state.stats.count("dead_lettered")
        raise


def _bad(state, status: int, code: str, message: str, pseudonyms: list[str]) -> None:
    state.stats.count(code)
    raise _Resp(status, code, message)


class _Resp(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


@router.post("/api/v1/conversations")
async def create_conversation(request: Request) -> JSONResponse:
    settings = get_settings()
    state = get_app_state()
    body_bytes = await request.body()

    try:
        verify_request(request, settings, body_bytes)
    except AuthError as e:
        return _error(e.status, e.code, str(e))

    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        return _error(400, "bad_request", "request body must be JSON")
    if not isinstance(payload, dict) or "recipients" not in payload:
        return _error(400, "bad_request", "'recipients' required")

    # 1. Resolve recipients via vault — dead-letter on any failure, NO sends.
    try:
        resolved = await _resolve_recipients(payload, state)
        if resolved is None:  # pragma: no cover — defensive
            return _error(400, "malformed_subject", "invalid recipients")
        pseudonyms, real_ids = resolved
    except UnknownSubject as e:
        return _error(422, "dead_letter", f"recipient {e.pseudonym} cannot be resolved; message NOT sent")
    except _Resp as e:
        return _error(e.status, e.code, e.message)

    # 2. Fetch names live (transient, in-memory only) unless NAMELESS_MODE.
    first_names: dict[int, str] = {}
    full_names: dict[int, str] = {}
    body_text = payload.get("body", "") or ""
    subject_text = payload.get("subject", "") or ""
    has_placeholder = bool(_ALL_PLACEHOLDER_RE.search(body_text + subject_text))

    if has_placeholder and not settings.nameless_mode:
        for pid, rid in zip(pseudonyms, real_ids):
            try:
                user = await state.canvas.get_user(rid)
            except UpstreamError:
                await state.vault.audit("dead_letter", pid, {"reason": "name_fetch_failed"})
                state.stats.count("dead_lettered")
                return _error(502, "dead_letter", f"cannot fetch recipient info for {pid}; message NOT sent")
            if user is None:
                await state.vault.audit("dead_letter", pid, {"reason": "canvas_user_missing"})
                state.stats.count("dead_lettered")
                return _error(422, "dead_letter", f"recipient {pid} no longer exists upstream; message NOT sent")
            full_names[rid] = user.get("name", "")
            first_names[rid] = user.get("name", "").split(" ", 1)[0] if user.get("name") else ""

    def _apply(text: str, rid: int | None) -> str:
        if settings.nameless_mode:
            return _ALL_PLACEHOLDER_RE.sub("", text)
        if rid is not None:
            text = _FIRST_NAME_RE.sub(first_names.get(rid, ""), text)
            text = _FULL_NAME_RE.sub(full_names.get(rid, ""), text)
        return text

    # 3. Forward. Personalized placeholders require single-recipient sends
    #    (Canvas has no per-recipient templating).
    if has_placeholder and not settings.nameless_mode and len(real_ids) > 1:
        responses = []
        for rid in real_ids:
            per = dict(payload)
            per["recipients"] = [str(rid)]  # self-hosted Canvas rejects int recipients
            per["body"] = _apply(body_text, rid)
            per["subject"] = _apply(subject_text, rid)
            try:
                responses.append(await state.canvas.request("POST", "/api/v1/conversations", json_body=per))
            except UpstreamError as e:
                state.stats.count("upstream_error")
                return _error(502, "upstream_error", str(e))
        final = responses[-1]
    else:
        fwd = dict(payload)
        template_rid = real_ids[0] if len(real_ids) == 1 else None
        fwd["body"] = _apply(body_text, template_rid)
        fwd["subject"] = _apply(subject_text, template_rid)
        fwd["recipients"] = [str(r) for r in real_ids]  # self-hosted Canvas rejects int recipients
        try:
            final = await state.canvas.request("POST", "/api/v1/conversations", json_body=fwd)
        except UpstreamError as e:
            state.stats.count("upstream_error")
            return _error(502, "upstream_error", str(e))

    # 4. Audit send (pseudonyms only — never names/real ids in audit payloads).
    await state.vault.audit("send", None, {"pseudonyms": pseudonyms, "count": len(pseudonyms)})
    state.stats.count("sends")

    # 5. Re-pseudonymize the response body.
    content_type = final.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = await pseudonymize_body(final.json(), state.vault)
            return JSONResponse(status_code=final.status_code, content=data)
        except ValueError:
            pass
    from fastapi import Response as _FastAPIResponse

    return _FastAPIResponse(content=final.content, status_code=final.status_code,
                            media_type=final.headers.get("content-type"))
