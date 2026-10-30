"""Read calendars from private iCal (ICS) feed URLs.

Google Calendar, Outlook, Apple Calendar and Canvas all publish these, so
reading availability needs no OAuth -- the user just pastes a URL.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events
from pydantic import BaseModel


class CalendarEvent(BaseModel):
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    busy: bool = True
    source: str | None = None


class CalendarProvider(Protocol):
    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]: ...


def _to_datetime(value: date | datetime, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def parse_ics(text: str, start: datetime, end: datetime, tz: ZoneInfo, source: str | None = None) -> list[CalendarEvent]:
    cal = icalendar.Calendar.from_ical(text)
    events = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        dtstart = comp.get("DTSTART").dt
        dtend_prop = comp.get("DTEND")
        dtend = dtend_prop.dt if dtend_prop else dtstart
        all_day = not isinstance(dtstart, datetime)
        transparent = str(comp.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT"
        events.append(
            CalendarEvent(
                title=str(comp.get("SUMMARY", "(no title)")),
                start=_to_datetime(dtstart, tz),
                end=_to_datetime(dtend, tz),
                all_day=all_day,
                # All-day events (holidays, "Exam week") are context, not blocked time.
                busy=not all_day and not transparent,
                source=source,
            )
        )
    return sorted(events, key=lambda e: e.start)


class IcsFeedCalendar:
    def __init__(self, urls: list[str], tz: ZoneInfo, client: httpx.Client | None = None):
        self.urls = urls
        self.tz = tz
        self._http = client or httpx.Client(timeout=20.0, follow_redirects=True)

    def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for i, url in enumerate(self.urls):
            # webcal:// is how many apps share feeds; it's plain https underneath.
            fetch_url = "https://" + url[len("webcal://"):] if url.startswith("webcal://") else url
            resp = self._http.get(fetch_url)
            resp.raise_for_status()
            events.extend(parse_ics(resp.text, start, end, self.tz, source=f"feed {i + 1}"))
        return sorted(events, key=lambda e: e.start)
