import pytest

from study_coach.documents import page_count, page_texts, parse_pages, render_pages

from pdf_samples import scanned_pdf, text_pdf


def test_text_is_extracted_per_page():
    assert page_texts(text_pdf("Normal forms", "Functional dependencies")) == [
        "Normal forms",
        "Functional dependencies",
    ]


def test_scanned_page_has_no_text_but_still_renders():
    data = scanned_pdf()
    assert page_texts(data) == [""]
    [png] = render_pages(data, [1])
    assert png.startswith(b"\x89PNG")


def test_rendered_pages_fit_claudes_image_size():
    from io import BytesIO

    from PIL import Image

    [png] = render_pages(text_pdf("Slide"), [1])
    assert max(Image.open(BytesIO(png)).size) <= 1568


def test_page_ranges_are_one_based_and_deduplicated():
    assert parse_pages("1-3, 2, 7", 10) == [1, 2, 3, 7]
    assert parse_pages(None, 3) == [1, 2, 3]


@pytest.mark.parametrize("spec", ["0", "4", "3-1", "two"])
def test_bad_page_ranges_explain_themselves(spec):
    with pytest.raises(ValueError, match="page"):
        parse_pages(spec, 3)


def test_non_pdf_bytes_give_a_clear_error():
    with pytest.raises(ValueError, match="readable PDF"):
        page_count(b"PK\x03\x04 this is a pptx")
