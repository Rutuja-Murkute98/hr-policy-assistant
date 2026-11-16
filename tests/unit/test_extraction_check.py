"""The test documents and the comparison of old and new readers. No database or model is used."""
import json

import pytest

from evaluation import extraction_check
from evaluation.extraction_check import old_docx, score, total, unnumbered
from evaluation.test_documents import parse, to_docx, to_pdf
from pipeline.extract import from_docx, from_pdf

POLICY = """# Leave Policy

**Version:** 1.0 | **Effective Date:** 1 April 2026

This policy sets out leave for all employees,
written over two lines.

## 1. Earned Leave

Staff get eighteen days of earned leave each year.

### 1.1. Carry Forward

- Up to thirty days can be carried forward
- The rest lapses

## 2. Leave by Band

| Band | Days |
|---|---|
| Band 1 to Band 3 | 18 |
| Band 4 and above | 21 |
"""


def test_a_policy_is_parsed_into_its_parts():
    blocks = parse(POLICY)
    assert [(block.kind, block.level) for block in blocks if block.kind == "heading"] == [
        ("heading", 1), ("heading", 2), ("heading", 3), ("heading", 2)]
    assert any(block.text == "This policy sets out leave for all employees, written over two lines."
               for block in blocks)  # one paragraph, though wrapped
    assert [block.text for block in blocks if block.kind == "item"] == [
        "Up to thirty days can be carried forward", "The rest lapses"]
    table = next(block for block in blocks if block.kind == "table")
    assert table.rows == [["Band", "Days"], ["Band 1 to Band 3", "18"], ["Band 4 and above", "21"]]
    assert next(block for block in blocks if block.kind == "paragraph").text.startswith("Version: 1.0")  # no ** left


@pytest.mark.parametrize(("make", "read"), [(to_pdf, from_pdf), (to_docx, from_docx)])
def test_a_made_document_reads_back_with_its_headings_and_table(make, read):
    result = score(POLICY, read(make(POLICY)))
    assert result["headings_found"] == result["headings"] == 3
    assert result["rows_as_markdown_table"] == result["table_rows"] == 3
    assert result["words_found"] == result["words"]


def test_section_numbers_can_be_taken_out():
    assert unnumbered("## 4.1. Earned Leave\n### 2 Scope\n# Title\nText 3. here") == \
        "## Earned Leave\n### Scope\n# Title\nText 3. here"


def test_the_old_word_reader_keeps_headings_but_not_tables():
    result = score(POLICY, old_docx(to_docx(POLICY)))
    assert result["headings_found"] == 3 and result["rows_intact"] == 3 and result["rows_as_markdown_table"] == 0


def test_missing_headings_and_broken_rows_count_against_a_reader():
    result = score(POLICY, "Leave Policy\nBand 1 to Band 3\n18\nStaff get eighteen days")
    assert result["headings_found"] == 0 and result["rows_intact"] == 0


def test_totals_are_shares_of_everything_checked():
    first = {"headings": 4, "headings_found": 4, "table_rows": 2, "rows_intact": 1, "rows_as_markdown_table": 0,
             "words": 10, "words_found": 9}
    second = {**first, "headings_found": 0, "rows_intact": 2, "rows_as_markdown_table": 2}
    assert total([first, second]) == {"headings_found": 0.5, "table_rows_intact": 0.75,
                                      "table_rows_as_markdown_tables": 0.5, "words_found": 0.9, "headings": 8,
                                      "table_rows": 4}


def test_the_whole_check_runs_and_saves_its_report(tmp_path, monkeypatch):
    pytest.importorskip("pypdf")  # the old PDF reader's package, which the API no longer installs
    policies, tables, reports = tmp_path / "policies", tmp_path / "tables", tmp_path / "reports"
    for folder in (policies, tables, reports):
        folder.mkdir()
    (policies / "Leave.md").write_text(POLICY, encoding="utf-8")
    (tables / "Bands.md").write_text(POLICY, encoding="utf-8")
    monkeypatch.setattr(extraction_check, "POLICIES", policies)
    monkeypatch.setattr(extraction_check, "TABLE_DOCUMENTS", tables)
    monkeypatch.setattr(extraction_check, "REPORTS", reports)
    extraction_check.main()
    report = json.loads((reports / "extraction_check.json").read_text(encoding="utf-8"))
    assert report["documents"] == 2
    assert report["unnumbered headings"]["pdf new"]["headings_found"] == 1.0
    assert report["unnumbered headings"]["pdf old"]["headings_found"] == 0.0
    assert report["table document"]["docx new"]["table_rows_as_markdown_tables"] == 1.0
