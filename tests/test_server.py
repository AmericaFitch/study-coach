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


def test_secret_urls_are_not_written_to_the_log(caplog):
    import logging

    import httpx

    caplog.set_level(logging.INFO)
    secret = "https://calendar.example.com/private-abc123/basic.ics"
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    client.get(secret)
    assert "private-abc123" not in caplog.text
