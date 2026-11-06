"""Reading each file type. No database or model is used."""
import io
import zipfile

import docx
import pytest

from pipeline.chunk_documents import chunk_text
from pipeline.extract import ExtractionError, add_headings, extract_document, finish


def make_pdf(lines: list[str]) -> bytes:
    """Build a small real PDF with text, so no sample file is needed."""
    body = "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(body)} >>\nstream\n{body}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for number, content in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{content}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


def make_docx() -> bytes:
    document = docx.Document()
    document.add_paragraph("Travel Policy", style="Title")
    document.add_paragraph("Rules for business travel apply to everyone at the company.")
    document.add_paragraph("Hotel Limits", style="Heading 1")
    document.add_paragraph("Hotels are capped by the type of city.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "City"
    table.cell(0, 1).text = "Limit"
    table.cell(1, 0).text = "Tier-1"
    table.cell(1, 1).text = "Rs. 4,000"
    document.add_paragraph("Daily Allowance", style="Heading 2")
    document.add_paragraph("Rs. 800 per day in big cities.")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_markdown_is_kept_as_it_is():
    result = extract_document(b"# Leave\n\n## 1. Rules\n\nFollow them.\n", "Leave.md")
    assert result.markdown == "# Leave\n\n## 1. Rules\n\nFollow them.\n"
    assert result.source_type == "md"


def test_markdown_without_a_title_gets_one_from_the_file_name():
    result = extract_document(b"## 1. Rules\n\nFollow them.\n", "Leave_Policy.md")
    assert result.markdown.startswith("# Leave Policy\n\n## 1. Rules")


def test_a_given_title_is_used():
    assert extract_document(b"Some text here.", "x.txt", "My Title").markdown.startswith("# My Title")


def test_text_files_find_numbered_and_capitalised_headings():
    text = b"1. Purpose\nWhy this exists.\n2.1 Earned Leave\nEighteen days.\nGENERAL RULES\nBe fair."
    markdown = extract_document(text, "Leave.txt").markdown
    assert "## 1. Purpose" in markdown
    assert "### 2.1 Earned Leave" in markdown
    assert "## General Rules" in markdown


def test_a_numbered_sentence_isnt_a_heading():
    assert "#" not in add_headings("1. Employees must always follow the rules.")


def test_text_with_no_headings_still_makes_a_section():
    markdown = extract_document(b"Just one paragraph of text about leave.", "Note.txt").markdown
    assert markdown.startswith("# Note\n\n## Content\n\nJust one")
    assert chunk_text(markdown, "Note.md")[0].metadata["section"] == "Content"


def test_windows_text_encoding_is_read():
    assert "Café" in extract_document("Café rules apply here.".encode("cp1252"), "Note.txt").markdown


def test_html_keeps_headings_lists_and_tables_and_drops_scripts():
    page = b"""<html><head><title>x</title><style>p {color:red}</style></head><body><nav>Menu</nav>
    <h1>Travel Policy</h1><p>Rules for &amp; about travel.</p><h2>Hotels</h2><ul><li>Book early</li></ul>
    <table><tr><th>City</th><th>Limit</th></tr><tr><td>Tier-1</td><td>Rs. 4,000</td></tr></table>
    <script>alert('x')</script></body></html>"""
    markdown = extract_document(page, "Travel.html").markdown
    assert "# Travel Policy" in markdown
    assert "## Hotels" in markdown
    assert "- Book early" in markdown
    assert "Tier-1 | Rs. 4,000" in markdown
    assert "Rules for & about travel." in markdown
    assert "alert" not in markdown and "Menu" not in markdown and "color" not in markdown


def test_docx_keeps_headings_and_tables():
    markdown = extract_document(make_docx(), "Travel.docx").markdown
    assert markdown.startswith("# Travel Policy")
    assert "## Hotel Limits" in markdown
    assert "### Daily Allowance" in markdown
    assert "Tier-1 | Rs. 4,000" in markdown


def test_pdf_text_is_read_and_headings_are_found():
    markdown = extract_document(make_pdf(["1. Hotels", "Rs. 4,000 per night in big cities."]), "Travel.pdf").markdown
    assert markdown.startswith("# Travel")
    assert "## 1. Hotels" in markdown
    assert "Rs. 4,000 per night" in markdown


def test_each_format_produces_chunks():
    for data, name in [(make_docx(), "A.docx"), (make_pdf(["1. Hotels", "Text here."]), "B.pdf"),
                       (b"<h1>T</h1><h2>S</h2><p>Text here.</p>", "C.html"), (b"1. Rules\nText here.", "D.txt")]:
        assert chunk_text(extract_document(data, name).markdown, "x.md"), name


@pytest.mark.parametrize("name", ["virus.exe", "archive.zip", "noextension", "image.png"])
def test_other_file_types_are_refused(name):
    with pytest.raises(ExtractionError, match="Please upload one of"):
        extract_document(b"data", name)


def test_a_fake_pdf_is_refused():
    with pytest.raises(ExtractionError, match="real PDF"):
        extract_document(b"not a pdf", "x.pdf")


def test_a_damaged_pdf_is_refused():
    with pytest.raises(ExtractionError):
        extract_document(b"%PDF-1.4 rubbish", "x.pdf")


def test_a_pdf_with_no_text_explains_ocr_is_off():
    with pytest.raises(ExtractionError, match="OCR"):
        extract_document(make_pdf([]), "scan.pdf")


def test_a_fake_docx_is_refused():
    with pytest.raises(ExtractionError, match="couldn't be read"):
        extract_document(b"not a zip", "x.docx")


def test_a_zip_that_isnt_a_word_document_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.txt", "hello")
    with pytest.raises(ExtractionError, match="real Word document"):
        extract_document(buffer.getvalue(), "x.docx")


def test_a_docx_that_unpacks_to_something_huge_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"0" * 60_000_000)
    with pytest.raises(ExtractionError, match="too large"):
        extract_document(buffer.getvalue(), "x.docx")


def test_empty_files_are_refused():
    with pytest.raises(ExtractionError, match="No text"):
        extract_document(b"   ", "x.txt")


def test_control_characters_are_removed():
    assert "\x00" not in finish("# T\n\n## S\n\nText\x00 here", "x.md", None)
