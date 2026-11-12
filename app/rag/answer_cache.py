"""Keep checked answers to first questions, so the same question on the same documents needs no model call.

A row is keyed by organisation, the answer model and its prompt, and a hash of the question's masked text after
small differences are evened out: case, spacing and the closing punctuation. Two questions worded differently are
two rows, as no similarity has been measured to be safe (see docs/DECISIONS.md, D48).

Each row also records a hash of the organisation's policy documents when it was written. A lookup only matches a
row whose hash is the current one, so an upload, a change or a removal means an old answer is never served. Only
answers that passed the citation check are kept, and only for policy questions, which never use personal data.
Rows belong to one organisation, under Row-Level Security, and are removed after the conversation retention period.
If the cache can't be read or written, the question is answered as usual.
"""
import hashlib
import json
import logging
import re

from sqlalchemy import text

from app.tenancy import TenantEngine, tenant_scoped

logger = logging.getLogger("hr_helpdesk")
DOCUMENTS_HASH = ("SELECT md5(coalesce(string_agg(source_file || ':' || content_hash || ':' || updated_at::text, ','"
                  " ORDER BY source_file), '')) AS documents_hash FROM policy_documents "
                  "WHERE tenant_id = :tenant_id AND collection_name = :collection")
LOOKUP = text(f"WITH documents AS ({DOCUMENTS_HASH}) SELECT documents.documents_hash, a.answer FROM documents "
              "LEFT JOIN answer_cache a ON a.tenant_id = :tenant_id AND a.model = :model "
              "AND a.question_hash = :question_hash AND a.documents_hash = documents.documents_hash")
STORE = text("INSERT INTO answer_cache (tenant_id, model, question_hash, documents_hash, answer) "
             "VALUES (:tenant_id, :model, :question_hash, :documents_hash, CAST(:answer AS jsonb)) "
             "ON CONFLICT (tenant_id, model, question_hash) DO UPDATE SET documents_hash = EXCLUDED.documents_hash, "
             "answer = EXCLUDED.answer, created_at = now()")
PURGE = text("DELETE FROM answer_cache WHERE tenant_id = :tenant_id "
             "AND created_at < now() - make_interval(days => :days)")
ENDING = re.compile(r"[\s?.!]+$")


def normalised(question: str) -> str:
    """The same question typed in another case, or with other spacing or closing marks, is the same question."""
    return ENDING.sub("", re.sub(r"\s+", " ", question).strip()).casefold()


def answer_key(model: str, messages: list) -> str:
    """Name the answer model and its prompt, so a new model or a changed prompt never reuses old answers."""
    prompt = "\n".join(str(message.content) for message in messages)
    return f"{model}:{hashlib.sha256(prompt.encode()).hexdigest()[:16]}"


class AnswerCache:
    def __init__(self, engine, tenant_id: str, collection: str, model: str):
        self.engine: TenantEngine = tenant_scoped(engine, tenant_id)
        self.tenant_id = tenant_id
        self.collection = collection
        self.model = model
        self.hits = 0
        self.misses = 0

    def _scope(self, question: str) -> dict:
        return {"tenant_id": self.tenant_id, "collection": self.collection, "model": self.model,
                "question_hash": hashlib.sha256(normalised(question).encode()).hexdigest()}

    def lookup(self, question: str) -> tuple[dict | None, str]:
        """Return (the cached answer or None, the current documents' hash). The hash is "" if it couldn't be read."""
        try:
            with self.engine.begin() as connection:
                row = connection.execute(LOOKUP, self._scope(question)).one()
        except Exception as error:
            logger.warning("Couldn't read the answer cache: %s", type(error).__name__)
            return None, ""
        if row.answer is None:
            self.misses += 1
            return None, row.documents_hash
        self.hits += 1
        return dict(row.answer), row.documents_hash

    def save(self, question: str, documents_hash: str, answer: dict) -> None:
        """Keep an answer, recorded against the documents it was written from, as read before it was written."""
        if not documents_hash:
            return
        try:
            with self.engine.begin() as connection:
                connection.execute(STORE, {**self._scope(question), "documents_hash": documents_hash,
                                           "answer": json.dumps(answer)})
        except Exception as error:  # only a later question loses out
            logger.warning("Couldn't save to the answer cache: %s", type(error).__name__)

    def purge_older_than(self, days: int) -> None:
        with self.engine.begin() as connection:
            connection.execute(PURGE, {"tenant_id": self.tenant_id, "days": days})
