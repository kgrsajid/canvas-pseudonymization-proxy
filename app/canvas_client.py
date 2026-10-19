"""httpx upstream Canvas client: token auth, retries, timeout, rate-limit awareness."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import Settings


class UpstreamError(Exception):
    """Honest upstream failure — surfaced to caller, never faked."""

    def __init__(self, status: int | None, message: str):
        self.status = status
        super().__init__(message)


class CanvasClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: httpx.AsyncClient | None = None
        self._window_start = time.monotonic()
        self._window_count = 0
        self._lock = asyncio.Lock()

    async def _get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            if not self.settings.canvas_api_token:
                raise UpstreamError(None, "CANVAS_API_TOKEN not configured (fail closed)")
            self._client = httpx.AsyncClient(
                base_url=self.settings.upstream_url.rstrip("/"),
                timeout=httpx.Timeout(self.settings.upstream_timeout_s, connect=self.settings.upstream_connect_timeout_s),
                headers={
                    "Authorization": f"Bearer {self.settings.canvas_api_token}",
                    "Accept": "application/json",
                },
                follow_redirects=False,
            )
        return self._client

    async def _throttle(self) -> None:
        """Local sliding window so we stay under Canvas's 300/min limit."""
        async with self._lock:
            now = time.monotonic()
            if now - self._window_start >= 60:
                self._window_start = now
                self._window_count = 0
            if self._window_count >= self.settings.upstream_rate_limit_per_min:
                wait = 60 - (now - self._window_start)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._window_start = time.monotonic()
                self._window_count = 0
            self._window_count += 1

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Raw request with retries on transient failures. Path must start with /api/v1/."""
        await self._throttle()
        client = await self._get()
        last_exc: Exception | None = None
        attempts = 1 + max(0, self.settings.upstream_max_retries)
        for attempt in range(attempts):
            try:
                resp = await client.request(method, path, params=params, json=json_body, headers=headers)
            except httpx.TransportError as e:
                last_exc = e
                if attempt < attempts - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise UpstreamError(None, f"upstream transport error: {e}") from e
            # Retry transient upstream statuses only
            if resp.status_code in (429, 502, 503, 504) and attempt < attempts - 1:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 1.0 * (attempt + 1)
                except ValueError:
                    delay = 1.0 * (attempt + 1)
                await asyncio.sleep(min(delay, 30))
                continue
            return resp
        raise UpstreamError(None, f"upstream exhausted retries: {last_exc}")

    # Convenience helpers -------------------------------------------------

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self.request("GET", path, params=params)
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, f"upstream {resp.status_code} on GET {path}")
        return resp.json()

    async def get_user(self, canvas_user_id: int) -> dict[str, Any] | None:
        """Fetch a user object from Canvas. 404 -> None; other errors raise."""
        resp = await self.request("GET", f"/api/v1/users/{canvas_user_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, f"upstream {resp.status_code} fetching user {canvas_user_id}")
        return resp.json()

    async def get_enrollments_for_course(self, canvas_course_id: int) -> list[dict[str, Any]]:
        """All active enrollments of a course (admin view includes all roles)."""
        out: list[dict[str, Any]] = []
        params: dict[str, Any] = {"per_page": 100}
        path = f"/api/v1/courses/{canvas_course_id}/enrollments"
        while path:
            resp = await self.request("GET", path, params=params if out == [] else None)
            if resp.status_code >= 400:
                raise UpstreamError(resp.status_code, f"upstream {resp.status_code} on enrollments")
            data = resp.json()
            if isinstance(data, list):
                out.extend(data)
            nxt = resp.links.get("next", {}).get("url")
            path = nxt.replace(self.settings.upstream_url.rstrip("/"), "", 1) if nxt else None
            params = None
        return out

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
