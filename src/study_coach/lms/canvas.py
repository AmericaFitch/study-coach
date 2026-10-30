"""Read-only Canvas LMS client.

Canvas paginates list endpoints and puts the next page's URL in the HTTP
`Link` header; `_get_all` follows it so no results are silently dropped.
"""

from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import httpx

from .base import (
    Announcement,
    Assignment,
    Attachment,
    Course,
    CourseFile,
    CoursePage,
    DownloadedFile,
    Feedback,
    GradedItem,
    Syllabus,
)

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


def _skip_hidden(fetch, default):
    """Run fetch(), returning default if Canvas hides (403) or has turned off
    (404) that part of the course. The student can't see it either."""
    try:
        return fetch()
    except CanvasAccessDenied:
        return default
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return default
        raise


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CanvasAccessDenied(RuntimeError):
    """403: this part of the course is hidden from the student."""


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
            raise CanvasAccessDenied(f"Canvas denied access to {resp.request.url.path} (403).")
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
                        attachments=file_links(a.get("description"), course.id) or None,
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

    def syllabus(self, course_id: int) -> Syllabus:
        body = self._get(f"/courses/{course_id}", [("include[]", "syllabus_body")]).get("syllabus_body")
        return Syllabus(
            text=html_to_text(body) or "No syllabus posted in Canvas.",
            attachments=file_links(body, course_id) or None,
        )

    def announcements(self, course_ids: list[int], days: int = 14) -> list[Announcement]:
        if not course_ids:
            return []
        now = datetime.now(timezone.utc)
        params = [("context_codes[]", f"course_{cid}") for cid in course_ids]
        params.append(("start_date", (now - timedelta(days=days)).date().isoformat()))
        # Without end_date Canvas stops 28 days after start_date, hiding recent posts.
        params.append(("end_date", (now + timedelta(days=1)).date().isoformat()))
        raw = self._get_all("/announcements", params)
        results = []
        for a in raw:
            course_id = int(a.get("context_code", "course_0").split("_")[-1])
            links = file_links(a.get("message"), course_id)
            # Files attached to the post itself, not linked in its text.
            linked = {f.id for f in links}
            links += [
                Attachment(id=f["id"], name=f.get("display_name") or f.get("filename") or f"file {f['id']}")
                for f in a.get("attachments") or []
                if f.get("id") and f["id"] not in linked
            ]
            results.append(
                Announcement(
                    course_id=course_id,
                    title=a.get("title", ""),
                    posted_at=_parse_dt(a.get("posted_at")),
                    message=html_to_text(a.get("message"), limit=1000),
                    attachments=links or None,
                )
            )
        return results

    def _module_items(self, course_id: int) -> list[tuple[str, dict]]:
        """(module name, item) for every module item, in course order."""
        out = []
        for m in self._get_all(f"/courses/{course_id}/modules", [("include[]", "items")]):
            # Canvas leaves out "items" when a module is too large to inline.
            items = m.get("items")
            if items is None:
                items = self._get_all(f"/courses/{course_id}/modules/{m['id']}/items")
            out += [(m.get("name", ""), item) for item in items]
        return out

    def course_pages(self, course_id: int) -> list[CoursePage]:
        # Read through Modules: the Pages list is turned off (404) in many classes.
        pages, seen = [], set()
        for module, item in self._module_items(course_id):
            if item.get("type") == "Page" and item.get("page_url") and item["page_url"] not in seen:
                seen.add(item["page_url"])
                pages.append(
                    CoursePage(course_id=course_id, page_url=item["page_url"], title=item.get("title", ""), module=module)
                )
        return pages

    def page(self, course_id: int, page_url: str) -> CoursePage:
        p = self._get(f"/courses/{course_id}/pages/{page_url}")
        title = p.get("title", page_url)
        if p.get("locked_for_user"):
            reason = html_to_text(p.get("lock_explanation")) or "the instructor hasn't made it available yet"
            return CoursePage(course_id=course_id, page_url=page_url, title=title, text=f"Locked: {reason}")
        return CoursePage(
            course_id=course_id,
            page_url=page_url,
            title=title,
            text=html_to_text(p.get("body")),
            attachments=file_links(p.get("body"), course_id) or None,
        )

    def course_files(self, course_id: int) -> list[CourseFile]:
        """Every file the course links, from Modules, the syllabus, assignments
        (past and future), pages and announcements, each listed once.

        The Files tab is blocked in many classes, and most files are only
        linked from inside other content, so all of it has to be read.
        """
        found: dict[int, CourseFile] = {}

        def add(file_id: int, name: str, source: str, link_course: int | None = None) -> None:
            f = found.get(file_id)
            if f is None:
                found[file_id] = CourseFile(
                    id=file_id, course_id=link_course or course_id, name=name, sources=[source]
                )
            elif source not in f.sources:
                f.sources.append(source)

        def add_links(links: list[Attachment], source: str) -> None:
            for a in links:
                add(a.id, a.name, source, a.course_id)

        # Modules come first because their item titles are the best file names.
        modules = _skip_hidden(lambda: self._module_items(course_id), [])
        for module, item in modules:
            if item.get("type") == "File" and item.get("content_id"):
                add(item["content_id"], item.get("title", ""), f"Module: {module}")
        add_links(_skip_hidden(lambda: self.syllabus(course_id).attachments, None) or [], "Syllabus")
        for a in _skip_hidden(lambda: self._get_all(f"/courses/{course_id}/assignments"), []):
            add_links(file_links(a.get("description"), course_id), f"Assignment: {a.get('name', '')}")
        page_urls = list(dict.fromkeys(
            i["page_url"] for _, i in modules if i.get("type") == "Page" and i.get("page_url")
        ))
        # One request per page; courses with 30+ pages took over 10 s one at a time.
        # map() keeps course order, and httpx clients are safe to share across threads.
        with ThreadPoolExecutor(max_workers=8) as pool:
            pages = pool.map(lambda url: _skip_hidden(lambda: self.page(course_id, url), None), page_urls)
            for page in list(pages):
                if page:
                    add_links(page.attachments or [], f"Page: {page.title}")
        # About a school year back, so last term's posts in a long course still count.
        for post in _skip_hidden(lambda: self.announcements([course_id], days=365), []):
            add_links(post.attachments or [], f"Announcement: {post.title}")
        return list(found.values())

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
