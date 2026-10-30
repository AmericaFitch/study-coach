"""Read course PDFs: extract text per page, or render pages as images.

Text is cheap and searchable, so it comes first. Slides that are mostly
diagrams, and scanned handouts, have little or no text layer; those pages are
rendered as PNGs so Claude can look at them instead.

Uses pypdfium2 (the PDF engine inside Chrome), which ships prebuilt wheels for
macOS, Windows and Linux, so nothing extra needs installing.
"""

from __future__ import annotations

import io

import pypdfium2 as pdfium

# Claude downscales images whose long edge is over ~1568px, so larger renders
# only cost upload time.
MAX_IMAGE_EDGE = 1568


def _open(data: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise ValueError(f"This file isn't a readable PDF ({e}).") from e


def page_texts(data: bytes) -> list[str]:
    """The text of every page, in order. Pages with no text layer give ''."""
    doc = _open(data)
    try:
        texts = []
        for page in doc:
            textpage = page.get_textpage()
            # PDFs use \r\n between lines; normalize so Claude sees plain newlines.
            texts.append(textpage.get_text_bounded().replace("\r\n", "\n").strip())
            textpage.close()
            page.close()
        return texts
    finally:
        doc.close()


def page_count(data: bytes) -> int:
    doc = _open(data)
    try:
        return len(doc)
    finally:
        doc.close()


def parse_pages(spec: int | str | None, count: int) -> list[int]:
    """Turn "1-3, 7" into [1, 2, 3, 7] (1-based, as people count pages).

    A single page may arrive as a number (3) rather than text ("3"). None
    means every page. Raises ValueError for pages that don't exist, so
    Claude learns the real page count instead of getting a silent empty result.
    """
    if spec is None or spec == "":
        return list(range(1, count + 1))
    pages: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part:
                lo, hi = (int(x) for x in part.split("-", 1))
            else:
                lo = hi = int(part)
        except ValueError:
            raise ValueError(f"Can't read page range {part!r}; use a form like '1-3,7'.") from None
        if lo < 1 or hi > count or lo > hi:
            raise ValueError(f"Pages {part} don't exist; this PDF has {count} pages.")
        pages.extend(p for p in range(lo, hi + 1) if p not in pages)
    return pages


def render_pages(data: bytes, pages: list[int]) -> list[bytes]:
    """PNG images of the given 1-based pages, sized for Claude to read."""
    doc = _open(data)
    try:
        images = []
        for number in pages:
            page = doc[number - 1]
            width, height = page.get_size()  # in points, 72 per inch
            scale = min(MAX_IMAGE_EDGE / max(width, height), 3.0)
            image = page.render(scale=scale).to_pil()
            buf = io.BytesIO()
            image.save(buf, format="PNG", optimize=True)
            images.append(buf.getvalue())
            page.close()
        return images
    finally:
        doc.close()
