"""Reading scanned PDFs with the OCR worker. Tesseract itself only runs where it's installed. See docs/DECISIONS.md, D58."""
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config
from evaluation.test_documents import to_pdf, to_scanned_pdf
from ops.ocr import worker
from pipeline import ocr
from pipeline.extract import ExtractionError, extract_document, pdf_markdown, size_bands, with_ocr

POLICY = """# Work From Home Policy

## 1. Eligibility

Confirmed employees can work from home for up to two days each week with their manager's approval.

## 2. Probation

Employees on probation are not eligible for regular work from home arrangements.
"""


def tesseract_data(lines: list[tuple[str, int, int]]) -> dict:
    """What pytesseract's image_to_data gives, for lines of (text, top, word height) in pixels."""
    data: dict[str, list] = {key: [] for key in ("text", "conf", "block_num", "par_num", "line_num", "left", "top",
                                                  "height")}
    for number, (text, top, height) in enumerate(lines, start=1):
        for position, word in enumerate(text.split()):
            for key, value in (("text", word), ("conf", "95"), ("block_num", 1), ("par_num", 1),
                               ("line_num", number), ("left", 100 + position * 50), ("top", top), ("height", height)):
                data[key].append(value)
    return data


def test_tesseract_words_become_lines_in_points():
    data = tesseract_data([("Work From Home", 100, 75), ("Staff can work from home", 300, 42)])
    data["text"].append("~")  # a speck Tesseract isn't sure about
    for key, value in (("conf", "12"), ("block_num", 1), ("par_num", 1), ("line_num", 2), ("left", 10),
                       ("top", 300), ("height", 42)):
        data[key].append(value)
    lines = worker.lines_from_data(data)
    assert [line["text"] for line in lines] == ["Work From Home", "Staff can work from home"]
    assert lines[0]["size"] == 18.0 and lines[1]["size"] == 10.1  # 300 dpi pixels to points
    assert lines[0]["top"] == 24.0


def test_ocr_sizes_are_grouped_into_bands():
    assert size_bands([18.0, 17.2, 13.9, 13.1, 10.2, 9.8, 9.5]) == {
        18.0: 18.0, 17.2: 18.0, 13.9: 13.9, 13.1: 13.9, 10.2: 10.2, 9.8: 10.2, 9.5: 10.2}


def test_scanned_lines_find_headings_by_their_size():
    found = {0: [{"text": "Work From Home Policy", "top": 20, "bottom": 40, "size": 17.5},
                 {"text": "Eligibility", "top": 60, "bottom": 75, "size": 13.4},
                 {"text": "Staff can work from home two days a week.", "top": 80, "bottom": 90, "size": 9.6},
                 {"text": "Probation", "top": 100, "bottom": 115, "size": 13.9},
                 {"text": "Not while on probation.", "top": 120, "bottom": 130, "size": 10.1}]}
    blocks, body = with_ocr([], 0.0, found)
    markdown, headed = pdf_markdown(blocks, body)
    assert headed
    assert "# Work From Home Policy" in markdown and "## Eligibility" in markdown and "## Probation" in markdown
    assert "## Staff" not in markdown


@pytest.fixture
def ocr_on(monkeypatch):
    monkeypatch.setattr(config, "OCR_URL", "http://ocr.test")
    monkeypatch.setattr(config, "OCR_TOKEN", "secret")
    asked: list = []

    def read(pdf, pages):
        asked.append(pages)
        return {page: [{"text": "Scanned Section", "top": 30, "bottom": 45, "size": 14.0},
                       {"text": f"Text read from page {page + 1}.", "top": 60, "bottom": 70, "size": 10.0}]
                for page in pages}

    monkeypatch.setattr(ocr, "read_scanned_pages", read)
    return asked


def test_a_scanned_pdf_is_read_when_ocr_is_on(ocr_on):
    markdown = extract_document(to_scanned_pdf(POLICY), "Scan.pdf").markdown
    assert ocr_on == [[0]]
    # The page's first and largest line is its title
    assert "Text read from page 1." in markdown and "# Scanned Section" in markdown


def test_only_the_scanned_pages_go_to_the_worker(ocr_on):
    import io

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for page in [*PdfReader(io.BytesIO(to_pdf(POLICY))).pages, *PdfReader(io.BytesIO(to_scanned_pdf(POLICY))).pages]:
        writer.add_page(page)
    mixed = io.BytesIO()
    writer.write(mixed)
    markdown = extract_document(mixed.getvalue(), "Mixed.pdf").markdown
    assert ocr_on == [[1]]  # the first page has its own text
    assert "Confirmed employees can work from home" in markdown and "Text read from page 2." in markdown


def test_a_pdf_with_text_never_goes_to_the_worker(ocr_on):
    extract_document(to_pdf(POLICY), "Policy.pdf")
    assert ocr_on == []


def test_a_worker_that_does_not_answer_is_explained(monkeypatch):
    monkeypatch.setattr(config, "OCR_URL", "http://ocr.test")

    def down(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    with pytest.raises(ExtractionError, match="OCR worker didn't answer"):
        extract_document(to_scanned_pdf(POLICY), "Scan.pdf")


def test_a_scan_with_nothing_readable_says_so(monkeypatch):
    monkeypatch.setattr(config, "OCR_URL", "http://ocr.test")
    monkeypatch.setattr(ocr, "read_scanned_pages", lambda pdf, pages: {page: [] for page in pages})
    with pytest.raises(ExtractionError, match="even with OCR"):
        extract_document(to_scanned_pdf(POLICY), "Faint.pdf")


def test_the_client_sends_the_token_and_the_pages(monkeypatch):
    monkeypatch.setattr(config, "OCR_URL", "http://ocr.test")
    monkeypatch.setattr(config, "OCR_TOKEN", "secret")
    sent: dict = {}

    def post(url, content, params, headers, timeout):
        sent.update(url=url, params=params, headers=headers)
        return httpx.Response(200, json={"pages": {"2": [{"text": "x", "top": 1, "bottom": 2, "size": 10}]}},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    assert ocr.read_scanned_pages(b"%PDF-1.4", [0, 2]) == {2: [{"text": "x", "top": 1, "bottom": 2, "size": 10}]}
    assert sent == {"url": "http://ocr.test/ocr", "params": {"pages": "0,2"},
                    "headers": {"Content-Type": "application/pdf", "X-OCR-Token": "secret"}}


# The worker's web app

@pytest.fixture
def worker_client(monkeypatch):
    monkeypatch.setenv("OCR_TOKEN", "secret")
    monkeypatch.setattr(worker, "read_pages", lambda pdf, pages: {str(page): [] for page in pages})
    return TestClient(worker.app)


def test_the_worker_needs_its_token(worker_client):
    pdf = to_pdf(POLICY)
    assert worker_client.post("/ocr?pages=0", content=pdf).status_code == 401
    assert worker_client.post("/ocr?pages=0", content=pdf, headers={"X-OCR-Token": "wrong"}).status_code == 401
    response = worker_client.post("/ocr?pages=0", content=pdf, headers={"X-OCR-Token": "secret"})
    assert response.status_code == 200 and response.json() == {"pages": {"0": []}}


@pytest.mark.parametrize(("body", "pages"), [(b"not a pdf", "0"), (b"%PDF-1.4", ""), (b"%PDF-1.4", "x"),
                                             (b"%PDF-1.4", ",".join(map(str, range(51))))])
def test_the_worker_refuses_what_it_should_not_read(worker_client, body, pages):
    response = worker_client.post(f"/ocr?pages={pages}", content=body, headers={"X-OCR-Token": "secret"})
    assert response.status_code == 422


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract isn't installed here")
def test_real_tesseract_reads_a_scanned_policy():
    scan = to_scanned_pdf(POLICY, dpi=200)
    lines = worker.read_pages(scan, [0])["0"]
    blocks, body = with_ocr([], 0.0, {0: lines})
    markdown, headed = pdf_markdown(blocks, body)
    assert headed and "Eligibility" in markdown
    assert "work from home" in markdown.lower() and "probation" in markdown.lower()
