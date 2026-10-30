from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from study_coach.config import Blackout, ScheduleConfig
from study_coach.scheduling import Interval, StudyTask, find_free_slots, plan_to_ics, propose_blocks

TZ = ZoneInfo("America/Chicago")


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    # November 2026: the 2nd is a Monday.
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def schedule(**kw) -> ScheduleConfig:
    base = dict(study_start=time(9), study_end=time(17), max_block_minutes=90, break_minutes=15, min_block_minutes=30)
    return ScheduleConfig(**(base | kw))


def test_free_slots_respect_study_hours():
    slots = find_free_slots(dt(2, 0), dt(3, 0), [], schedule(), TZ)
    assert [(s.start, s.end) for s in slots] == [(dt(2, 9), dt(2, 17))]


def test_busy_events_split_the_day():
    busy = [Interval(start=dt(2, 11), end=dt(2, 12, 30))]
    slots = find_free_slots(dt(2, 0), dt(3, 0), busy, schedule(), TZ)
    assert [(s.start, s.end) for s in slots] == [(dt(2, 9), dt(2, 11)), (dt(2, 12, 30), dt(2, 17))]


def test_blackouts_only_apply_on_their_days():
    sched = schedule(blackouts=[Blackout(days=["tue"], start=time(13), end=time(15), label="work")])
    slots = find_free_slots(dt(2, 0), dt(4, 0), [], sched, TZ)
    monday = [s for s in slots if s.start.astimezone(TZ).day == 2]
    tuesday = [s for s in slots if s.start.astimezone(TZ).day == 3]
    assert len(monday) == 1
    assert [(s.start, s.end) for s in tuesday] == [(dt(3, 9), dt(3, 13)), (dt(3, 15), dt(3, 17))]


def test_short_gaps_are_dropped():
    busy = [Interval(start=dt(2, 9, 20), end=dt(2, 17))]
    assert find_free_slots(dt(2, 0), dt(3, 0), busy, schedule(), TZ) == []


def test_window_start_clips_today():
    slots = find_free_slots(dt(2, 14), dt(3, 0), [], schedule(), TZ)
    assert [(s.start, s.end) for s in slots] == [(dt(2, 14), dt(2, 17))]


def test_study_hours_past_midnight():
    sched = schedule(study_start=time(20), study_end=time(1))
    slots = find_free_slots(dt(2, 0), dt(3, 12), [], sched, TZ)
    assert (dt(2, 20), dt(3, 1)) in [(s.start, s.end) for s in slots]


def test_blocks_split_by_max_length_with_breaks():
    free = [Interval(start=dt(2, 9), end=dt(2, 17))]
    blocks, unscheduled = propose_blocks([StudyTask(title="Essay", minutes=150)], free, schedule())
    assert [(b.start, b.end) for b in blocks] == [(dt(2, 9), dt(2, 10, 30)), (dt(2, 10, 45), dt(2, 11, 45))]
    assert unscheduled == []


def test_earliest_deadline_goes_first():
    free = [Interval(start=dt(2, 9), end=dt(2, 17))]
    tasks = [
        StudyTask(title="Later", minutes=60, due=dt(6, 23)),
        StudyTask(title="Sooner", minutes=60, due=dt(3, 23)),
    ]
    blocks, _ = propose_blocks(tasks, free, schedule())
    assert [b.title for b in blocks] == ["Sooner", "Later"]


def test_work_after_due_date_is_reported_unscheduled():
    free = [Interval(start=dt(2, 9), end=dt(2, 10)), Interval(start=dt(4, 9), end=dt(4, 17))]
    task = StudyTask(title="Quiz prep", minutes=120, due=dt(3, 9))
    blocks, unscheduled = propose_blocks([task], free, schedule())
    assert sum(int((b.end - b.start).total_seconds() // 60) for b in blocks) == 60
    assert unscheduled == [{"title": "Quiz prep", "minutes_unscheduled": 60}]


def test_no_tiny_fragments():
    free = [Interval(start=dt(2, 9), end=dt(2, 9, 40)), Interval(start=dt(2, 13), end=dt(2, 17))]
    tasks = [StudyTask(title="A", minutes=30), StudyTask(title="B", minutes=60)]
    blocks, _ = propose_blocks(tasks, free, schedule())
    # A takes 9:00-9:30; the 10 leftover minutes are too short for B.
    assert [(b.title, b.start) for b in blocks] == [("A", dt(2, 9)), ("B", dt(2, 13))]


def test_ics_export_contains_events():
    free = [Interval(start=dt(2, 9), end=dt(2, 17))]
    blocks, _ = propose_blocks([StudyTask(title="Regression review", minutes=60)], free, schedule())
    ics = plan_to_ics(blocks)
    assert "BEGIN:VEVENT" in ics and "Study: Regression review" in ics


def test_no_free_time_with_mixed_due_dates_reports_unscheduled():
    tasks = [StudyTask(title="Undated", minutes=30), StudyTask(title="Dated", minutes=30, due=dt(3, 9))]
    blocks, unscheduled = propose_blocks(tasks, [], schedule())
    assert blocks == []
    assert [u["title"] for u in unscheduled] == ["Dated", "Undated"]


def test_undated_tasks_go_after_dated_ones_regardless_of_priority():
    free = [Interval(start=dt(2, 9), end=dt(2, 17))]
    tasks = [StudyTask(title="Urgent but undated", minutes=30, priority=1),
             StudyTask(title="Due Friday", minutes=30, due=dt(6, 23), priority=3)]
    blocks, _ = propose_blocks(tasks, free, schedule())
    assert [b.title for b in blocks] == ["Due Friday", "Urgent but undated"]


def test_dst_fall_back_night_counts_the_extra_hour():
    # US clocks fall back at 2:00 on 2026-11-01, so 22:00 -> 03:00 is 6 real hours.
    sched = schedule(study_start=time(22), study_end=time(3))
    slots = find_free_slots(datetime(2026, 10, 31, 12, tzinfo=TZ), datetime(2026, 11, 1, 12, tzinfo=TZ), [], sched, TZ)
    assert [s.minutes for s in slots] == [360]


def test_no_leftover_sliver():
    free = [Interval(start=dt(2, 9), end=dt(2, 17))]
    blocks, _ = propose_blocks([StudyTask(title="Essay", minutes=100)], free, schedule())
    assert [b.end - b.start for b in blocks] == [timedelta(minutes=70), timedelta(minutes=30)]


def test_task_shorter_than_min_block_still_scheduled():
    free = [Interval(start=dt(2, 9), end=dt(2, 17))]
    blocks, unscheduled = propose_blocks([StudyTask(title="Quick review", minutes=15)], free, schedule())
    assert len(blocks) == 1 and unscheduled == []
