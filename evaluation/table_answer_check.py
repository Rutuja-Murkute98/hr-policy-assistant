"""Answer questions from a PDF whose facts are in tables, read by the old and the new PDF reader.

    python -m evaluation.table_answer_check

The test document of tables is made into a PDF, read and chunked. Search judges a chunk against the rest of its
organisation's chunks, so the document is indexed for a throwaway organisation that also holds copies of the sample
policies' stored vectors, and everything under that organisation is deleted afterwards. Each question's answer is in one
table row. It uses the real models: about 4 embedding calls and 24 answer calls. See docs/DECISIONS.md, D57.
"""
import json
from contextlib import contextmanager

from sqlalchemy import text

from app import config
from app.db import get_engine
from app.rag.providers import get_chat_model
from app.rag.retrieval import SearchFilters
from app.rag.service import RagService
from app.rag.store import PolicyIndex, get_database_uri, get_store, run_migrations
from evaluation.extraction_check import TABLE_DOCUMENTS, old_pdf, unnumbered
from evaluation.metrics import facts_present
from evaluation.run_eval import REPORTS, call_with_retry, save_json
from evaluation.test_documents import to_pdf
from pipeline.chunk_documents import chunk_text
from pipeline.extract import finish, from_pdf

DOCUMENT = TABLE_DOCUMENTS / "Benefits_by_Band_Summary.md"
CHECK_TENANT = "tablecheck"  # a throwaway organisation: everything under it is deleted afterwards
QUESTIONS = [
    ("What is the room rent limit per day for Band 4 to Band 5 employees?", ["5,000"]),
    ("How much is the yearly training budget for Band 6 and above?", ["75,000"]),
    ("What referral bonus is paid for a Band 7 role?", ["75,000"]),
    ("What is the maximum salary advance for Band 4 to Band 5?", ["1.5"]),
    ("How many days of paternity leave are there?", ["15"]),
    ("How long is maternity leave, and how long must someone have worked to get it?", ["26 weeks", "80 days"]),
]


def clear(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM langchain_pg_embedding WHERE cmetadata->>'tenant_id' = :tenant"),
                           {"tenant": CHECK_TENANT})
        for table in ("embedding_models", "policy_documents"):
            connection.execute(text(f"DELETE FROM {table} WHERE tenant_id = :tenant"), {"tenant": CHECK_TENANT})


@contextmanager
def check_tenant():
    """An organisation holding copies of the sample policies' chunks and vectors, removed afterwards."""
    engine = get_engine(get_database_uri())
    clear(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO langchain_pg_embedding (id, collection_id, embedding, document, cmetadata) "
            "SELECT :tenant || '::' || e.id, e.collection_id, e.embedding, e.document, "
            "e.cmetadata || jsonb_build_object('tenant_id', CAST(:tenant AS text)) "
            "FROM langchain_pg_embedding e JOIN langchain_pg_collection c ON e.collection_id = c.uuid "
            "WHERE c.name = :collection AND COALESCE(e.cmetadata->>'tenant_id', :default) = :default "
            "AND COALESCE(e.cmetadata->>'is_current', 'true') = 'true'"
        ), {"tenant": CHECK_TENANT, "collection": config.COLLECTION_NAME, "default": config.TENANT_ID})
    try:
        yield engine
    finally:
        clear(engine)


def run(reader_name: str, read, numbered: bool = True) -> dict:
    """Index the PDF as this reader sees it, alongside the sample policies, and ask every question."""
    source = DOCUMENT.read_text(encoding="utf-8")
    pdf = to_pdf(source if numbered else unnumbered(source))
    markdown = finish(read(pdf), "Benefits_by_Band_Summary.pdf", None)
    chunks = chunk_text(markdown, "Benefits_by_Band_Summary.md", tenant_id=CHECK_TENANT)
    with check_tenant():
        index = PolicyIndex(get_store(), CHECK_TENANT)
        index.add(chunks)
        rag = RagService(index, get_chat_model())
        rows = []
        for question, facts in QUESTIONS:
            result = call_with_retry(lambda question=question: rag.ask(question, [], SearchFilters(CHECK_TENANT)))
            from_table = any("Benefits_by_Band_Summary" in str(source.get("chunk_id", "")) for source in result.sources)
            rows.append({"question": question, "facts": facts, "right": facts_present(result.answer, facts),
                         "answered": result.answered, "cited_the_table_document": from_table,
                         "answer": result.answer})
            print(f"{reader_name}: {rows[-1]['right']} | {question} | {' '.join(result.answer.split())[:120]}",
                  flush=True)
        return {"chunks": len(chunks), "sections": len({chunk.metadata["section"] for chunk in chunks}),
                "right": sum(row["right"] for row in rows),
                "cited_the_table_document": sum(row["cited_the_table_document"] for row in rows),
                "asked": len(rows), "rows": rows}


def main() -> None:
    run_migrations()
    report = {"old reader (pypdf)": run("old", old_pdf), "new reader (pdfplumber)": run("new", from_pdf),
              "old reader, unnumbered headings": run("old unnumbered", old_pdf, numbered=False),
              "new reader, unnumbered headings": run("new unnumbered", from_pdf, numbered=False)}
    save_json(REPORTS / "table_answer_check.json", report)
    print(json.dumps({name: f"{result['right']} of {result['asked']} right, {result['cited_the_table_document']} "
                            f"citing the document, {result['sections']} sections"
                      for name, result in report.items()}, indent=1))


if __name__ == "__main__":
    main()
