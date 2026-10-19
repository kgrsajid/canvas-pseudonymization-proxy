"""§4.2 strip/passthrough tables applied to ALL endpoints (user objects are nested
everywhere in Canvas: enrollments[].user, submissions[].user, conversations...).

Behavioral data (grades, scores, page views, timestamps) passes through untouched.
"""

from __future__ import annotations

# Fields stripped from any user-shaped object, wherever it appears.
# Design §4.2: name, sortable_name, short_name, first_name, email, login_id,
# sis_user_id, sis_login_id, avatar_url, bio, pronouns, uuid.
STRIP_USER_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "full_name",
        "sortable_name",
        "short_name",
        "first_name",
        "last_name",
        "middle_name",
        "preferred_name",
        "email",
        "login_id",
        "sis_user_id",
        "sis_login_id",
        "avatar_url",
        "bio",
        "pronouns",
        "uuid",
    }
)

# Keys whose VALUES are user ids to pseudonymize, in any object.
USER_ID_KEYS: frozenset[str] = frozenset(
    {
        "user_id",
        "student_id",
        "canvas_user_id",
        "target_user_id",
        # Reviewer-added (P1 review): user-id fields in other positions
        # (conversations.sender_id, observer pairings, submission.grader_id, ...).
        "sender_id",
        "observer_id",
        "grader_id",
        "last_user_id",
        "author_id",
        "submitter_id",
        "associated_user_id",  # observer pairing target (Canvas enrollments)
    }
)

# String URL fields whose path may embed a real user id (/users/<id>,
# /grades/<id>, /submissions/<id>) — the id segment is pseudonymized in place
# by reads.py.
URL_WITH_USER_ID_KEYS: frozenset[str] = frozenset({"html_url", "url"})

# Keys whose values are LISTS of user ids (conversations messages/participants,
# bulk user lists). Each int element is pseudonymized.
# - "student_ids": assignment overrides ({"student_ids": [3, 4]}) — Canvas
#   AssignmentOverride object; each entry is a Canvas user id.
# - "observer_ids": observer-pairing payloads (audit of Canvas API docs shows
#   observer ids travel in observer_id (scalar, in USER_ID_KEYS) and in some
#   pairing/list payloads as "observer_ids"); covered defensively.
USER_ID_LIST_KEYS: frozenset[str] = frozenset(
    {
        "participating_user_ids",
        "audience",
        "user_ids",
        "observed_user_ids",
        "recipients",
        "student_ids",
        "observer_ids",
    }
)

# Endpoint patterns where top-level objects are user objects themselves
# (their "id" field is a user id needing pseudonymization).
USER_OBJECT_ENDPOINTS: tuple[str, ...] = (
    "/api/v1/users/",          # /api/v1/users/<id>, /api/v1/courses/<id>/users
    "/api/v1/search/recyclers",
    "/api/v1/courses/",        # nested under: .../users, .../enrollments, .../submissions handled structurally
)

# Explicitly NOT stripped (design §4.2 passthrough): grades, score, submitted_at,
# workflow_state, page_view fields, last_activity_at, total_activity_time,
# time_zone, locale, course ids/names/terms, assignment names.

# Non-user "id" keys that must stay real integers (course, assignment, etc.).
# The "id" key itself is only rewritten when the enclosing object is user-shaped,
# decided structurally in reads.py.
