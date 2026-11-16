"""Make PDF and Word versions of the sample policies, to test how uploads are read. For development only.

The PDFs have headings in larger bold fonts and tables drawn as tables, as a policy exported from Word would. They're
made with fpdf2, which isn't in the API image. See docs/DECISIONS.md, D57.
"""
import io
import re
from dataclasses import dataclass
from pathlib import Path

BOLD = re.compile(r"\*\*(.+?)\*\*")
UNICODE_FONT = Path("C:/Windows/Fonts/arial.ttf")  # used if present, as the core PDF fonts are only Latin-1
UNICODE_BOLD = Path("C:/Windows/Fonts/arialbd.ttf")
PLAIN = {"\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u20b9": "Rs.",
         "\u2022": "-", "\u2026": "...", "\u00a0": " "}


@dataclass
class Block:
    kind: str  # "heading", "paragraph", "table" or "item"
    text: str = ""
    level: int = 0
    rows: list[list[str]] | None = None


def plain(text: str) -> str:
    """Markdown's bold markers off, and the text as it reads."""
    return BOLD.sub(r"\1", text).strip()


def parse(markdown: str) -> list[Block]:
    """The headings, paragraphs, list items and tables of a policy written in this project's Markdown."""
    blocks: list[Block] = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            blocks.append(Block("heading", plain(heading[2]), len(heading[1])))
        elif line.startswith("|"):
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [plain(cell) for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
                    rows.append(cells)
                index += 1
            blocks.append(Block("table", rows=rows))
            continue
        elif re.match(r"^[-*]\s+|^\d+\.\s+", line):
            blocks.append(Block("item", plain(re.sub(r"^([-*]|\d+\.)\s+", "", line))))
        else:
            # A paragraph wrapped over several lines in the Markdown is one paragraph
            parts = [line]
            while index + 1 < len(lines) and lines[index + 1].strip() and not re.match(
                    r"^(#{1,6}\s|\||[-*]\s|\d+\.\s)", lines[index + 1].strip()):
                index += 1
                parts.append(lines[index].strip())
            blocks.append(Block("paragraph", plain(" ".join(parts))))
        index += 1
    return blocks


def to_pdf(markdown: str) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=15)
    unicode = UNICODE_FONT.exists() and UNICODE_BOLD.exists()
    if unicode:
        pdf.add_font("Body", "", str(UNICODE_FONT))
        pdf.add_font("Body", "B", str(UNICODE_BOLD))
    family = "Body" if unicode else "Helvetica"

    def text_of(value: str) -> str:
        if unicode:
            return value
        for character, replacement in PLAIN.items():
            value = value.replace(character, replacement)
        return value.encode("latin-1", "replace").decode("latin-1")

    pdf.add_page()
    sizes = {1: 18, 2: 14, 3: 12}
    for block in parse(markdown):
        if block.kind == "heading":
            pdf.ln(3)
            pdf.set_font(family, "B", sizes.get(block.level, 11))
            pdf.multi_cell(0, 7, text_of(block.text), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
        elif block.kind == "table" and block.rows:
            pdf.set_font(family, "", 9)
            width = max(len(row) for row in block.rows)
            with pdf.table(headings_style=FontFace(emphasis="BOLD"), line_height=5) as table:
                for row in block.rows:
                    cells = table.row()
                    for column in range(width):
                        cells.cell(text_of(row[column] if column < len(row) else ""))
            pdf.ln(2)
        else:
            pdf.set_font(family, "", 10)
            prefix = "- " if block.kind == "item" else ""
            pdf.multi_cell(0, 5, text_of(prefix + block.text), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
    return bytes(pdf.output())


def to_docx(markdown: str) -> bytes:
    import docx

    document = docx.Document()
    for block in parse(markdown):
        if block.kind == "heading":
            if block.level == 1:
                document.add_paragraph(block.text, style="Title")
            else:
                document.add_heading(block.text, level=min(block.level - 1, 3))
        elif block.kind == "table" and block.rows:
            width = max(len(row) for row in block.rows)
            table = document.add_table(rows=len(block.rows), cols=width)
            table.style = "Table Grid"
            for row_cells, row in zip(table.rows, block.rows, strict=True):
                for column in range(width):
                    row_cells.cells[column].text = row[column] if column < len(row) else ""
        elif block.kind == "item":
            document.add_paragraph(block.text, style="List Bullet")
        else:
            document.add_paragraph(block.text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def to_scanned_pdf(markdown: str, dpi: int = 200) -> bytes:
    """The PDF as a scan would arrive: each page an image, with no text in it at all."""
    import pypdfium2
    from fpdf import FPDF

    document = pypdfium2.PdfDocument(io.BytesIO(to_pdf(markdown)))
    try:
        scan = FPDF(format="A4")
        for page in document:
            scan.add_page()
            scan.image(page.render(scale=dpi / 72).to_pil(), x=0, y=0, w=scan.w, h=scan.h)
        return bytes(scan.output())
    finally:
        document.close()
