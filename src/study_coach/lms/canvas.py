"""Read-only Canvas LMS client.

Canvas paginates list endpoints and puts the next page's URL in the HTTP
`Link` header; `_get_all` follows it so no results are silently dropped.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import httpx

from .base import Announcement, Assignment, Attachment, Course, CourseFile, DownloadedFile, Feedback, GradedItem

# Only documents Study Coach can read are ever downloaded; videos, audio and
# everything else are refused from their metadata, before any bytes move.
DOWNLOADABLE_TYPES = {"application/pdf"}
# Course PDFs are usually a few MB; this guards against an unusually huge one.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n\s*\n+")


def html_to_text(raw: str | None, limit: int | None = None) -> str:
    """Strip HTML so Claude gets readable text instead of markup."""
    if not raw:
        return ""
    text = re.sub(r"<(br|/p|/li|/h\d)\s*/?>", "\n", raw, flags=re.I)
    text = html.unescape(_TAG_RE.sub("", text))
    text = _WS_RE.sub("\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + " ..."
    return text


# Matches /courses/1/files/2 and /api/v1/courses/1/files/2, with or without
# the host and with any suffix (/download, /preview, ?verifier=...).
_FILE_URL_RE = re.compile(r"/(?:api/v1/)?courses/(\d+)/files/(\d+)")


class _FileLinkParser(HTMLParser):
    """Collects (course id, file id, name) from <a> and <img> tags."""

    def __init__(self):
        super().__init__()  # convert_charrefs=True decodes &amp; in text and attributes
        self.links: list[tuple[int, int, str]] = []
        self._open: list | None = None  # the <a> being read: [course, file, title, text parts]

    @staticmethod
    def _match(attrs: dict) -> tuple[int, int] | None:
        # Canvas puts the API URL in data-api-endpoint and a page URL in href;
        # either one is enough.
        for key in ("data-api-endpoint", "href", "src"):
            m = _FILE_URL_RE.search(attrs.get(key) or "")
            if m:
                return int(m.group(1)), int(m.group(2))
        return None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        found = self._match(attrs)
        if tag == "a" and found:
            self._open = [*found, attrs.get("title") or "", []]
        elif tag == "img" and found:
            self.links.append((*found, attrs.get("alt") or attrs.get("title") or ""))

    def handle_data(self, data):
        if self._open:
            self._open[3].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            course, file, title, text = self._open
            self.links.append((course, file, " ".join("".join(text).split()) or title))
            self._open = None


def file_links(raw: str | None, course_id: int) -> list[Attachment]:
    """Canvas files linked from HTML, in order, once each.

    Must run on the original HTML: html_to_text keeps a link's words but drops
    where it points, which is how course files went missing before.
    """
    if not raw:
        return []
    parser = _FileLinkParser()
    parser.feed(raw)
    parser.close()
    seen: dict[int, Attachment] = {}
    for link_course, file_id, name in parser.links:
        if file_id not in seen:
            seen[file_id] = Attachment(
                id=file_id,
                name=name or f"file {file_id}",
                course_id=link_course if link_course != course_id else None,
            )
    return list(seen.values())


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CanvasClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client | None = None):
        if not base_url:
            raise ValueError("Canvas base_url is not configured. Run `study-coach init`.")
        self.base_url = base_url.rstrip("/")
        self._http = client or httpx.Client(timeout=20.0)
        self._headers = {"Authorization": f"Bearer {token}"}

    # -- HTTP helpers -------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code == 401:
            raise RuntimeError(
                "Canvas rejected the token (401). It may be expired or revoked -- create a new one "
                "under Account -> Settings -> New Access Token and update CANVAS_TOKEN."
            )
        if resp.status_code == 403:
            raise RuntimeError(f"Canvas denied access to {resp.request.url.path} (403).")
        resp.raise_for_status()

    def _get(self, path: str, params: dict | list | None = None) -> dict:
        resp = self._http.get(self._url(path), params=params, headers=self._headers)
        self._check(resp)
        return resp.json()

    def _get_all(self, path: str, params: list[tuple[str, str]] | None = None) -> list[dict]:
        params = list(params or []) + [("per_page", "100")]
        resp = self._http.get(self._url(path), params=params, headers=self._headers)
        self._check(resp)
        items = list(resp.json())
        while "next" in resp.links:
            resp = self._http.get(resp.links["next"]["url"], headers=self._headers)
            self._check(resp)
            items.extend(resp.json())
        return items

    # -- LmsProvider --------------------------------------------------------

    def list_courses(self) -> list[Course]:
        raw = self._get_all(
            "/courses",
            # state[]=available drops past-term courses that were never formally concluded.
            [("enrollment_state", "active"), ("state[]", "available"), ("include[]", "total_scores")],
        )
        courses = []
        for c in raw:
            if "name" not in c:  # restricted/concluded courses come back as stubs
                continue
            enrollment = next(iter(c.get("enrollments") or []), {})
            courses.append(
                Course(
                    id=c["id"],
                    name=c["name"],
                    code=c.get("course_code"),
                    current_score=enrollment.get("computed_current_score"),
                    current_grade=enrollment.get("computed_current_grade"),
                )
            )
        return courses

    def upcoming_assignments(self, days: int = 7) -> list[Assignment]:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=days)
        results = []
        for course in self.list_courses():
            raw = self._get_all(
                f"/courses/{course.id}/assignments",
                [("bucket", "future"), ("order_by", "due_at"), ("include[]", "submission")],
            )
            for a in raw:
                due = _parse_dt(a.get("due_at"))
                if due is None or not (now <= due <= cutoff):
                    continue
                submission = a.get("submission") or {}
                results.append(
                    Assignment(
                        id=a["id"],
                        course_id=course.id,
                        name=a["name"],
                        due_at=due,
                        points_possible=a.get("points_possible"),
                        submitted=submission.get("workflow_state") in {"submitted", "graded"},
                        description=html_to_text(a.get("description"), limit=600),
                        url=a.get("html_url"),
                    )
                )
        return sorted(results, key=lambda a: a.due_at)

    def grades(self, course_id: int) -> list[GradedItem]:
        raw = self._get_all(
            f"/courses/{course_id}/assignments", [("include[]", "submission")]
        )
        items = []
        for a in raw:
            sub = a.get("submission") or {}
            score = sub.get("score")
            if score is None:
                continue
            possible = a.get("points_possible")
            items.append(
                GradedItem(
                    assignment_id=a["id"],
                    course_id=course_id,
                    name=a["name"],
                    score=score,
                    points_possible=possible,
                    percent=round(100 * score / possible, 1) if possible else None,
                    graded_at=_parse_dt(sub.get("graded_at")),
                )
            )
        return sorted(items, key=lambda g: g.graded_at or datetime.min.replace(tzinfo=timezone.utc))

    def feedback(self, course_id: int, assignment_id: int) -> Feedback:
        sub = self._get(
            f"/courses/{course_id}/assignments/{assignment_id}/submissions/self",
            [("include[]", "submission_comments"), ("include[]", "rubric_assessment")],
        )
        comments = [
            c.get("comment", "") for c in sub.get("submission_comments") or [] if c.get("comment")
        ]
        rubric = [
            {"criterion": k, "points": v.get("points"), "comment": v.get("comments") or None}
            for k, v in (sub.get("rubric_assessment") or {}).items()
        ]
        return Feedback(
            assignment_id=assignment_id,
            course_id=course_id,
            score=sub.get("score"),
            comments=comments,
            rubric=rubric,
        )

    def syllabus(self, course_id: int) -> str:
        course = self._get(f"/courses/{course_id}", [("include[]", "syllabus_body")])
        return html_to_text(course.get("syllabus_body")) or "No syllabus posted in Canvas."

    def announcements(self, course_ids: list[int], days: int = 14) -> list[Announcement]:
        if not course_ids:
            return []
        now = datetime.now(timezone.utc)
        params = [("context_codes[]", f"course_{cid}") for cid in course_ids]
        params.append(("start_date", (now - timedelta(days=days)).date().isoformat()))
        # Without end_date Canvas stops 28 days after start_date, hiding recent posts.
        params.append(("end_date", (now + timedelta(days=1)).date().isoformat()))
        raw = self._get_all("/announcements", params)
        return [
            Announcement(
                course_id=int(a.get("context_code", "course_0").split("_")[-1]),
                title=a.get("title", ""),
                posted_at=_parse_dt(a.get("posted_at")),
                message=html_to_text(a.get("message"), limit=1000),
            )
            for a in raw
        ]

    def module_files(self, course_id: int) -> list[CourseFile]:
        # Files are listed through Modules because many classes block the Files tab (403).
        modules = self._get_all(f"/courses/{course_id}/modules", [("include[]", "items")])
        files = []
        for m in modules:
            # Canvas leaves out "items" when a module is too large to inline.
            items = m.get("items")
            if items is None:
                items = self._get_all(f"/courses/{course_id}/modules/{m['id']}/items")
            for item in items:
                if item.get("type") == "File" and item.get("content_id"):
                    files.append(
                        CourseFile(
                            id=item["content_id"],
                            course_id=course_id,
                            name=item.get("title", ""),
                            module=m.get("name", ""),
                        )
                    )
        return files

    def download_file(self, course_id: int, file_id: int) -> DownloadedFile:
        meta = self._get(f"/courses/{course_id}/files/{file_id}")
        name = meta.get("display_name") or meta.get("filename") or str(file_id)
        if meta.get("locked_for_user") or not meta.get("url"):
            reason = meta.get("lock_explanation") or "the instructor hasn't made it available yet"
            raise RuntimeError(f"{name} is locked: {html_to_text(reason)}")
        content_type = meta.get("content-type", "")
        if content_type not in DOWNLOADABLE_TYPES:
            raise ValueError(f"{name} isn't a PDF ({content_type or 'unknown type'}), so it wasn't downloaded.")
        if (meta.get("size") or 0) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(f"{name} is too large to read ({meta['size'] // 2**20} MB).")
        # The download link redirects to Canvas's file storage on another host;
        # httpx drops the Authorization header on that cross-host hop.
        with self._http.stream("GET", meta["url"], headers=self._headers, follow_redirects=True) as resp:
            self._check(resp)
            chunks, total = [], 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(f"{name} is too large to read (over 50 MB).")
                chunks.append(chunk)
        return DownloadedFile(
            id=file_id, name=name, content_type=content_type, data=b"".join(chunks)
        )
