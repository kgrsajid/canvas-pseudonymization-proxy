"""Golden fixture pipeline tests: pseudonymize_body over realistic Canvas shapes,
automated PII scanner asserting ZERO forbidden keys, and planted-violation scanner proof."""

from app.transforms.reads import find_forbidden_keys, pseudonymize_body
from app.pseudonym import compute_pseudonym, is_valid_pseudonym
from tests.fixtures import golden

KEY = "g" * 48


class FakeVault:
    """Minimal stand-in matching the vault surface used by reads.py."""

    def __init__(self, key: str):
        self.key = key

    async def map_subject(self, uid: int) -> str:
        return compute_pseudonym(uid, self.key)


async def run(data):
    return await pseudonymize_body(data, FakeVault(KEY))


async def test_enrollments():
    out = await run(golden.ENROLLMENTS)
    assert find_forbidden_keys(out) == []
    for enr in out:
        assert is_valid_pseudonym(enr["user_id"])
        assert is_valid_pseudonym(enr["user"]["id"])
    # passthrough behavioral fields
    assert out[0]["last_activity_at"] == golden.ENROLLMENTS[0]["last_activity_at"]
    assert out[0]["total_activity_time"] == 45230
    assert out[2]["type"] == "TeacherEnrollment"  # teachers pseudonymized too


async def test_users_list():
    out = await run(golden.USERS)
    assert find_forbidden_keys(out) == []
    assert all(is_valid_pseudonym(u["id"]) for u in out)
    assert "email" not in out[0] and "sis_user_id" not in out[0]
    assert out[0]["time_zone"] == "Asia/Almaty"  # passthrough


async def test_submissions():
    out = await run(golden.SUBMISSIONS)
    assert find_forbidden_keys(out) == []
    assert is_valid_pseudonym(out[0]["user_id"])
    assert out[0]["score"] == 82.5 and out[0]["grade"] == "82.5"  # behavioral untouched
    assert out[1]["missing"] is True and out[1]["submitted_at"] is None
    assert out[0]["assignment_id"] == 55  # non-user id untouched


async def test_page_views_passthrough():
    out = await run(golden.PAGE_VIEWS)
    assert out == golden.PAGE_VIEWS  # no user ids in payload -> byte-identical


async def test_conversation_response():
    out = await run(golden.CONVERSATION_RESPONSE)
    assert find_forbidden_keys(out) == []
    assert is_valid_pseudonym(out[0]["participants"][0]["id"])


async def test_nested_depth():
    deep = {"data": {"results": [{"user": {"id": 9, "name": "Deep Name", "email": "d@x.test"}}]}}
    out = await run(deep)
    assert find_forbidden_keys(out) == []
    assert is_valid_pseudonym(out["data"]["results"][0]["user"]["id"])


async def test_user_id_positions_all_pseudonymized():
    """Reviewer regression (P1 review): user ids in EVERY position — scalar
    author/sender/observer/grader fields AND list-valued audience /
    participating_user_ids — must be pseudonymized; non-user ids untouched."""
    conv = {
        "id": 777,
        "messages": [
            {
                "id": 5,
                "body": "hi",
                "author_id": 3,
                "submitter_id": 3,
                "participating_user_ids": [3, 4],
            }
        ],
        "audience": [3, 4],
        "user_ids": [3, 10],
        "sender_id": 2,
        "observer_id": 9,
        "grader_id": 2,
    }
    out = await run(conv)
    assert out["id"] == 777  # conversation id untouched
    assert out["messages"][0]["id"] == 5  # message id untouched
    m = out["messages"][0]
    for v in (m["author_id"], m["submitter_id"], out["sender_id"], out["observer_id"], out["grader_id"]):
        assert is_valid_pseudonym(v), f"scalar user-id field leaked raw id: {v}"
    for v in (m["participating_user_ids"] + out["audience"] + out["user_ids"]):
        assert is_valid_pseudonym(v), f"list user-id field leaked raw id: {v}"
    # distinct ids -> distinct pseudonyms, same id -> same pseudonym
    assert m["author_id"] == m["submitter_id"]
    assert out["user_ids"][1] != out["user_ids"][0]
    assert find_forbidden_keys(out) == []


async def test_scanner_catches_planted_violations():
    """The scanner itself is tested: planted violations MUST be caught."""
    planted = [
        {"id": 1, "name": "Leak"},                       # top level
        {"ok": True, "nested": {"email": "leak@x.test"}},  # nested
        [{"sis_user_id": "SIS-1"}, {"uuid": "abc"}],      # in list, multiple
    ]
    for p in planted:
        assert find_forbidden_keys(p) != [], f"scanner missed violation in {p}"
    assert len(find_forbidden_keys(planted[2])) == 2


async def test_id_fields_in_other_positions():
    """Reviewer-added: sender_id / observer_id / grader_id are user ids too."""
    convo = [{"id": 50, "sender_id": 3, "participants": [{"id": 3, "name": "A"}],
              "messages": [{"id": 9, "sender_id": 3, "body": "hi"}]}]
    out = await run(convo)
    assert out[0]["sender_id"] == compute_pseudonym(3, KEY)
    assert out[0]["messages"][0]["sender_id"] == compute_pseudonym(3, KEY)
    obs = {"user_id": 5, "observer_id": 6, "observed_user": {"id": 5, "name": "X"}}
    o2 = await run(obs)
    assert o2["observer_id"] == compute_pseudonym(6, KEY)
    sub = {"user_id": 3, "grader_id": 2}
    o3 = await run(sub)
    assert o3["grader_id"] == compute_pseudonym(2, KEY)
    for o in (out, o2, o3):
        assert find_forbidden_keys(o) == []


async def test_behavioral_values_byte_identical():
    """Data minimization, NOT transformation: behavioral values unchanged."""
    submission = golden.SUBMISSIONS[0]
    out = (await run([submission]))[0]
    for k in ("score", "grade", "submitted_at", "late", "missing", "workflow_state", "attempt"):
        assert out[k] == submission[k]


async def test_html_url_user_segment_pseudonymized():
    """Reviewer-added: html_url embeds the real user id -> pseudonymize segment."""
    from app.transforms.reads import pseudonymize_body, find_forbidden_keys

    class V:
        async def map_subject(self, u):
            return compute_pseudonym(u, KEY)

    enr = [{"id": 101, "course_id": 1, "user_id": 9,
            "html_url": "http://canvas.test/courses/1/users/9",
            "user": {"id": 9, "name": "X"}}]
    out = await pseudonymize_body(enr, V())
    assert out[0]["html_url"] == f"http://canvas.test/courses/1/users/{compute_pseudonym(9, KEY)}"
    assert out[0]["html_url"].endswith("/users/P-" + out[0]["html_url"].rsplit("/users/P-", 1)[1])
    assert out[0]["user_id"] == compute_pseudonym(9, KEY)
    assert find_forbidden_keys(out) == []
    # non-user urls untouched
    c = {"course": {"id": 1, "html_url": "http://canvas.test/courses/1"}}
    assert (await pseudonymize_body(c, V()))["course"]["html_url"].endswith("/courses/1")


# ---------------------------------------------------------------------------
# B3 regression — non-user Canvas objects must NOT be treated as users
# ---------------------------------------------------------------------------

async def test_course_object_survives_unchanged():
    """Reviewer-flagged case: a course has `name` but is NOT a user object.
    Its id and name must pass through untouched."""
    course = {"id": 1, "name": "Algorithm & Data Structure",
              "course_code": "CSS2002", "workflow_state": "available"}
    out = await run(course)
    assert out == course  # byte-identical: id 1 stays 1, name stays real
    assignment = {"id": 55, "name": "Lab 3", "course_id": 1,
                  "workflow_state": "published", "points_possible": 10}
    assert await run(assignment) == assignment
    section = {"id": 7, "name": "Section A", "course_id": 1,
               "workflow_state": "active"}
    assert await run(section) == section
    file_obj = {"id": 88, "display_name": "notes.pdf", "filename": "notes.pdf",
                "content-type": "application/pdf", "size": 1024}
    assert await run(file_obj) == file_obj


async def test_named_objects_under_non_user_parents_keep_real_ids():
    """Nested name-bearing non-user objects (modules, groups, outcomes...)."""
    payload = {"modules": [{"id": 10, "name": "Week 1", "state": "active"}],
               "assignment": {"id": 55, "name": "Lab 3"}}
    out = await run(payload)
    assert out["modules"][0]["id"] == 10
    assert out["modules"][0]["name"] == "Week 1"
    assert out["assignment"]["id"] == 55


# ---------------------------------------------------------------------------
# B4a — student_ids / observer_id lists pseudonymized
# ---------------------------------------------------------------------------

async def test_assignment_override_student_ids_pseudonymized():
    """AssignmentOverride.student_ids is a list of Canvas user ids."""
    override = {"id": 1, "assignment_id": 55, "course_id": 1,
                "student_ids": [3, 4], "title": "Pair 1"}
    out = await run(override)
    assert out["student_ids"] == [compute_pseudonym(3, KEY), compute_pseudonym(4, KEY)]
    assert out["id"] == 1 and out["assignment_id"] == 55  # non-user ids untouched
    pairing = {"user_id": 5, "observer_ids": [8]}
    o2 = await run(pairing)
    assert o2["user_id"] == compute_pseudonym(5, KEY)
    assert o2["observer_ids"] == [compute_pseudonym(8, KEY)]


# ---------------------------------------------------------------------------
# B4b — bare user refs inside known user lists get their id pseudonymized
# ---------------------------------------------------------------------------

async def test_bare_user_refs_in_known_user_lists():
    """[{"id": 3}] under users/students/observed_users/participants IS a user
    ref even without name-ish fields; the id must be pseudonymized. Rich
    objects (enrollments with type/user_id) keep their own real ids."""
    data = {"users": [{"id": 3}, {"id": 4, "name": "Someone"}],
            "students": [{"id": 7}],
            "participants": [{"id": 9, "full_name": "P"}],
            "observed_users": [{"id": 11}]}
    out = await run(data)
    assert out["users"][0]["id"] == compute_pseudonym(3, KEY)
    assert out["users"][1]["id"] == compute_pseudonym(4, KEY)
    assert "name" not in out["users"][1]
    assert out["students"][0]["id"] == compute_pseudonym(7, KEY)
    assert out["participants"][0]["id"] == compute_pseudonym(9, KEY)
    assert "full_name" not in out["participants"][0]
    assert out["observed_users"][0]["id"] == compute_pseudonym(11, KEY)
    # enrollment objects under "enrollments" keep enrollment ids (not users)
    enrs = {"enrollments": [{"id": 101, "user_id": 3, "type": "StudentEnrollment"}]}
    o2 = await run(enrs)
    assert o2["enrollments"][0]["id"] == 101
    assert o2["enrollments"][0]["user_id"] == compute_pseudonym(3, KEY)
