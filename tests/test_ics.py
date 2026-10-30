from datetime import datetime
from zoneinfo import ZoneInfo

from study_coach.calendars.ics import parse_ics

TZ = ZoneInfo("America/Chicago")

FEED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:lecture
SUMMARY:Regression lecture
DTSTART;TZID=America/Chicago:20261102T100000
DURATION:PT1H15M
RRULE:FREQ=WEEKLY;BYDAY=MO,WE
END:VEVENT
BEGIN:VEVENT
UID:cancelled
SUMMARY:Cancelled meeting
STATUS:CANCELLED
DTSTART;TZID=America/Chicago:20261102T140000
DTEND;TZID=America/Chicago:20261102T150000
END:VEVENT
BEGIN:VEVENT
UID:holiday
SUMMARY:No classes
DTSTART;VALUE=DATE:20261103
DTEND;VALUE=DATE:20261104
END:VEVENT
BEGIN:VEVENT
UID:free
SUMMARY:Optional review session
TRANSP:TRANSPARENT
DTSTART;TZID=America/Chicago:20261103T180000
DTEND;TZID=America/Chicago:20261103T190000
END:VEVENT
END:VCALENDAR"""


def events():
    return parse_ics(FEED, datetime(2026, 11, 2, tzinfo=TZ), datetime(2026, 11, 5, tzinfo=TZ), TZ)


def test_recurring_events_expand_with_duration():
    lectures = [e for e in events() if e.title == "Regression lecture"]
    assert [e.start.day for e in lectures] == [2, 4]
    assert all((e.end - e.start).seconds == 75 * 60 for e in lectures)
    assert all(e.busy for e in lectures)


def test_cancelled_events_are_skipped():
    assert "Cancelled meeting" not in [e.title for e in events()]


def test_all_day_and_transparent_events_are_not_busy():
    by_title = {e.title: e for e in events()}
    assert by_title["No classes"].all_day and not by_title["No classes"].busy
    assert not by_title["Optional review session"].busy
