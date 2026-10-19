"""POST /proxy/v1/sync-roster — bulk vault population for a course (design §6)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import AuthError, verify_request
from ..canvas_client import UpstreamError
from ..config import get_settings
from ..main import get_app_state

router = APIRouter()


class RosterRequest(BaseModel):
    course_id: int = Field(..., gt=0)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": [{"message": message, "error_code": code}]})


@router.post("/proxy/v1/sync-roster")
async def sync_roster(req: RosterRequest, request: Request) -> JSONResponse:
    settings = get_settings()
    state = get_app_state()
    body = (await request.body()) or b""

    try:
        verify_request(request, settings, body)
    except AuthError as e:
        return _error(e.status, e.code, str(e))

    try:
        enrollments = await state.canvas.get_enrollments_for_course(req.course_id)
    except UpstreamError as e:
        return _error(502, "upstream_error", str(e))

    mapped = 0
    for enr in enrollments:
        uid = enr.get("user_id") if isinstance(enr, dict) else None
        if isinstance(uid, int):
            await state.vault.map_subject(uid)
            mapped += 1

    return JSONResponse(content={"course_id": req.course_id, "mapped": mapped})
