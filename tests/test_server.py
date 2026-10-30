"""Call the MCP tool functions directly with a temporary config."""

import pytest

from study_coach import server


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDY_COACH_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)


def test_preferences_persist_and_shape_availability():
    server.update_schedule_preferences(study_start="10:00", study_end="12:00")
    # Leaves a 20-minute gap at 10:00, shorter than the 30-minute minimum block.
    server.add_blackout(days=["mon"], start="10:20", end="11:00", label="club")
    result = server.get_availability(days=1, start_date="2026-11-02")  # a Monday
    assert [s["start"][11:16] for s in result["slots"]] == ["11:00"]
    assert result["slots"][0]["start"].endswith("-06:00")  # shown in local time, not UTC


def test_propose_blocks_returns_local_times_and_accepts_naive_due():
    server.update_schedule_preferences(study_start="09:00", study_end="12:00")
    tasks = [server.StudyTask(title="HW", minutes=60, due="2026-11-02T23:59")]
    result = server.propose_study_blocks(tasks=tasks, days=1, start_date="2026-11-02")
    assert result["blocks"][0]["start"] == "2026-11-02T09:00:00-06:00"


def test_course_profile_shows_in_student_profile():
    server.update_course_profile(course_id=42, difficulty="high", notes="interaction terms")
    profile = server.get_student_profile()
    assert profile["courses"] == [{"id": 42, "difficulty": "high", "notes": "interaction terms"}]


def test_canvas_tools_explain_missing_token():
    with pytest.raises(RuntimeError, match="CANVAS_TOKEN"):
        server.list_courses()


@pytest.fixture
def course_pdf(monkeypatch):
    """Serve a fake Canvas file to the PDF tools, counting downloads."""
    from pdf_samples import text_pdf

    from study_coach.lms.base import DownloadedFile

    calls = []

    class FakeCanvas:
        def download_file(self, course_id, file_id):
            calls.append(file_id)
            pages = [f"Page {n} about joins" for n in range(1, 4)]
            if file_id == 2:
                raise ValueError("Syllabus.docx isn't a PDF (application/msword), so it wasn't downloaded.")
            return DownloadedFile(id=file_id, name="Joins.pdf", content_type="application/pdf", data=text_pdf(*pages))

    monkeypatch.setattr(server, "_canvas", lambda cfg: FakeCanvas())
    monkeypatch.setattr(server, "_files", {})
    return calls


def test_read_pdf_returns_requested_pages(course_pdf):
    result = server.read_pdf(course_id=7, file_id=1, pages="2-3")
    assert result["page_count"] == 3
    assert [p["text"] for p in result["pages"]] == ["Page 2 about joins", "Page 3 about joins"]
    assert result["pages_with_little_text"] == [2, 3]


def test_long_pdf_text_stops_early_and_says_where_to_continue(course_pdf, monkeypatch):
    monkeypatch.setattr(server, "PDF_TEXT_BUDGET", 30)
    result = server.read_pdf(course_id=7, file_id=1)
    assert [p["page"] for p in result["pages"]] == [1]
    assert result["next_page"] == 2


def test_viewing_pages_after_reading_reuses_the_download(course_pdf):
    server.read_pdf(course_id=7, file_id=1)
    content = server.view_pdf_pages(course_id=7, file_id=1, pages="1")
    assert content[0] == "Joins.pdf, page 1:"
    assert isinstance(content[1], server.Image)
    assert course_pdf == [1]


def test_view_pdf_pages_limits_pages_per_call(course_pdf, monkeypatch):
    monkeypatch.setattr(server, "MAX_PAGES_PER_VIEW", 2)
    with pytest.raises(ValueError, match="at most 2"):
        server.view_pdf_pages(course_id=7, file_id=1, pages="1-3")


def test_non_pdf_file_is_rejected_by_name(course_pdf):
    with pytest.raises(ValueError, match="Syllabus.docx isn't a PDF"):
        server.read_pdf(course_id=7, file_id=2)


def test_secret_urls_are_not_written_to_the_log(caplog):
    import logging

    import httpx

    caplog.set_level(logging.INFO)
    secret = "https://calendar.example.com/private-abc123/basic.ics"
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    client.get(secret)
    assert "private-abc123" not in caplog.text
