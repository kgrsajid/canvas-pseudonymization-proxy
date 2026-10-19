"""Synthetic golden fixtures — realistic Canvas response shapes.

ALL identities below are SYNTHETIC and obviously fictional (Тестова, Примеров,
Демова, Образцов, Синтетик, Преподавателев). They exist only to exercise the
Cyrillic-script code paths and the user-object heuristic. user_id integers are
arbitrary test constants, not real Canvas ids. Emails use the reserved
example.edu domain (RFC 2606). No real student or staff data appears here.
"""

# Course enrollment list (Canvas /api/v1/courses/1/enrollments)
ENROLLMENTS = [
    {
        "id": 101,
        "course_id": 1,
        "user_id": 3,
        "type": "StudentEnrollment",
        "enrollment_state": "active",
        "last_activity_at": "2026-10-15T09:12:00Z",
        "total_activity_time": 45230,
        "user": {
            "id": 3,
            "name": "Алия Тестова",
            "sortable_name": "Тестова, Алия",
            "short_name": "Алия",
            "login_id": "aliya.testova@example.edu",
            "email": "aliya.testova@example.edu",
            "sis_user_id": "SIS-1003",
            "sis_login_id": "aliya.testova@example.edu",
            "uuid": "8b1c-dead-beef-0003",
            "avatar_url": "https://canvas.test/files/avatar3",
            "bio": "Second-year student",
            "pronouns": "she/her",
            "time_zone": "Asia/Almaty",
            "locale": "ru",
        },
    },
    {
        "id": 102,
        "course_id": 1,
        "user_id": 4,
        "type": "StudentEnrollment",
        "enrollment_state": "active",
        "last_activity_at": "2026-10-16T14:40:00Z",
        "total_activity_time": 12210,
        "user": {
            "id": 4,
            "name": "Берик Примеров",
            "sortable_name": "Примеров, Берик",
            "login_id": "berik.primerov@example.edu",
            "email": "berik.primerov@example.edu",
            "sis_user_id": "SIS-1004",
            "uuid": "8b1c-dead-beef-0004",
            "pronouns": "he/him",
            "time_zone": "Asia/Almaty",
        },
    },
    {
        "id": 103,
        "course_id": 1,
        "user_id": 2,
        "type": "TeacherEnrollment",
        "enrollment_state": "active",
        "user": {
            "id": 2,
            "name": "Иван Преподавателев",
            "sortable_name": "Преподавателев, Иван",
            "login_id": "teacher@example.edu",
            "email": "teacher@example.edu",
            "uuid": "8b1c-dead-beef-0002",
        },
    },
]

# Users list (/api/v1/courses/1/users)
USERS = [
    {
        "id": 3,
        "name": "Алия Тестова",
        "sortable_name": "Тестова, Алия",
        "short_name": "Алия",
        "email": "aliya.testova@example.edu",
        "login_id": "aliya.testova@example.edu",
        "sis_user_id": "SIS-1003",
        "uuid": "8b1c-dead-beef-0003",
        "avatar_url": "https://canvas.test/files/avatar3",
        "bio": "Second-year student",
        "pronouns": "she/her",
        "time_zone": "Asia/Almaty",
    },
    {"id": 5, "name": "Гаухар Демова", "sortable_name": "Демова, Гаухар",
     "email": "gauhar.demo@example.edu", "login_id": "gauhar.demo@example.edu",
     "sis_user_id": "SIS-1005", "uuid": "8b1c-dead-beef-0005"},
]

# Submissions (/api/v1/courses/1/students/submissions)
SUBMISSIONS = [
    {
        "id": 9001,
        "assignment_id": 55,
        "course_id": 1,
        "user_id": 3,
        "score": 82.5,
        "grade": "82.5",
        "submitted_at": "2026-10-14T18:03:00Z",
        "late": False,
        "missing": False,
        "workflow_state": "graded",
        "attempt": 1,
        "user": {"id": 3, "name": "Алия Тестова", "login_id": "aliya.testova@example.edu"},
    },
    {
        "id": 9002,
        "assignment_id": 55,
        "user_id": 4,
        "score": None,
        "grade": None,
        "submitted_at": None,
        "late": True,
        "missing": True,
        "workflow_state": "unsubmitted",
        "attempt": 0,
    },
]

# Page views (/api/v1/users/3/page_views)
PAGE_VIEWS = [
    {"created_at": "2026-10-16T10:00:00Z", "url": "https://canvas.test/courses/1/assignments",
     "context_type": "Course", "context_id": 1, "interaction_seconds": 42,
     "user_agent": "Mozilla/5.0", " participated": False},
    {"created_at": "2026-10-16T10:05:00Z", "url": "https://canvas.test/courses/1/quizzes/7",
     "context_type": "Quiz", "context_id": 7, "interaction_seconds": 611},
]

# Conversation create response (Canvas POST /api/v1/conversations)
CONVERSATION_RESPONSE = [
    {
        "id": 777,
        "subject": "Добро пожаловать",
        "workflow_state": "active",
        "last_message": "Привет, Алия!",
        "participants": [
            {"id": 3, "name": "Алия Тестова", "full_name": "Алия Тестова",
             "avatar_url": "https://canvas.test/files/avatar3"},
            {"id": 2, "name": "Иван Преподавателев"},
        ],
        "audience": 1,
    }
]

# Single user object (/api/v1/users/3)
USER_3 = ENROLLMENTS[0]["user"]
