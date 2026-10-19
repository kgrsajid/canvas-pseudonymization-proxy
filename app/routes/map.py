"""POST /proxy/v1/map — real Canvas user id -> pseudonym (design §6 adjunct).

The app needs this ONE direction of the vault map: at LTI launch it holds
the launching user's real ``canvas_user_id`` (from the LTI custom claim) and
must key session claims / ownership checks on the pseudonym that the rest of
the system uses — without ever storing real ids in the app DB.

- Authenticated with the same layered auth as every route (mTLS and/or HMAC).
- The mapping is lazily created via ``vault.map_subject`` (deterministic
  HMAC), so a launching user who has never been roster-synced still gets a
  stable pseudonym — exactly the one every other endpoint returns for them.
- Every call is audited (event "map"). Real ids appear in the audit detail
  (they are not names); pseudonyms elsewhere.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import AuthError, verify_request
from ..config import get_settings
from ..main import get_app_state

router = APIRouter()


class MapRequest(BaseModel):
    canvas_user_id: int = Field(..., gt=0)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": [{"message": message, "error_code": code}]})


@router.post("/proxy/v1/map")
async def map_subject(req: MapRequest, request: Request) -> JSONResponse:
    settings = get_settings()
    state = get_app_state()
    body = (await request.body()) or b""

    try:
        verify_request(request, settings, body)
    except AuthError as e:
        return _error(e.status, e.code, str(e))

    try:
        pseudonym = await state.vault.map_subject(req.canvas_user_id)
    except Exception as e:  # PseudonymError (collision) or backend failure — fail closed
        state.stats.count("map_error")
        return _error(500, "map_failed", f"cannot map subject: {e}")

    await state.vault.audit(
        "map",
        pseudonym,
        {"canvas_user_id": req.canvas_user_id, "via": "proxy/v1/map"},
    )
    state.stats.count("mapped")
    return JSONResponse(content={"pseudonym": pseudonym})
