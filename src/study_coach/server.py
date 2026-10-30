"""The MCP layer: thin tool definitions over the Canvas client, calendar
feeds and scheduling logic. Everything here is read-only except the tools
that edit the user's own local config."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer

from .calendars import IcsFeedCalendar
from .config import Blackout, Config, CourseProfile, Weekday, canvas_token, load_config, save_config
from .lms.canvas import CanvasClient
from .scheduling import Interval, StudyBlock, StudyTask, find_free_slots, plan_to_ics, propose_blocks

mcp = MCPServer("study-coach")

# One connection pool for the life of the server instead of one per tool call.
_http = httpx.Client(timeout=20.0, follow_redirects=True)

# httpx logs every request URL at INFO, and the MCP server sends INFO logs to
# Claude Desktop's log file. Calendar feed URLs and file download links are
# secrets, so keep them out of it.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _config() -> Config:
    # Reloaded on every call so edits to config.yaml take effect without a restart.
    return load_config()


def _canvas(cfg: Config) -> CanvasClient:
    return CanvasClient(cfg.lms.base_url, canvas_token(), client=_http)


def _feeds(cfg: Config) -> IcsFeedCalendar:
    return IcsFeedCalendar(cfg.calendar_feeds, cfg.tz, client=_http)


def _local(dt: datetime, cfg: Config) -> datetime:
    """Treat naive times from Claude as the student's time zone; convert aware ones to it."""
    return dt.replace(tzinfo=cfg.tz) if dt.tzinfo is None else dt.astimezone(cfg.tz)


def _window(cfg: Config, days: int, start_date: str | None) -> tuple[datetime, datetime]:
    tz = cfg.tz
    if start_date:
        start = datetime.combine(date.fromisoformat(start_date), time.min, tzinfo=tz)
    else:
        start = datetime.now(tz).replace(second=0, microsecond=0)
    end = datetime.combine(start.date() + timedelta(days=days), time.min, tzinfo=tz)
    return start, end


def _busy(cfg: Config, start: datetime, end: datetime) -> list[Interval]:
    if not cfg.calendar_feeds:
        return []
    events = _feeds(cfg).events(start, end)
    return [Interval(start=e.start, end=e.end) for e in events if e.busy]


def _dump(items) -> list[dict]:
    return [i.model_dump(mode="json", exclude_none=True) for i in items]


# -- Canvas -----------------------------------------------------------------


@mcp.tool()
def list_courses() -> list[dict]:
    """Active Canvas courses with current scores, merged with the student's own
    notes about each course (nickname, difficulty, notes)."""
    cfg = _config()
    out = []
    for course in _canvas(cfg).list_courses():
        row = course.model_dump(mode="json", exclude_none=True)
        profile = cfg.course_profile(course.id)
        if profile:
            row["profile"] = profile.model_dump(exclude_none=True, exclude={"id"})
        out.append(row)
    return out


@mcp.tool()
def get_upcoming_assignments(days: int = 7) -> list[dict]:
    """Assignments due in the next `days` days across all active courses,
    soonest first, including whether each has been submitted."""
    cfg = _config()
    return _dump(_canvas(cfg).upcoming_assignments(days))


@mcp.tool()
def get_grades(course_id: int) -> list[dict]:
    """Graded assignments for one course with score, points possible and percent.
    Use this to find topics the student is weak in."""
    return _dump(_canvas(_config()).grades(course_id))


@mcp.tool()
def get_submission_feedback(course_id: int, assignment_id: int) -> dict:
    """Instructor comments and rubric scores for one of the student's submissions."""
    return _canvas(_config()).feedback(course_id, assignment_id).model_dump(mode="json")


@mcp.tool()
def get_syllabus(course_id: int) -> str:
    """The course syllabus as plain text (grade weights, policies, schedule)."""
    return _canvas(_config()).syllabus(course_id)


@mcp.tool()
def list_announcements(days: int = 14, course_id: int | None = None) -> list[dict]:
    """Recent course announcements (moved due dates, exam topics, etc.).
    Omit course_id to include every active course."""
    cfg = _config()
    canvas = _canvas(cfg)
    ids = [course_id] if course_id else [c.id for c in canvas.list_courses()]
    return _dump(canvas.announcements(ids, days))


# -- Calendar and scheduling -----------------------------------------------


@mcp.tool()
def get_calendar_events(days: int = 7, start_date: str | None = None) -> list[dict]:
    """Events from the student's calendar feeds. start_date is YYYY-MM-DD (default: now)."""
    cfg = _config()
    if not cfg.calendar_feeds:
        return []
    start, end = _window(cfg, days, start_date)
    return _dump(_feeds(cfg).events(start, end))


@mcp.tool()
def get_availability(days: int = 7, start_date: str | None = None) -> dict:
    """Free study slots after removing calendar events, blackout times (work,
    practice) and hours outside the student's preferred study window."""
    cfg = _config()
    start, end = _window(cfg, days, start_date)
    slots = find_free_slots(start, end, _busy(cfg, start, end), cfg.schedule, cfg.tz)
    return {
        "timezone": cfg.timezone,
        "total_free_minutes": sum(s.minutes for s in slots),
        "slots": [
            {"start": _local(s.start, cfg).isoformat(), "end": _local(s.end, cfg).isoformat(), "minutes": s.minutes}
            for s in slots
        ],
        "calendar_connected": bool(cfg.calendar_feeds),
    }


@mcp.tool()
def propose_study_blocks(tasks: list[StudyTask], days: int = 7, start_date: str | None = None) -> dict:
    """Fit study tasks into the student's free time, earliest deadline first.

    You decide what to study and how many minutes each task deserves (using
    grades, deadlines and the student profile); this tool does the calendar
    math. Tasks are ordered by due date; priority only breaks ties, and tasks
    without a due date go last -- give urgent work a due date.
    Returns a DRAFT -- nothing is booked. Show it to the student before
    adding it to a calendar or exporting it.
    """
    cfg = _config()
    for t in tasks:
        if t.due:
            t.due = _local(t.due, cfg)
    start, end = _window(cfg, days, start_date)
    free = find_free_slots(start, end, _busy(cfg, start, end), cfg.schedule, cfg.tz)
    blocks, unscheduled = propose_blocks(tasks, free, cfg.schedule)
    local_blocks = [b.model_copy(update={"start": _local(b.start, cfg), "end": _local(b.end, cfg)}) for b in blocks]
    return {"timezone": cfg.timezone, "blocks": _dump(local_blocks), "unscheduled": unscheduled}


@mcp.tool()
def export_plan_ics(blocks: list[StudyBlock], filename: str = "study-plan.ics") -> str:
    """Save approved study blocks as an .ics file the student can import into
    any calendar app. Returns the file path."""
    cfg = _config()
    blocks = [b.model_copy(update={"start": _local(b.start, cfg), "end": _local(b.end, cfg)}) for b in blocks]
    out_dir = Path.home() / ".study-coach" / "plans"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / Path(filename).name
    path.write_text(plan_to_ics(blocks))
    return str(path)


# -- Personalization ---------------------------------------------------------


@mcp.tool()
def get_student_profile() -> dict:
    """The student's preferences: time zone, study hours, block lengths, blackout
    times and per-course notes. Read this before planning."""
    cfg = _config()
    return cfg.model_dump(mode="json", exclude_none=True, exclude={"calendar_feeds", "lms"}) | {
        "calendar_feeds_connected": len(cfg.calendar_feeds)
    }


@mcp.tool()
def update_schedule_preferences(
    study_start: str | None = None,
    study_end: str | None = None,
    max_block_minutes: int | None = None,
    break_minutes: int | None = None,
    min_block_minutes: int | None = None,
) -> dict:
    """Change the student's study rules. Times are HH:MM (24-hour).
    Only pass the fields that should change."""
    cfg = _config()
    updates = {
        k: v
        for k, v in {
            "study_start": study_start,
            "study_end": study_end,
            "max_block_minutes": max_block_minutes,
            "break_minutes": break_minutes,
            "min_block_minutes": min_block_minutes,
        }.items()
        if v is not None
    }
    cfg.schedule = cfg.schedule.model_validate(cfg.schedule.model_dump() | updates)
    save_config(cfg)
    return cfg.schedule.model_dump(mode="json")


@mcp.tool()
def add_blackout(days: list[Weekday], start: str, end: str, label: str) -> list[dict]:
    """Add a recurring weekly time the student can't study, e.g. work on
    ["tue", "thu"] from "17:00" to "21:00". Replaces any blackout with the same label."""
    cfg = _config()
    new = Blackout(days=days, start=start, end=end, label=label)
    cfg.schedule.blackouts = [b for b in cfg.schedule.blackouts if b.label != label] + [new]
    save_config(cfg)
    return _dump(cfg.schedule.blackouts)


@mcp.tool()
def remove_blackout(label: str) -> list[dict]:
    """Remove a recurring blackout by its label."""
    cfg = _config()
    cfg.schedule.blackouts = [b for b in cfg.schedule.blackouts if b.label != label]
    save_config(cfg)
    return _dump(cfg.schedule.blackouts)


@mcp.tool()
def update_course_profile(
    course_id: int,
    nickname: str | None = None,
    difficulty: Literal["low", "medium", "high"] | None = None,
    notes: str | None = None,
) -> dict:
    """Save what the student says about a course ("Regression is my hardest
    class", "I keep losing points on citations") so future plans account for it."""
    cfg = _config()
    existing = cfg.course_profile(course_id) or CourseProfile(id=course_id)
    updated = existing.model_copy(
        update={k: v for k, v in {"nickname": nickname, "difficulty": difficulty, "notes": notes}.items() if v is not None}
    )
    cfg.courses = [c for c in cfg.courses if c.id != course_id] + [updated]
    save_config(cfg)
    return updated.model_dump(exclude_none=True)


# -- Prompts -------------------------------------------------------------------


@mcp.prompt()
def plan_my_week() -> str:
    """Build a personalized study plan for the next 7 days."""
    return (
        "Build me a study plan for the next 7 days.\n"
        "1. Call get_student_profile and list_courses to learn my preferences and how each class is going.\n"
        "2. Call get_upcoming_assignments(days=7) and list_announcements to see what's due and what changed.\n"
        "3. For courses where my score is weak or the profile says 'high' difficulty, check get_grades "
        "and recent get_submission_feedback to find specific gaps.\n"
        "4. Decide the tasks and minutes for each, weighting by due date, point value and my weak areas.\n"
        "5. Call propose_study_blocks with those tasks.\n"
        "6. Show me the draft as a day-by-day table, explaining why each block is there, and list anything "
        "that didn't fit. Don't book anything until I approve."
    )


def run() -> None:
    mcp.run()
