"""Turn an uploaded file into Markdown with headings, so the chunker can treat every format alike.

A PDF's headings come from its fonts: a line larger than the body text, or bold and short, is a heading. Its tables
become Markdown tables. A PDF whose fonts show nothing, and plain text, fall back to treating numbered lines such as
"4.1 Earned Leave" and short lines in capitals as headings. Word documents use their heading styles. A scanned page has
no text of its own, so it's read by the OCR worker, if one is set up, and refused if not. See docs/DECISIONS.md, D57
and D58.
"""
import io
import re
import statistics
import zipfile
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

MAX_PDF_PAGES = 300
MAX_DOCX_UNPACKED_BYTES = 50_000_000  # stops a tiny file that unpacks to something huge
SUPPORTED_TYPES = {".md", ".markdown", ".txt", ".pdf", ".docx", ".html", ".htm"}
NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+([A-Z][^\n]{1,100})$")
CAPITALS_HEADING = re.compile(r"^[A-Z][A-Z0-9 ,&/()'-]{3,80}$")
MARKDOWN_HEADING = re.compile(r"^(#{1,3}) +\S", re.MULTILINE)
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class ExtractionError(Exception):
    """The file can't be used. The message is safe to show the person uploading it."""


@dataclass
class ExtractedDocument:
    markdown: str
    source_type: str


def title_from_name(file_name: str) -> str:
    return re.sub(r"[_-]+", " ", Path(file_name).stem).strip() or "Untitled"


def add_headings(text: str) -> str:
    """Mark up numbered and capitalised lines as headings, for text with no structure of its own."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        numbered = NUMBERED_HEADING.match(line)
        if numbered and len(line) <= 100 and not line.endswith((".", ",", ";", ":")):
            depth = numbered[1].count(".")
            lines.append(f"\n{'##' if depth == 0 else '###'} {line}\n")
        elif CAPITALS_HEADING.match(line):
            lines.append(f"\n## {line.title()}\n")
        else:
            lines.append(line)
    return "\n".join(lines)


def finish(markdown: str, file_name: str, title: str | None) -> str:
    """Make sure the result starts with a title and has at least one section."""
    text = CONTROL_CHARACTERS.sub("", markdown).strip()
    if not text:
        raise ExtractionError("No text could be read from this file.")
    first = MARKDOWN_HEADING.search(text)
    if not first or len(first[1]) != 1:
        text = f"# {title or title_from_name(file_name)}\n\n{text}"
    if not re.search(r"^#{2,3} +\S", text, re.MULTILINE):
        heading, _, body = text.partition("\n")
        text = f"{heading}\n\n## Content\n\n{body.strip()}"
    return text + "\n"


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExtractionError("The file must be text, saved as UTF-8.")


def markdown_table(rows: list[list[str | None]]) -> str:
    """A table as Markdown, its first row as the header. A | inside a cell would split it, so it becomes /."""
    def cell(value: str | None) -> str:
        return " ".join((value or "").split()).replace("|", "/")

    width = max(len(row) for row in rows)
    cleaned = [[cell(row[index] if index < len(row) else "") for index in range(width)] for row in rows]
    lines = ["| " + " | ".join(cleaned[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(row) + " |" for row in cleaned[1:] if any(row)]
    return "\n".join(lines)


def is_bold(chars: list[dict]) -> bool:
    names = [str(char.get("fontname", "")).lower() for char in chars if str(char.get("text", "")).strip()]
    heavy = sum(any(mark in name for mark in ("bold", "black", "heavy")) for name in names)
    return bool(names) and heavy > len(names) / 2


def line_size(chars: list[dict]) -> float:
    sizes = [round(float(char.get("size", 0)), 1) for char in chars if str(char.get("text", "")).strip()]
    return statistics.median(sizes) if sizes else 0.0


def heading_like(text: str) -> bool:
    return 0 < len(text) <= 120 and not text.endswith((".", ",", ";", ":")) and len(text.split()) <= 16


def pdf_blocks(pdf) -> tuple[list[tuple], float]:
    """Every page's text lines and tables in reading order, and the body text's font size."""
    sizes: Counter = Counter()
    blocks: list[tuple] = []
    for number, page in enumerate(pdf.pages):
        tables = [table for table in page.find_tables()
                  if len(table.rows) >= 2 and max(len(row.cells) for row in table.rows) >= 2]
        boxes = [table.bbox for table in tables]

        def outside(obj, boxes=boxes) -> bool:
            if obj.get("object_type") != "char":
                return True
            x, y = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
            return not any(x0 <= x <= x1 and top <= y <= bottom for x0, top, x1, bottom in boxes)

        for line in page.filter(outside).extract_text_lines(return_chars=True, strip=True):
            chars = line["chars"]
            sizes.update(round(float(char.get("size", 0)), 1) for char in chars if char.get("text", "").strip())
            blocks.append((number, line["top"], "line", line["text"].strip(), line_size(chars), is_bold(chars),
                           line["bottom"]))
        for table in tables:
            rows = table.extract()
            if rows:
                blocks.append((number, table.bbox[1], "table", rows, 0.0, False, table.bbox[3]))
    blocks.sort(key=lambda block: (block[0], block[1]))
    return blocks, (sizes.most_common(1)[0][0] if sizes else 0.0)


def pdf_markdown(blocks: list[tuple], body: float) -> tuple[str, bool]:
    """The blocks as Markdown, and whether the fonts showed any headings."""
    candidates = {block[4] for block in blocks
                  if block[2] == "line" and block[4] >= body * 1.15 and heading_like(block[3])}
    ranked = sorted(candidates, reverse=True)
    first_size = next((block[4] for block in blocks if block[2] == "line"), 0.0)
    # The largest size is the title if the document starts with it. Larger sizes come first, smaller ones under them.
    title = ranked[0] if ranked and first_size == ranked[0] else None
    levels = {size: (1 if size == title else min(2 + index - (title is not None), 3))
              for index, size in enumerate(ranked)}
    out: list[str] = []
    found = False
    previous_bottom, previous_page, previous_heading = None, None, None
    last_table: tuple | None = None  # its page, its rows, and its text in out
    for page, top, kind, content, size, bold, bottom in blocks:
        if kind == "table":
            # A table carried onto the next page, with its header repeated, continues the one before it
            if last_table and page == last_table[0] + 1 and out and out[-1] is last_table[2] and content[0] == \
                    last_table[1][0]:
                merged = last_table[1] + content[1:]
                out[-1] = "\n" + markdown_table(merged) + "\n"
                last_table = (page, merged, out[-1])
            else:
                out.append("\n" + markdown_table(content) + "\n")
                last_table = (page, content, out[-1])
            previous_heading = None
        else:
            level = levels.get(size) if size in candidates else (3 if bold and heading_like(content) else None)
            wrapped = (level is not None and level == previous_heading and previous_page == page
                       and previous_bottom is not None and top - previous_bottom < size)
            if wrapped:
                out[-1] = out[-1].rstrip("\n") + " " + content + "\n"  # a heading wrapped onto a second line
            elif level is not None:
                out.append(f"\n{'#' * level} {content}\n")
                found = found or level > 1
            else:
                gap = top - previous_bottom if previous_bottom is not None and previous_page == page else 0.0
                out.append(("\n" if gap > max(size, 1.0) else "") + content)
            previous_heading = level
        previous_bottom, previous_page = bottom, page
    return "\n".join(out), found


def size_bands(sizes: list[float]) -> dict[float, float]:
    """Group sizes within 10% of each other, as OCR measures the same font a little differently on each line."""
    bands: dict[float, float] = {}
    top = None
    for size in sorted(set(sizes), reverse=True):
        if top is None or size < top * 0.9:
            top = size
        bands[size] = top
    return bands


def with_ocr(blocks: list[tuple], body: float, found: dict[int, list[dict]]) -> tuple[list[tuple], float]:
    """Add the lines the OCR worker read. Their sizes are scaled so its body text matches the PDF's own."""
    lines = [(page, line) for page, page_lines in found.items() for line in page_lines if line.get("text", "").strip()]
    if not lines:
        return blocks, body
    bands = size_bands([float(line["size"]) for _, line in lines])
    # The body is the size with the most characters, as for a PDF's own text: headings are many lines but short
    characters: Counter = Counter()
    for _, line in lines:
        characters[bands[float(line["size"])]] += len(line["text"])
    ocr_body = characters.most_common(1)[0][0]
    scale = body / ocr_body if body and ocr_body else 1.0
    added = [(page, float(line["top"]), "line", line["text"].strip(), round(bands[float(line["size"])] * scale, 1),
              False, float(line["bottom"])) for page, line in lines]
    return sorted(blocks + added, key=lambda block: (block[0], block[1])), body or ocr_body


def from_pdf(data: bytes) -> str:
    import pdfplumber

    from pipeline.ocr import OcrUnavailable, ocr_enabled, read_scanned_pages

    if not data.startswith(b"%PDF-"):
        raise ExtractionError("This file doesn't look like a real PDF.")
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise ExtractionError(f"This PDF has more than {MAX_PDF_PAGES} pages.")
            blocks, body = pdf_blocks(pdf)
            page_count = len(pdf.pages)
    except ExtractionError:
        raise
    except Exception as error:
        # pdfminer wraps its errors, so a password is recognised by name
        if "password" in f"{type(error).__name__} {error!r} {error.__cause__!r}".lower():
            raise ExtractionError("This PDF is password protected. Please upload an unprotected copy.") from error
        raise ExtractionError("This PDF couldn't be read.") from error
    # Pages with no text of their own are scans, which the OCR worker can read if it's switched on (D58)
    with_text = {block[0] for block in blocks if block[2] == "table" or block[3]}
    scanned = [number for number in range(page_count) if number not in with_text]
    if scanned and ocr_enabled():
        try:
            blocks, body = with_ocr(blocks, body, read_scanned_pages(data, scanned))
        except OcrUnavailable as error:
            raise ExtractionError(str(error)) from error
    if not any(block[2] == "table" or block[3] for block in blocks):
        if ocr_enabled():
            raise ExtractionError("No text could be read from this PDF, even with OCR. Is the scan very faint?")
        raise ExtractionError("This PDF has no readable text. It may be scanned, and OCR isn't switched on.")
    markdown, found = pdf_markdown(blocks, body)
    # With no headings in the fonts, numbered and capitalised lines are the best guess, as before
    return markdown if found else add_headings(markdown)


def from_docx(data: bytes) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "word/document.xml" not in archive.namelist():
                raise ExtractionError("This file doesn't look like a real Word document.")
            if sum(item.file_size for item in archive.infolist()) > MAX_DOCX_UNPACKED_BYTES:
                raise ExtractionError("This Word document is too large once unpacked.")
        document = docx.Document(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, KeyError) as error:
        raise ExtractionError("This Word document couldn't be read.") from error

    lines = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            kind = docx_style_kind(block.style)
            if kind == 0:
                lines.append(f"# {text}")
            elif kind is not None:
                lines.append(f"{'#' * min(kind + 1, 3)} {text}")
            elif is_list_item(block):
                lines.append(f"- {text}")
            else:
                lines.append(text)
        elif isinstance(block, Table):
            rows = [[cell.text for cell in row.cells] for row in block.rows]
            if len(rows) >= 2 and max(len(row) for row in rows) >= 2:
                lines.append(markdown_table(rows))
            else:  # a one-cell box or a single line is just text
                lines += [" ".join(cell.strip() for cell in row if cell.strip()) for row in rows]
    return "\n\n".join(line for line in lines if line)


def docx_style_kind(style) -> int | None:
    """0 for the title, the level for a heading, or None. A custom style based on a heading style counts as one."""
    seen = 0
    while style is not None and seen < 10:  # a style can't be based on itself, but a broken file could say so
        name = style.name or ""
        if name == "Title":
            return 0
        level = re.fullmatch(r"Heading ([1-9])", name)
        if level:
            return int(level[1])
        style, seen = style.base_style, seen + 1
    return None


def is_list_item(paragraph) -> bool:
    """A bulleted or numbered paragraph, from its style or from Word's numbering."""
    name = (paragraph.style.name if paragraph.style is not None else "") or ""
    return name.startswith("List") or paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None


class _HtmlToMarkdown(HTMLParser):
    SKIPPED = {"script", "style", "nav", "header", "footer", "noscript", "head", "svg"}
    HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "###", "h5": "###", "h6": "###"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0
        self.first_cell = True

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.SKIPPED:
            self.skipping += 1
        elif tag in self.HEADINGS:
            self.parts.append(f"\n\n{self.HEADINGS[tag]} ")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("p", "div", "section", "article", "table", "br"):
            self.parts.append("\n\n" if tag != "br" else "\n")
        elif tag == "tr":
            self.parts.append("\n")
            self.first_cell = True
        elif tag in ("td", "th"):
            if not self.first_cell:
                self.parts.append(" | ")
            self.first_cell = False

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIPPED:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.HEADINGS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(re.sub(r"\s+", " ", data))


def from_html(data: bytes) -> str:
    parser = _HtmlToMarkdown()
    parser.feed(decode(data))
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts))


def extract_document(data: bytes, file_name: str, title: str | None = None) -> ExtractedDocument:
    """Read a file's text. Raises ExtractionError with a message for the person uploading it."""
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_TYPES:
        raise ExtractionError(f"Please upload one of: {', '.join(sorted(SUPPORTED_TYPES))}.")
    if suffix == ".pdf":
        markdown = from_pdf(data)
    elif suffix == ".docx":
        markdown = from_docx(data)
    elif suffix in (".html", ".htm"):
        markdown = from_html(data)
    elif suffix == ".txt":
        markdown = add_headings(decode(data))
    else:
        markdown = decode(data)
    return ExtractedDocument(finish(markdown, file_name, title), suffix.removeprefix("."))
