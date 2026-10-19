"""POST /proxy/v1/resolve — display-time re-identification (design §5.3).

Role-gated: the caller must present a valid instructor/TA role assertion for
the course context; the proxy INDEPENDENTLY verifies against Canvas. Names
fetched live, returned in a short-lived display map (TTL 60 s semantics —
documented for the app; proxy stateless here). Rate-limited per session.
Fully audited. No name is ever persisted or logged.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import AuthError, verify_request
from ..canvas_client import UpstreamError
from ..config import get_settings
from ..main import get_app_state
from ..pseudonym import is_valid_pseudonym

router = APIRouter()

INSTRUCTOR_ROLES = {"TeacherEnrollment", "TaEnrollment", "Instructor", "TA"}
STAFF_ENROLLMENT_TYPES = {"TeacherEnrollment", "TaEnrollment"}


class ResolveRequest(BaseModel):
    context: str = Field(..., pattern=r"^course:\d+$")
    pseudonyms: list[str] = Field(..., min_length=1, max_length=200)
    session_token: str = Field(..., min_length=1)  # app-side LTI session id (audited)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": [{"message": message, "error_code": code}]})


class _RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True


_rl: _RateLimiter | None = None


def _rate_limiter(settings) -> _RateLimiter:
    global _rl
    if _rl is None or _rl.per_minute != settings.resolve_rate_limit_per_min:
        _rl = _RateLimiter(settings.resolve_rate_limit_per_min)
    return _rl


async def _verify_instructor(state, settings, course_id: int, session_token: str) -> bool:
    """Independently verify the session's user holds an instructor/TA role in the
    course, via Canvas. The session_token is the app-side identity reference; the
    app must present it as 'user:<canvas_session_or_pseudonym>' — we resolve the
    pseudonym to a real id via the vault and check enrollments upstream.

    Format: 'P-xxxxxxxxxx' (the instructor's own pseudonym, mapped in the vault).
    """
    token = session_token.strip()
    if not is_valid_pseudonym(token):
        return False
    real_id = await state.vault.reverse_lookup(token)
    if real_id is None:
        return False
    try:
        enrollments = await state.canvas.get_enrollments_for_course(course_id)
    except UpstreamError:
        return False  # fail closed: cannot verify -> deny
    for enr in enrollments:
        if not isinstance(enr, dict):
            continue
        if enr.get("user_id") == real_id and (
            enr.get("type") in STAFF_ENROLLMENT_TYPES or enr.get("role") in INSTRUCTOR_ROLES
            or any(r in INSTRUCTOR_ROLES for r in (enr.get("roles") or []) if isinstance(r, str))
        ):
            return True
    return False


@router.post("/proxy/v1/resolve")
async def resolve(req: ResolveRequest, request: Request) -> JSONResponse:
    settings = get_settings()
    state = get_app_state()
    body = (await request.body()) or b""

    try:
        verify_request(request, settings, body)
    except AuthError as e:
        return _error(e.status, e.code, str(e))

    if not _rate_limiter(settings).allow(req.session_token):
        return _error(429, "rate_limited", "resolve rate limit exceeded")

    course_id = int(req.context.split(":", 1)[1])

    if not await _verify_instructor(state, settings, course_id, req.session_token):
        await state.vault.audit("resolve_denied", None, {"context": req.context, "reason": "not_instructor"})
        state.stats.count("resolve_denied")
        return _error(403, "forbidden", "instructor role required for this course context")

    # Course-scope enforcement (design §5.3): re-identification is limited to
    # students actually enrolled in THIS course. Fetch the course roster and
    # refuse to resolve anyone outside it — an instructor of course A must not
    # re-identify a student of course B.
    try:
        enrollments = await state.canvas.get_enrollments_for_course(course_id)
    except UpstreamError:
        await state.vault.audit("resolve_denied", None,
                                {"context": req.context, "reason": "enrollment_check_failed"})
        state.stats.count("resolve_denied")
        return _error(403, "forbidden", "course enrollment could not be verified")
    enrolled_user_ids = {e.get("user_id") for e in enrollments if isinstance(e, dict)}

    # Resolve each pseudonym; unknown -> reported as unknown, never guessed.
    # Out-of-course -> omitted from the display map (and audited).
    display: dict[str, dict[str, str]] = {}
    unknown: list[str] = []
    out_of_scope: list[str] = []
    for pid in req.pseudonyms:
        if not is_valid_pseudonym(pid):
            return _error(400, "malformed_subject", f"{pid!r} is not a valid pseudonym")
        real_id = await state.vault.reverse_lookup(pid)
        if real_id is None:
            unknown.append(pid)
            continue
        if real_id not in enrolled_user_ids:
            out_of_scope.append(pid)
            continue
        try:
            user = await state.canvas.get_user(real_id)
        except UpstreamError:
            unknown.append(pid)
            continue
        if user is None:
            unknown.append(pid)
            continue
        display[pid] = {"display_name": user.get("name", "")}  # transient — response only

    if out_of_scope:
        await state.vault.audit("resolve_out_of_scope", None,
                                {"context": req.context, "pseudonyms": sorted(out_of_scope)})

    await state.vault.audit(
        "resolve",
        None,
        {"context": req.context, "session": req.session_token, "resolved": sorted(display), "unknown": unknown},
    )
    state.stats.count("resolved", n=len(display))
    return JSONResponse(
        content={
            "display_map": display,
            "unknown": unknown,
            "out_of_scope": out_of_scope,
            "ttl_s": settings.resolve_display_ttl_s,  # app must discard after this
        }
    )
