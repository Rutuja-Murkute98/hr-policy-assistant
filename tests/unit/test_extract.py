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


# PDFs with fonts and tables, as Word or a PDF printer would make them: see docs/DECISIONS.md, D57

def styled_pdf(blocks: list[tuple], encrypt: bool = False) -> bytes:
    """A PDF of ("h1" | "h2" | "p" | "table" | "page", content) blocks, with headings in larger bold fonts."""
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    pdf = FPDF()
    if encrypt:
        pdf.set_encryption(owner_password="owner", user_password="user")
    pdf.add_page()
    for kind, content in blocks:
        if kind == "page":
            pdf.add_page()
        elif kind == "table":
            pdf.set_font("Helvetica", "", 9)
            with pdf.table(headings_style=FontFace(emphasis="BOLD")) as table:
                for row in content:
                    cells = table.row()
                    for value in row:
                        cells.cell(value)
        else:
            pdf.set_font("Helvetica", "B" if kind != "p" else "", {"h1": 18, "h2": 14, "h3": 12}.get(kind, 10))
            pdf.multi_cell(0, 6, content, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def test_pdf_headings_come_from_the_fonts_even_without_numbers():
    data = styled_pdf([("h1", "Leave Policy"), ("p", "Leave rules for everyone in the company."),
                       ("h2", "Earned Leave"), ("p", "Staff get eighteen days of earned leave each year."),
                       ("h2", "Sick Leave"), ("p", "Staff get twelve days of sick leave each year.")])
    markdown = extract_document(data, "Leave.pdf").markdown
    assert "# Leave Policy" in markdown
    assert "## Earned Leave" in markdown and "## Sick Leave" in markdown
    assert "Staff get eighteen days" in markdown and "## Staff" not in markdown


def test_a_bold_short_line_in_body_size_is_a_subheading():
    data = styled_pdf([("h1", "Travel Policy"), ("p", "How travel works here."), ("h2", "Hotels"),
                       ("h3", "Tier one cities"), ("p", "The limit is four thousand rupees a night.")])
    assert "### Tier one cities" in extract_document(data, "Travel.pdf").markdown


def test_a_pdf_table_becomes_a_markdown_table():
    rows = [["Band", "Room rent limit"], ["Band 1 to Band 3", "Rs. 3,000"], ["Band 4 to Band 5", "Rs. 5,000 | daily"]]
    data = styled_pdf([("h1", "Insurance"), ("h2", "Room Rent"), ("p", "Limits by band."), ("table", rows)])
    markdown = extract_document(data, "Insurance.pdf").markdown
    assert "| Band | Room rent limit |" in markdown
    assert "| Band 1 to Band 3 | Rs. 3,000 |" in markdown
    assert "| Band 4 to Band 5 | Rs. 5,000 / daily |" in markdown  # a bar inside a cell can't split the row
    assert markdown.count("Band 1 to Band 3") == 1  # the table's words aren't repeated as text


def test_a_table_carried_onto_the_next_page_is_one_table():
    header = ["Leave type", "Days"]
    first = [header] + [[f"Leave {number}", str(number)] for number in range(1, 4)]
    second = [header] + [[f"Leave {number}", str(number)] for number in range(4, 6)]
    data = styled_pdf([("h1", "Leave"), ("h2", "Entitlements"), ("table", first), ("page", ""), ("table", second)])
    markdown = extract_document(data, "Leave.pdf").markdown
    assert markdown.count("| Leave type | Days |") == 1
    assert "| Leave 5 | 5 |" in markdown


def test_a_password_protected_pdf_is_explained():
    data = styled_pdf([("h1", "Secret"), ("p", "Text.")], encrypt=True)
    with pytest.raises(ExtractionError, match="password"):
        extract_document(data, "Secret.pdf")


def test_a_scanned_pdf_with_no_text_is_explained():
    from fpdf import FPDF
    from PIL import Image

    pdf = FPDF()
    pdf.add_page()
    pdf.image(Image.new("RGB", (200, 100), "white"), x=10, y=10, w=100)
    with pytest.raises(ExtractionError, match="scanned"):
        extract_document(bytes(pdf.output()), "Scan.pdf")


def word_file(build) -> bytes:
    import io

    import docx

    document = docx.Document()
    build(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_a_word_heading_style_based_on_a_built_in_one_counts():
    from docx.enum.style import WD_STYLE_TYPE

    def build(document):
        style = document.styles.add_style("Policy Section", WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = document.styles["Heading 1"]
        document.add_paragraph("Leave Policy", style="Title")
        document.add_paragraph("Earned Leave", style="Policy Section")
        document.add_paragraph("Staff get eighteen days of earned leave each year.")

    markdown = extract_document(word_file(build), "Leave.docx").markdown
    assert "## Earned Leave" in markdown


def test_word_tables_and_lists_keep_their_shape():
    def build(document):
        document.add_paragraph("Benefits", style="Title")
        document.add_heading("Room Rent", level=1)
        table = document.add_table(rows=2, cols=2)
        for row, values in zip(table.rows, [["Band", "Limit"], ["Band 1 to 3", "Rs. 3,000"]], strict=True):
            for cell, value in zip(row.cells, values, strict=True):
                cell.text = value
        box = document.add_table(rows=1, cols=1)
        box.rows[0].cells[0].text = "A note in a box"
        document.add_paragraph("Bring your ID card", style="List Bullet")

    markdown = extract_document(word_file(build), "Benefits.docx").markdown
    assert "| Band | Limit |\n| --- | --- |\n| Band 1 to 3 | Rs. 3,000 |" in markdown
    assert "A note in a box" in markdown and "| A note in a box |" not in markdown
    assert "- Bring your ID card" in markdown
