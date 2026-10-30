from datetime import datetime, timedelta, timezone

import httpx

from study_coach.lms.canvas import CanvasClient, html_to_text

BASE = "https://school.instructure.com"


def make_client(handler) -> CanvasClient:
    return CanvasClient(BASE, "token", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_pagination_follows_link_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token"
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"id": 2, "name": "Java"}])
        next_url = f"{BASE}/api/v1/courses?page=2&per_page=100"
        return httpx.Response(
            200, json=[{"id": 1, "name": "Regression"}], headers={"Link": f'<{next_url}>; rel="next"'}
        )

    courses = make_client(handler).list_courses()
    assert [c.name for c in courses] == ["Regression", "Java"]


def test_upcoming_assignments_filters_by_window():
    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/courses"):
            return httpx.Response(200, json=[{"id": 7, "name": "Stats"}])
        return httpx.Response(
            200,
            json=[
                {"id": 1, "name": "HW 5", "due_at": soon, "points_possible": 10,
                 "submission": {"workflow_state": "unsubmitted"}, "description": "<p>Do <b>it</b></p>"},
                {"id": 2, "name": "Final", "due_at": later},
                {"id": 3, "name": "Undated", "due_at": None},
            ],
        )

    items = make_client(handler).upcoming_assignments(days=7)
    assert [a.name for a in items] == ["HW 5"]
    assert items[0].description == "Do it"
    assert items[0].submitted is False


def test_grades_compute_percent_and_skip_ungraded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"id": 1, "name": "Quiz 1", "points_possible": 20, "submission": {"score": 17}},
                {"id": 2, "name": "Quiz 2", "points_possible": 20, "submission": {"score": None}},
            ],
        )

    grades = make_client(handler).grades(7)
    assert len(grades) == 1 and grades[0].percent == 85.0


def test_html_to_text():
    assert html_to_text("<p>Line one</p><p>Line &amp; two</p>") == "Line one\nLine & two"


def test_courses_request_filters_to_available():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = request.url.params.multi_items()
        return httpx.Response(200, json=[])

    make_client(handler).list_courses()
    assert ("state[]", "available") in seen["params"]


def test_announcements_send_end_date_so_recent_posts_are_included():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params.multi_items())
        return httpx.Response(200, json=[])

    make_client(handler).announcements([7], days=60)
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    assert seen["params"]["end_date"] == tomorrow


def test_expired_token_gives_helpful_error():
    import pytest

    client = make_client(lambda request: httpx.Response(401, json={"errors": [{"message": "Invalid access token."}]}))
    with pytest.raises(RuntimeError, match="New Access Token"):
        client.list_courses()
