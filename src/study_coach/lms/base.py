"""The LMS interface. Canvas is the only implementation today; Moodle or
Blackboard would be a new module implementing the same protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class Course(BaseModel):
    id: int
    name: str
    code: str | None = None
    current_score: float | None = None
    current_grade: str | None = None


class Assignment(BaseModel):
    id: int
    course_id: int
    name: str
    due_at: datetime | None = None
    points_possible: float | None = None
    submitted: bool = False
    description: str | None = None
    url: str | None = None


class GradedItem(BaseModel):
    assignment_id: int
    course_id: int
    name: str
    score: float | None
    points_possible: float | None
    percent: float | None
    graded_at: datetime | None = None


class Feedback(BaseModel):
    assignment_id: int
    course_id: int
    score: float | None
    comments: list[str]
    rubric: list[dict]


class Announcement(BaseModel):
    course_id: int
    title: str
    posted_at: datetime | None
    message: str


class CourseFile(BaseModel):
    """A file the course links from a module (Canvas hides the Files tab in some classes)."""

    id: int
    course_id: int
    name: str
    module: str


class DownloadedFile(BaseModel):
    id: int
    name: str
    content_type: str
    data: bytes


class LmsProvider(Protocol):
    def list_courses(self) -> list[Course]: ...

    def upcoming_assignments(self, days: int) -> list[Assignment]: ...

    def grades(self, course_id: int) -> list[GradedItem]: ...

    def feedback(self, course_id: int, assignment_id: int) -> Feedback: ...

    def syllabus(self, course_id: int) -> str: ...

    def announcements(self, course_ids: list[int], days: int) -> list[Announcement]: ...

    def module_files(self, course_id: int) -> list[CourseFile]: ...

    def download_file(self, course_id: int, file_id: int) -> DownloadedFile: ...
