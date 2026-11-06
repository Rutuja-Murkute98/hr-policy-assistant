"""Turn an uploaded file into Markdown with headings, so the chunker can treat every format alike.

PDF and plain text have no reliable heading information, so numbered lines such as "4.1 Earned
Leave" and short lines in capitals are treated as headings. Tables become lines with | between
cells. Scanned PDFs have no text to read, and OCR isn't included because of memory limits.
"""
import io
import re
import zipfile
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


def from_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    if not data.startswith(b"%PDF-"):
        raise ExtractionError("This file doesn't look like a real PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ExtractionError("This PDF is password protected. Please upload an unprotected copy.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ExtractionError(f"This PDF has more than {MAX_PDF_PAGES} pages.")
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PyPdfError, ValueError, KeyError, TypeError, OSError) as error:
        raise ExtractionError("This PDF couldn't be read.") from error
    if not text.strip():
        raise ExtractionError("This PDF has no readable text. It may be scanned, and OCR isn't switched on.")
    return add_headings(text)


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
            style = block.style.name if block.style is not None else ""
            level = re.fullmatch(r"Heading ([1-9])", style or "")
            if not text:
                continue
            if style == "Title":
                lines.append(f"# {text}")
            elif level:
                lines.append(f"{'#' * min(int(level[1]) + 1, 3)} {text}")
            else:
                lines.append(text)
        elif isinstance(block, Table):
            lines += [" | ".join(cell.text.strip() for cell in row.cells) for row in block.rows]
    return "\n\n".join(lines)


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
