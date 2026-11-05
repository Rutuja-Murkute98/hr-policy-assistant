"""Checks for Stage A chunking. None of these call an API.

Run from the project folder:  venv\\Scripts\\python -m pytest tests -q
"""
import re

import pytest

from pipeline.chunk_documents import MAX_SECTION_CHARS, POLICY_DIR, chunk_document, document_details, load_chunks

SAMPLE = """# Sample Policy

**Company:** Example Pvt. Ltd.
**Version:** 2.0

**At a Glance:** A short summary.

## 1. Purpose

Why this policy exists.

## 2. Types of Leave

### 2.1. Earned Leave

18 days per year.

### 2.2. Sick Leave

12 days per year.
"""


@pytest.fixture
def sample_chunks(tmp_path):
    file = tmp_path / "Sample_Policy.md"
    file.write_text(SAMPLE, encoding="utf-8")
    return chunk_document(file)


@pytest.fixture(scope="module")
def real_chunks():
    return load_chunks()


def test_one_chunk_per_section_and_empty_headings_skipped(sample_chunks):
    assert [c.metadata["section"] for c in sample_chunks] == [
        "At a Glance",
        "1. Purpose",
        "2. Types of Leave > 2.1. Earned Leave",
        "2. Types of Leave > 2.2. Sick Leave",
    ]


def test_company_and_version_lines_are_left_out(sample_chunks):
    assert sample_chunks[0].page_content == "Sample Policy > At a Glance\n\nA short summary."


def test_chunk_text_starts_with_its_full_heading_path(sample_chunks):
    assert sample_chunks[2].page_content == (
        "Sample Policy > 2. Types of Leave > 2.1. Earned Leave\n\n18 days per year."
    )


def test_metadata(sample_chunks):
    assert sample_chunks[2].metadata == {
        "source": "Sample_Policy.md",
        "policy": "Sample Policy",
        "section": "2. Types of Leave > 2.1. Earned Leave",
        "chunk_id": "Sample_Policy > 2. Types of Leave > 2.1. Earned Leave > 0",
        "tenant_id": "default",
        "category": "general",
        "version": "2.0",
        "effective_date": None,
    }


def test_version_and_effective_date_are_read_from_the_header():
    text = "# A\n\n**Version:** 3.1 | **Effective Date:** 1 April 2026 | **Next Review:** 1 April 2027\n\n## 1. B\n\nText."
    assert document_details(text, "Leave_Policy.md") == {
        "tenant_id": "default", "category": "leave", "version": "3.1", "effective_date": "2026-04-01",
    }


def test_a_date_in_another_format_is_left_out():
    assert document_details("**Effective Date:** soon", "x.md")["effective_date"] is None


def test_every_real_chunk_has_a_category_and_effective_date(real_chunks):
    for chunk in real_chunks:
        assert chunk.metadata["tenant_id"] == "default"
        assert chunk.metadata["category"] != "general" or chunk.metadata["source"] in {
            "About_Rutuja_Talent_Nexus.md", "Employee_FAQ.md", "HR_Glossary.md"}
        assert chunk.metadata["effective_date"] == "2026-04-01"


def test_long_section_is_cut_and_every_piece_keeps_its_heading(tmp_path):
    sentence = "Employees must follow this rule at all times. "
    file = tmp_path / "Long.md"
    file.write_text(f"# Long\n\n## 1. Rules\n\n{sentence * 60}", encoding="utf-8")
    chunks = chunk_document(file)
    assert len(chunks) > 1
    assert len({c.id for c in chunks}) == len(chunks)
    for chunk in chunks:
        assert chunk.page_content.startswith("Long > 1. Rules\n\nEmployees")
        assert chunk.page_content.endswith("at all times.")


def test_real_policies_all_have_chunks(real_chunks):
    sources = {c.metadata["source"] for c in real_chunks}
    assert sources == {file.name for file in POLICY_DIR.glob("*.md")}
    assert len(sources) == 33


def test_real_chunk_ids_are_unique(real_chunks):
    ids = [c.id for c in real_chunks]
    assert len(ids) == len(set(ids))


def test_real_chunks_have_text_and_a_sensible_size(real_chunks):
    for chunk in real_chunks:
        _, _, body = chunk.page_content.partition("\n\n")
        assert body.strip(), chunk.id
        assert len(body) <= MAX_SECTION_CHARS, chunk.id


def test_no_policy_text_is_lost(real_chunks):
    for file in POLICY_DIR.glob("*.md"):
        text = file.read_text(encoding="utf-8")
        body = text[text.index("**At a Glance:**"):]
        chunked = " ".join(c.page_content for c in real_chunks if c.metadata["source"] == file.name)
        missing = set(re.findall(r"\w+", body)) - set(re.findall(r"\w+", chunked))
        assert not missing, f"{file.name}: {sorted(missing)[:10]}"
