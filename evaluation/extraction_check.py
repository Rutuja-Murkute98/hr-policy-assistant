"""Compare how the old and new readers turn PDF and Word versions of the policies back into Markdown.

    python -m evaluation.extraction_check

Every sample policy, and a test document of tables, is made into a PDF and a Word file with
evaluation/test_documents.py, then read back. The Markdown it came from is the answer key, so no model is needed.
Each is also made with its section numbers taken out, as many real policies have none. The old PDF reader needs pypdf,
which the API no longer installs. See docs/DECISIONS.md, D57.
"""
import io
import json
import re
from pathlib import Path

from evaluation.run_eval import REPORTS, save_json
from evaluation.test_documents import parse, to_docx, to_pdf
from pipeline.extract import add_headings, from_docx, from_pdf

POLICIES = Path("policies")
TABLE_DOCUMENTS = Path(__file__).parent / "data" / "table_documents"


def old_pdf(data: bytes) -> str:
    """The PDF reader before Phase 12: pypdf's text, with numbered lines guessed as headings."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return add_headings("\n".join(page.extract_text() or "" for page in reader.pages))


def old_docx(data: bytes) -> str:
    """The Word reader before Phase 12: built-in heading styles only, and table rows as words between bars."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    lines = []
    for block in docx.Document(io.BytesIO(data)).iter_inner_content():
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


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9,.']*[a-z0-9]|[a-z0-9]", text.lower())


def normal(text: str) -> str:
    return " ".join(words(text))


def score(markdown: str, extracted: str) -> dict:
    """How much of the source's structure and text came back."""
    blocks = parse(markdown)
    headings = [normal(block.text) for block in blocks if block.kind == "heading" and block.level >= 2]
    found_headings = {normal(line.lstrip("#")) for line in extracted.splitlines() if line.startswith("#")}
    rows = [[normal(cell) for cell in row] for block in blocks if block.kind == "table" for row in block.rows or []]
    lines = [normal(line) for line in extracted.splitlines()]
    table_lines = [normal(line) for line in extracted.splitlines() if line.lstrip().startswith("|")]

    def intact(row: list[str], among: list[str]) -> bool:
        """Every cell of the row on one line, in order."""
        pattern = r"\b" + r"\b.*\b".join(re.escape(cell) for cell in row if cell) + r"\b"
        return any(re.search(pattern, line) for line in among)

    source = set(words(" ".join(block.text or " ".join(" ".join(row) for row in block.rows or []) for block in blocks)))
    got = set(words(extracted))
    return {
        "headings": len(headings), "headings_found": sum(heading in found_headings for heading in headings),
        "table_rows": len(rows), "rows_intact": sum(intact(row, lines) for row in rows),
        "rows_as_markdown_table": sum(intact(row, table_lines) for row in rows),
        "words": len(source), "words_found": len(source & got),
    }


def total(results: list[dict]) -> dict:
    sums = {key: sum(result[key] for result in results) for key in results[0]}
    return {"headings_found": round(sums["headings_found"] / max(sums["headings"], 1), 3),
            "table_rows_intact": round(sums["rows_intact"] / max(sums["table_rows"], 1), 3),
            "table_rows_as_markdown_tables": round(sums["rows_as_markdown_table"] / max(sums["table_rows"], 1), 3),
            "words_found": round(sums["words_found"] / max(sums["words"], 1), 3),
            "headings": sums["headings"], "table_rows": sums["table_rows"]}


def unnumbered(markdown: str) -> str:
    """The same document with "## 4.1. Earned Leave" as "## Earned Leave"."""
    return re.sub(r"^(#{2,6})\s+\d+(?:\.\d+)*\.?\s+", r"\1 ", markdown, flags=re.MULTILINE)


def main() -> None:
    sources = sorted(POLICIES.glob("*.md")) + sorted(TABLE_DOCUMENTS.glob("*.md"))
    readers = {"pdf": {"old": old_pdf, "new": from_pdf}, "docx": {"old": old_docx, "new": from_docx}}
    makers = {"pdf": to_pdf, "docx": to_docx}
    groups = ("numbered headings", "unnumbered headings", "table document")
    results: dict[str, dict[str, list]] = {group: {f"{kind} {version}": [] for kind in readers
                                                   for version in ("old", "new")} for group in groups}
    for path in sources:
        original = path.read_text(encoding="utf-8")
        for group, markdown in (("numbered headings", original), ("unnumbered headings", unnumbered(original))):
            for kind, maker in makers.items():
                data = maker(markdown)
                for version, read in readers[kind].items():
                    result = score(markdown, read(data))
                    results[group][f"{kind} {version}"].append(result)
                    if path.parent == TABLE_DOCUMENTS and group == "numbered headings":
                        results["table document"][f"{kind} {version}"].append(result)
    report = {"documents": len(sources),
              **{group: {key: total(value) for key, value in by_reader.items()}
                 for group, by_reader in results.items()}}
    save_json(REPORTS / "extraction_check.json", report)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
