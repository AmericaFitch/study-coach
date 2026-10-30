"""Calendar math. Pure functions with no network calls, so they're easy to test.

Claude decides *what* to study and for how long; this module decides *when*,
because LLMs are unreliable at overlap and time-zone arithmetic.

All arithmetic happens in UTC. Python compares and subtracts datetimes that
share a tzinfo by wall-clock time, which is off by an hour across a DST change.
Callers convert results back to the student's time zone for display.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import icalendar
from pydantic import BaseModel, Field

from ..config import WEEKDAYS, ScheduleConfig

UTC = timezone.utc


class Interval(BaseModel):
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def utc(self) -> Interval:
        return Interval(start=self.start.astimezone(UTC), end=self.end.astimezone(UTC))


class StudyTask(BaseModel):
    title: str
    minutes: int = Field(gt=0)
    due: datetime | None = None
    course: str | None = None
    priority: int = Field(
        2, ge=1, le=3,
        description="1 = highest. Only breaks ties between equal due dates; undated tasks always go last.",
    )


class StudyBlock(BaseModel):
    title: str
    start: datetime
    end: datetime
    course: str | None = None


def subtract(free: list[Interval], busy: list[Interval]) -> list[Interval]:
    """Remove every busy interval from the free intervals."""
    result = list(free)
    for b in busy:
        next_result = []
        for f in result:
            if b.end <= f.start or b.start >= f.end:
                next_result.append(f)
                continue
            if b.start > f.start:
                next_result.append(Interval(start=f.start, end=b.start))
            if b.end < f.end:
                next_result.append(Interval(start=b.end, end=f.end))
        result = next_result
    return sorted(result, key=lambda i: i.start)


def find_free_slots(
    window_start: datetime,
    window_end: datetime,
    busy: list[Interval],
    schedule: ScheduleConfig,
    tz: ZoneInfo,
) -> list[Interval]:
    """Free study time inside the window, after study hours, blackouts and busy events.

    Returned intervals are in UTC.
    """
    local_start = window_start.astimezone(tz)
    local_end = window_end.astimezone(tz)

    study: list[Interval] = []
    blackouts: list[Interval] = []
    day = local_start.date() - timedelta(days=1)  # catch study hours that cross midnight
    while day <= local_end.date():
        # Build each window from local wall-clock times, then convert to UTC.
        start = datetime.combine(day, schedule.study_start, tzinfo=tz)
        end = datetime.combine(day, schedule.study_end, tzinfo=tz)
        if end <= start:  # e.g. 18:00 -> 01:00
            end = datetime.combine(day + timedelta(days=1), schedule.study_end, tzinfo=tz)
        study.append(Interval(start=start, end=end).utc())

        weekday = WEEKDAYS[day.weekday()]
        for bo in schedule.blackouts:
            if weekday in bo.days:
                bo_start = datetime.combine(day, bo.start, tzinfo=tz)
                bo_end = datetime.combine(day, bo.end, tzinfo=tz)
                if bo_end <= bo_start:
                    bo_end = datetime.combine(day + timedelta(days=1), bo.end, tzinfo=tz)
                blackouts.append(Interval(start=bo_start, end=bo_end).utc())
        day += timedelta(days=1)

    w_start, w_end = window_start.astimezone(UTC), window_end.astimezone(UTC)
    clipped = [
        Interval(start=max(s.start, w_start), end=min(s.end, w_end))
        for s in study
        if s.end > w_start and s.start < w_end
    ]
    free = subtract(clipped, blackouts + [b.utc() for b in busy])
    return [f for f in free if f.minutes >= schedule.min_block_minutes]


def propose_blocks(
    tasks: list[StudyTask],
    free: list[Interval],
    schedule: ScheduleConfig,
) -> tuple[list[StudyBlock], list[dict]]:
    """Fit tasks into free slots, earliest deadline first.

    Returns (blocks, unscheduled), where unscheduled lists tasks that didn't
    fully fit before their due date along with the minutes left over.
    Blocks are in UTC.
    """
    ordered = sorted(
        tasks,
        key=lambda t: (t.due is None, t.due.astimezone(UTC) if t.due else datetime.min.replace(tzinfo=UTC), t.priority),
    )
    slots = [s.utc() for s in free]
    max_block = timedelta(minutes=schedule.max_block_minutes)
    min_block = timedelta(minutes=schedule.min_block_minutes)
    gap = timedelta(minutes=schedule.break_minutes)

    blocks: list[StudyBlock] = []
    unscheduled: list[dict] = []
    for task in ordered:
        due = task.due.astimezone(UTC) if task.due else None
        remaining = timedelta(minutes=task.minutes)
        for slot in slots:
            # A long slot can hold several blocks of the same task, separated by breaks.
            while remaining > timedelta(0):
                limit = slot.end if due is None else min(slot.end, due)
                length = min(remaining, max_block, limit - slot.start)
                # Don't leave a sliver (e.g. 90 + 10 min); split as 70 + 30 instead.
                leftover = remaining - length
                if timedelta(0) < leftover < min_block and remaining - min_block >= min_block:
                    length = remaining - min_block
                # Don't create tiny fragments unless that's all the task needs.
                if length <= timedelta(0) or (length < min_block and length < remaining):
                    break
                block_end = slot.start + length
                blocks.append(StudyBlock(title=task.title, start=slot.start, end=block_end, course=task.course))
                remaining -= length
                slot.start = min(block_end + gap, slot.end)
            if remaining <= timedelta(0):
                break
        if remaining > timedelta(0):
            unscheduled.append(
                {"title": task.title, "minutes_unscheduled": int(remaining.total_seconds() // 60)}
            )
        slots = [s for s in slots if s.end - s.start >= min_block]

    return sorted(blocks, key=lambda b: b.start), unscheduled


def plan_to_ics(blocks: list[StudyBlock]) -> str:
    """An .ics file the user can import into any calendar app."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//study-coach//EN")
    cal.add("version", "2.0")
    stamp = datetime.now(UTC)
    for i, b in enumerate(blocks):
        ev = icalendar.Event()
        ev.add("uid", f"study-coach-{b.start.isoformat()}-{i}@study-coach")
        ev.add("dtstamp", stamp)
        ev.add("summary", f"Study: {b.title}")
        ev.add("dtstart", b.start)
        ev.add("dtend", b.end)
        if b.course:
            ev.add("description", b.course)
        cal.add_component(ev)
    return cal.to_ical().decode()
