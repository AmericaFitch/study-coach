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


def test_module_files_lists_files_and_fetches_items_canvas_left_out():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/modules"):
            return httpx.Response(200, json=[
                {"id": 1, "name": "Week 1", "items": [
                    {"type": "File", "title": "Slides 1.pdf", "content_id": 11},
                    {"type": "Page", "title": "Intro", "page_url": "intro"},
                ]},
                {"id": 2, "name": "Week 2"},  # big module: items must be fetched separately
            ])
        assert request.url.path.endswith("/modules/2/items")
        return httpx.Response(200, json=[{"type": "File", "title": "Slides 2.pdf", "content_id": 22}])

    files = make_client(handler).module_files(7)
    assert [(f.id, f.module) for f in files] == [(11, "Week 1"), (22, "Week 2")]


def test_download_follows_redirect_without_sending_token_to_file_host():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files/11"):
            return httpx.Response(200, json={
                "display_name": "Slides.pdf", "content-type": "application/pdf", "size": 9,
                "url": f"{BASE}/files/11/download?verifier=abc",
            })
        if request.url.host == "school.instructure.com":
            return httpx.Response(302, headers={"Location": "https://files.example-cdn.com/11"})
        seen["file_host_auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=b"%PDF-1.4 ")

    f = make_client(handler).download_file(7, 11)
    assert f.data == b"%PDF-1.4 " and f.name == "Slides.pdf"
    assert seen["file_host_auth"] is None


def test_locked_file_says_why():
    import pytest

    client = make_client(lambda request: httpx.Response(200, json={
        "display_name": "Exam key.pdf", "locked_for_user": True,
        "lock_explanation": "This file is locked until <b>Dec 1</b>.",
    }))
    with pytest.raises(RuntimeError, match="locked until Dec 1"):
        client.download_file(7, 11)


def test_videos_are_never_downloaded():
    import pytest

    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json={
            "display_name": "Lecture 4.mp4", "content-type": "video/mp4", "size": 10, "url": f"{BASE}/files/11/download",
        })

    with pytest.raises(ValueError, match="Lecture 4.mp4 isn't a PDF"):
        make_client(handler).download_file(7, 11)
    assert requested == ["/api/v1/courses/7/files/11"]  # metadata only, no download


def test_oversized_download_is_refused():
    import pytest

    from study_coach.lms import canvas

    client = make_client(lambda request: httpx.Response(200, json={
        "display_name": "Huge scan.pdf", "content-type": "application/pdf",
        "size": canvas.MAX_DOWNLOAD_BYTES + 1, "url": f"{BASE}/x",
    }))
    with pytest.raises(RuntimeError, match="too large"):
        client.download_file(7, 11)


def test_file_links_finds_every_form_canvas_uses():
    from study_coach.lms.canvas import file_links

    raw = (
        f'<p>Read <a href="{BASE}/courses/7/files/11?verifier=x&amp;wrap=1" '
        f'data-api-endpoint="{BASE}/api/v1/courses/7/files/11">Chapter <b>1</b>.pdf</a></p>'
        f'<a href="/courses/7/files/12/download?download_frd=1">Starter.java</a>'
        f'<img src="{BASE}/courses/7/files/13/preview" alt="ER diagram">'
        f'<a data-api-endpoint="{BASE}/api/v1/courses/7/files/14" title="Notes.pdf"></a>'
        f'<a href="https://example.com/files/99">not a Canvas course file</a>'
    )
    assert [(a.id, a.name) for a in file_links(raw, 7)] == [
        (11, "Chapter 1.pdf"), (12, "Starter.java"), (13, "ER diagram"), (14, "Notes.pdf"),
    ]


def test_file_links_lists_a_file_once_and_marks_other_courses():
    from study_coach.lms.canvas import file_links

    raw = (
        f'<a href="{BASE}/courses/7/files/11">Slides</a> and again <a href="{BASE}/courses/7/files/11">here</a>'
        f'<a href="{BASE}/courses/3/files/50">Shared rubric</a>'
    )
    links = file_links(raw, 7)
    assert [(a.id, a.name, a.course_id) for a in links] == [(11, "Slides", None), (50, "Shared rubric", 3)]


def test_file_links_handles_empty_html():
    from study_coach.lms.canvas import file_links

    assert file_links(None, 7) == [] and file_links("<p>No links</p>", 7) == []
