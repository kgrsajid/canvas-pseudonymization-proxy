"""Request rewriting both directions: paths, query params, bodies. Fail-closed drills."""

import pytest

from app.transforms.requests import (
    MalformedSubject,
    UnknownSubject,
    rewrite_body,
    rewrite_path,
    rewrite_query,
)
from app.pseudonym import compute_pseudonym

KEY = "r" * 48


@pytest.fixture
def fv():
    return FakeVault()


class FakeVault:
    def __init__(self):
        self.map = {}

    def add(self, uid: int):
        p = compute_pseudonym(uid, KEY)
        self.map[p] = uid
        return p

    async def reverse_lookup(self, p):
        return self.map.get(p)


async def test_path_rewrite(fv):
    p3 = fv.add(3)
    assert await rewrite_path(f"/api/v1/users/{p3}/page_views", fv) == "/api/v1/users/3/page_views"


async def test_path_no_pids_untouched(fv):
    assert await rewrite_path("/api/v1/courses/1/assignments", fv) == "/api/v1/courses/1/assignments"


async def test_path_unknown_pid_fails_closed():
    fv = FakeVault()
    with pytest.raises(UnknownSubject):
        await rewrite_path("/api/v1/users/P-ZZZZZZZZZZ/page_views", fv)


async def test_path_malformed_pid_fails_closed():
    fv = FakeVault()
    with pytest.raises(MalformedSubject):
        await rewrite_path("/api/v1/users/P-ABC/page_views", fv)


async def test_query_rewrite(fv):
    p3, p4 = fv.add(3), fv.add(4)
    out = await rewrite_query({"user_id[]": [p3, p4], "per_page": ["100"]}, fv)
    assert out["user_id[]"] == ["3", "4"]
    assert out["per_page"] == ["100"]


async def test_query_comma_joined(fv):
    p3, p4 = fv.add(3), fv.add(4)
    out = await rewrite_query({"user_id[]": [f"{p3},{p4}"]}, fv)
    assert out["user_id[]"] == ["3,4"]


async def test_query_unknown_fails_closed():
    fv = FakeVault()
    with pytest.raises(UnknownSubject):
        await rewrite_query({"user_id[]": ["P-ZZZZZZZZZZ"]}, fv)


async def test_body_recipients(fv):
    p3 = fv.add(3)
    out = await rewrite_body({"recipients": [p3], "body": "hi"}, fv, resolve_keys=frozenset({"recipients"}))
    assert out["recipients"] == [3]
    assert out["body"] == "hi"  # free text untouched


async def test_body_unknown_recipient_fails_closed():
    fv = FakeVault()
    with pytest.raises(UnknownSubject):
        await rewrite_body({"recipients": ["P-ZZZZZZZZZZ"]}, fv, resolve_keys=frozenset({"recipients"}))


async def test_body_malformed_recipient_fails_closed():
    fv = FakeVault()
    with pytest.raises(MalformedSubject):
        await rewrite_body({"recipients": ["P-BAD"]}, fv, resolve_keys=frozenset({"recipients"}))


async def test_body_without_resolve_keys_untouched(fv):
    body = {"note": "P-AAAAAAAAAA is a string in text"}
    assert await rewrite_body(body, fv) == body
