"""The MCP layer: thin tool definitions over the Canvas client, calendar
feeds and scheduling logic. Everything here is read-only except the tools
that edit the user's own local config."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

import httpx
from mcp.server.mcpserver import Image, MCPServer

from .calendars import IcsFeedCalendar
from .documents import page_count, page_texts, parse_pages, render_pages
from .config import Blackout, Config, CourseProfile, Weekday, canvas_token, load_config, save_config
from .lms.base import DownloadedFile
from .lms.canvas import CanvasClient
from .scheduling import Interval, StudyBlock, StudyTask, find_free_slots, plan_to_ics, propose_blocks

mcp = MCPServer("study-coach")

# One connection pool for the life of the server instead of one per tool call.
_http = httpx.Client(timeout=20.0, follow_redirects=True)

# httpx logs every request URL at INFO, and the MCP server sends INFO logs to
# Claude Desktop's log file. Calendar feed URLs and file download links are
# secrets, so keep them out of it.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Recent downloads, so reading a PDF and then viewing its pages fetches it once.
_files: dict[tuple[int, int], DownloadedFile] = {}
_FILE_CACHE_SIZE = 3

# Keeps one read_pdf result small enough to leave room in Claude's context.
PDF_TEXT_BUDGET = 40_000
MAX_PAGES_PER_VIEW = 5
# Fewer characters than this on a page usually means a diagram or a scan.
LITTLE_TEXT = 40


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


def _pdf(course_id: int, file_id: int) -> DownloadedFile:
    key = (course_id, file_id)
    if key not in _files:
        # download_file refuses non-PDFs (videos included) before downloading anything.
        f = _canvas(_config()).download_file(course_id, file_id)
        if len(_files) >= _FILE_CACHE_SIZE:
            _files.pop(next(iter(_files)))  # dicts keep insertion order, so this is the oldest
        _files[key] = f
    return _files[key]


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
def get_upcoming_assignments(days: int = 7, include_past: bool = False) -> list[dict]:
    """Assignments due in the next `days` days across all active courses,
    soonest first, including whether each has been submitted. `attachments`
    lists files the instructions link to (handouts, starter code); open a PDF
    one with read_pdf, using its course_id if one is given.

    Set include_past=True to also get assignments whose due date has passed
    (this term's earlier work), e.g. when the student asks about "Assignment 5"
    or wants to review graded work."""
    cfg = _config()
    return _dump(_canvas(cfg).upcoming_assignments(days, include_past))


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
def get_syllabus(course_id: int) -> dict:
    """The course syllabus as plain text (grade weights, policies, schedule),
    plus `attachments`: files it links to, readable with read_pdf."""
    return _canvas(_config()).syllabus(course_id).model_dump(mode="json", exclude_none=True)


@mcp.tool()
def list_announcements(days: int = 14, course_id: int | None = None) -> list[dict]:
    """Recent course announcements (moved due dates, exam topics, etc.), with
    any files they attach or link. Omit course_id to include every active course."""
    cfg = _config()
    canvas = _canvas(cfg)
    ids = [course_id] if course_id else [c.id for c in canvas.list_courses()]
    return _dump(canvas.announcements(ids, days))


# -- Course pages and files ----------------------------------------------------


@mcp.tool()
def list_course_pages(course_id: int) -> list[dict]:
    """Pages a course links from its Modules (lecture notes, instructions,
    reading lists). Open one with get_course_page."""
    return _dump(_canvas(_config()).course_pages(course_id))


@mcp.tool()
def get_course_page(course_id: int, page_url: str) -> dict:
    """One course page as plain text, plus `attachments`: files it links to."""
    return _canvas(_config()).page(course_id, page_url).model_dump(mode="json", exclude_none=True)


@mcp.tool()
def list_course_files(course_id: int) -> list[dict]:
    """Every file a course links (slides, readings, handouts, starter code),
    from Modules, the syllabus, assignments (past and future), pages and
    announcements. Each file appears once; `sources` says where it's linked,
    e.g. "Assignment: Assignment 6". Open PDFs with read_pdf or
    view_pdf_pages, using the file's own course_id. Can take several seconds
    for a course with many pages."""
    return _dump(_canvas(_config()).course_files(course_id))


@mcp.tool()
def read_pdf(course_id: int, file_id: int, pages: int | str | None = None) -> dict:
    """Text of a course PDF, page by page. `pages` is like 3 or "1-5,9" (default:
    all pages, stopping early if the text gets long; continue from `next_page`).

    Pages listed in `pages_with_little_text` are probably diagrams or scans:
    call view_pdf_pages on them to see what's actually there.
    """
    f = _pdf(course_id, file_id)
    texts = page_texts(f.data)
    wanted = parse_pages(pages, len(texts))
    out, used = [], 0
    for number in wanted:
        text = texts[number - 1]
        if out and used + len(text) > PDF_TEXT_BUDGET:
            break
        out.append({"page": number, "text": text})
        used += len(text)
    result = {
        "name": f.name,
        "page_count": len(texts),
        "pages": out,
        "pages_with_little_text": [p["page"] for p in out if len(p["text"]) < LITTLE_TEXT],
    }
    if len(out) < len(wanted):
        result["next_page"] = wanted[len(out)]
    return result


@mcp.tool()
def view_pdf_pages(course_id: int, file_id: int, pages: int | str) -> list[str | Image]:
    """Images of up to 5 pages of a course PDF, like 3 or "4-6". Use for
    diagrams, charts, worked examples and scanned pages that read_pdf can't
    turn into text."""
    f = _pdf(course_id, file_id)
    wanted = parse_pages(pages, page_count(f.data))
    if len(wanted) > MAX_PAGES_PER_VIEW:
        raise ValueError(f"Ask for at most {MAX_PAGES_PER_VIEW} pages at a time.")
    out: list[str | Image] = []
    for number, png in zip(wanted, render_pages(f.data, wanted)):
        out += [f"{f.name}, page {number}:", Image(data=png, format="png")]
    return out


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
