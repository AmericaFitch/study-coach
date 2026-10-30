"""Loading and saving the user's personal configuration.

Everything specific to one student lives here (school URL, time zone, study
rules, course notes) so the code itself never hardcodes personal details.
"""

from __future__ import annotations

import os
from datetime import time
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, PlainSerializer, field_validator

# Saved to YAML as "17:00" rather than "17:00:00".
ClockTime = Annotated[time, PlainSerializer(lambda t: t.strftime("%H:%M"), return_type=str, when_used="json")]

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: list[Weekday] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class LmsConfig(BaseModel):
    provider: Literal["canvas"] = "canvas"
    base_url: str = ""

    @field_validator("base_url")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")


class Blackout(BaseModel):
    """A recurring weekly window when the student can't study (work, practice, ...)."""

    days: list[Weekday]
    start: ClockTime
    end: ClockTime
    label: str = "busy"


class ScheduleConfig(BaseModel):
    study_start: ClockTime = time(9, 0)
    study_end: ClockTime = time(23, 0)
    max_block_minutes: int = Field(90, ge=15)
    break_minutes: int = Field(15, ge=0)
    min_block_minutes: int = Field(30, ge=5)
    blackouts: list[Blackout] = []


class CourseProfile(BaseModel):
    id: int
    nickname: str | None = None
    difficulty: Literal["low", "medium", "high"] | None = None
    notes: str | None = None


class Config(BaseModel):
    timezone: str = "America/Chicago"
    lms: LmsConfig = LmsConfig()
    calendar_feeds: list[str] = []
    schedule: ScheduleConfig = ScheduleConfig()
    courses: list[CourseProfile] = []

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, v: str) -> str:
        ZoneInfo(v)  # raises if unknown
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def course_profile(self, course_id: int) -> CourseProfile | None:
        return next((c for c in self.courses if c.id == course_id), None)


def config_path() -> Path:
    override = os.environ.get("STUDY_COACH_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".study-coach" / "config.yaml"


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    data = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(data)


def save_config(config: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_none=True)
    # Calendar feed URLs are secrets (anyone with one can read the calendar),
    # so the file is readable only by its owner.
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def canvas_token() -> str:
    token = os.environ.get("CANVAS_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "CANVAS_TOKEN is not set. Create one in Canvas under Account -> Settings -> "
            "New Access Token, then add it to the server's env in your Claude config."
        )
    return token
