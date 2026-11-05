"""Stage A: split the policy documents into one chunk per heading.

Each chunk is one coherent section, so a chunk never mixes two unrelated topics.
Only sections too long for one chunk are cut further, and every piece keeps its
heading path, such as "Leave Policy > 4. Types of Leave > 4.1. Earned Leave".

Run it to see the chunks without any API calls:
    venv\\Scripts\\python pipeline\\chunk_documents.py
"""
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app import config

POLICY_DIR = config.POLICY_DIR
MAX_SECTION_CHARS = 1200  # a longer section is cut into smaller pieces

HEADING = re.compile(r"^(#{1,3}) +(.+?) *$", re.MULTILINE)
AT_A_GLANCE = re.compile(r"^\*\*At a Glance:\*\* *(.+)$", re.MULTILINE)
VERSION = re.compile(r"\*\*Version:\*\* *([^|\n]+?) *(?:\||$)", re.MULTILINE)
EFFECTIVE_DATE = re.compile(r"\*\*Effective Date:\*\* *([^|\n]+?) *(?:\||$)", re.MULTILINE)

long_section_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1000,
    chunk_overlap=150,
    separators=["\n\n", "\n", ". ", " "],  # prefer paragraph, then sentence, breaks
    keep_separator="end",
)


def split_into_sections(text: str) -> Iterator[tuple[list[str], str]]:
    """Yield (heading path, text under that heading) for every heading."""
    matches = list(HEADING.finditer(text))
    path: list[str] = []
    for i, match in enumerate(matches):
        level = len(match[1])
        path = path[:level - 1] + [match[2]]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield path, text[match.end():end].strip()


def document_details(text: str, file_name: str) -> dict[str, str | None]:
    """Read the details that apply to every chunk of a document."""
    version, effective = VERSION.search(text), EFFECTIVE_DATE.search(text)
    effective_date = None
    if effective:
        try:
            effective_date = datetime.strptime(effective[1], "%d %B %Y").date().isoformat()
        except ValueError:
            pass  # a date in another format is left out, not guessed
    return {
        "tenant_id": config.TENANT_ID,
        "category": config.tenant.document_categories.get(file_name, config.tenant.default_category),
        "version": version[1] if version else None,
        "effective_date": effective_date,
    }


def chunk_document(file: Path) -> list[Document]:
    """Turn one policy file into chunks with metadata."""
    return chunk_text(file.read_text(encoding="utf-8"), file.name)


def chunk_text(text: str, file_name: str) -> list[Document]:
    """Turn the text of one policy into chunks with metadata."""
    stem = Path(file_name).stem
    details = document_details(text, file_name)
    chunks = []
    for headings, body in split_into_sections(text):
        if len(headings) == 1:
            # Under the title: keep the summary, skip the company and version lines
            glance = AT_A_GLANCE.search(body)
            body = glance[1].strip() if glance else ""
            headings = headings + ["At a Glance"]
        if not body:
            continue  # e.g. a heading whose text is all in its subsections

        parts = [body] if len(body) <= MAX_SECTION_CHARS else long_section_splitter.split_text(body)
        heading_path = " > ".join(headings)
        for part_number, part in enumerate(parts):
            chunk_id = f"{stem} > {' > '.join(headings[1:])} > {part_number}"
            chunks.append(Document(
                id=chunk_id,
                # Including the headings tells the search and the LLM what this text is about
                page_content=f"{heading_path}\n\n{part.strip()}",
                metadata={
                    "source": file_name,
                    "policy": headings[0],
                    "section": " > ".join(headings[1:]),
                    "chunk_id": chunk_id,
                    **details,
                },
            ))
    return chunks


def load_chunks(policy_dir: Path = POLICY_DIR) -> list[Document]:
    """Chunk every policy file in the folder."""
    return [chunk for file in sorted(policy_dir.glob("*.md")) for chunk in chunk_document(file)]


if __name__ == "__main__":
    all_chunks = load_chunks()
    sizes = sorted(len(chunk.page_content) for chunk in all_chunks)
    print(f"{len(all_chunks)} chunks from {len({c.metadata['source'] for c in all_chunks})} documents")
    print(f"Chunk size: smallest {sizes[0]}, median {sizes[len(sizes) // 2]}, largest {sizes[-1]}")
    print("\nExample chunk:\n")
    print(next(c for c in all_chunks if c.metadata["section"].startswith("4. Eligibility")).page_content)
