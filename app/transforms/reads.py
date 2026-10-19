"""Recursive response-body walker (design §5.1 / §4.2).

- Pseudonymizes user ids (user_id, student_id, ... and the "id" of user-shaped objects).
- Strips forbidden fields wherever they appear, at any nesting depth.
- Lazily populates the vault on first sight of a new user id.
- Behavioral data passes through unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from ..pseudonym import compute_pseudonym
from ..vault import Vault
from .rules import STRIP_USER_FIELDS, USER_ID_KEYS, USER_ID_LIST_KEYS, URL_WITH_USER_ID_KEYS

# html_url shapes that embed a REAL user id in the path:
#   .../users/<id>               (user/enrollment pages)
#   .../grades/<id>              (per-student gradebook: /courses/1/grades/9)
#   .../submissions/<id>         (per-student submission: /courses/1/assignments/2/submissions/9)
# The trailing id segment is pseudonymized in place by reads.py.
_USER_URL_RE = re.compile(
    r"^(?P<pre>.*/(?:users|grades|submissions)/)(?P<uid>\d+)(?P<post>/.*)?$"
)

# User-specific marker fields: a dict carrying ANY of these is a Canvas user
# object (courses/assignments/sections never have login_id/sis_user_id/email/
# sortable_name/short_name/avatar_url/bio). Bare "name" alone is NOT enough —
# every course, assignment, section and file also has a "name".
_USER_SPECIFIC_FIELDS = frozenset(
    {"login_id", "sis_user_id", "email", "sortable_name", "short_name",
     "avatar_url", "bio"}
)

# Parent keys under which a dict containing "name" IS a user object
# (Canvas nests users under these keys; non-user objects do not appear here).
_USER_NAME_PARENTS = frozenset(
    {"user", "users", "students", "observed_users", "observed_user",
     "participants"}
)

# List parent keys whose items are user objects. Items under these parents are
# treated as users even when they are BARE refs like {"id": 3} with no
# name-ish fields (Canvas returns such shapes for some include/student lists).
# Items with richer key sets (e.g. enrollments with type/user_id/course_id)
# are NOT reclassified — their "id" is an enrollment id, not a user id.
KNOWN_USER_LIST_PARENTS = frozenset(
    {"users", "students", "observed_users", "participants"}
)
_BARE_USER_REF_KEYS = frozenset({"id", "name", "full_name"})


def _is_user_object(obj: dict[str, Any], parent_key: str) -> bool:
    if parent_key == "user":
        return True
    if any(f in obj for f in _USER_SPECIFIC_FIELDS):
        return True
    if "name" in obj and parent_key in _USER_NAME_PARENTS:
        return True
    if parent_key in KNOWN_USER_LIST_PARENTS and set(obj) <= _BARE_USER_REF_KEYS:
        return True  # bare user ref: {"id": 3} inside a known user list
    return False


async def pseudonymize_body(
    data: Any,
    vault: Vault,
    *,
    parent_key: str = "",
) -> Any:
    """Return a transformed copy of a Canvas response body. Never mutates input."""
    if isinstance(data, list):
        return [await pseudonymize_body(item, vault, parent_key=parent_key) for item in data]
    if isinstance(data, dict):
        is_user = _is_user_object(data, parent_key)
        out: dict[str, Any] = {}
        for key, value in data.items():
            if is_user and key in STRIP_USER_FIELDS:
                continue  # stripped — never leaked
            if key in USER_ID_KEYS and isinstance(value, int) and not isinstance(value, bool):
                out[key] = await vault.map_subject(value)
            elif key in USER_ID_LIST_KEYS and isinstance(value, list):
                # each int element is a user id -> pseudonymize; non-ints pass as-is
                out[key] = [
                    await vault.map_subject(v)
                    if isinstance(v, int) and not isinstance(v, bool)
                    else v
                    for v in value
                ]
            elif is_user and key == "id" and isinstance(value, int):
                out[key] = await vault.map_subject(value)
            elif key in URL_WITH_USER_ID_KEYS and isinstance(value, str):
                m = _USER_URL_RE.match(value)
                if m:  # e.g. html_url ".../courses/1/grades/9" -> ".../grades/P-xxxx"
                    out[key] = m.group("pre") + await vault.map_subject(int(m.group("uid"))) + (m.group("post") or "")
                else:
                    out[key] = value
            elif isinstance(value, (dict, list)):
                out[key] = await pseudonymize_body(value, vault, parent_key=key)
            else:
                out[key] = value
        return out
    return data


def find_forbidden_keys(data: Any, path: str = "$") -> list[tuple[str, str]]:
    """Scanner helper: locate any §4.2 forbidden key in a structure (for tests + selftest)."""
    hits: list[tuple[str, str]] = []
    if isinstance(data, list):
        for i, item in enumerate(data):
            hits.extend(find_forbidden_keys(item, f"{path}[{i}]"))
    elif isinstance(data, dict):
        for key, value in data.items():
            if key in STRIP_USER_FIELDS:
                hits.append((path, key))
            hits.extend(find_forbidden_keys(value, f"{path}.{key}"))
    return hits
