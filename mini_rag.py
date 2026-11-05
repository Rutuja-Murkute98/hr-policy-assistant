# mini_rag.py: the whole RAG idea in one file
#
# Differences from the guide's Part 3 code:
#   - PostgreSQL runs inside Python (pixeltable-pgserver), because Docker can't run on this PC
#   - Uses current Gemini model names, because the guide's models have been shut down
#   - Reads from policies/policies, which is where the documents actually are
#   - Chunks by heading (Stage A) and shows which section each answer came from
#   - Indexes slowly enough for the free-tier limit (100 chunks per minute)
#   - Embeds only chunks not stored yet, so later runs don't repeat the API calls
#     and an interrupted run carries on where it stopped
import os
import sys
import time
from pathlib import Path

import pixeltable_pgserver as pgserver
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_postgres import PGVector

from pipeline.chunk_documents import load_chunks

load_dotenv()
if os.getenv("GOOGLE_API_KEY", "your_key_here") == "your_key_here":
    sys.exit("Add your Gemini API key to the .env file first.")

# Start PostgreSQL with pgvector. The data is kept in the pgdata folder.
server = pgserver.get_server(Path("pgdata").resolve())
connection = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)

embeddings = GoogleGenerativeAIEmbeddings(model="gemini-embedding-2")
store = PGVector(embeddings, collection_name="hr_policies_by_section", connection=connection)

# 1 + 2. LOAD the policy documents and CHUNK them by heading
chunks = load_chunks()
print(f"Created {len(chunks)} chunks")

# 3. EMBED + STORE only the chunks that aren't in the database yet (the slow part)
stored_ids = {doc.id for doc in store.get_by_ids([chunk.id for chunk in chunks])}
missing = [chunk for chunk in chunks if chunk.id not in stored_ids]
print(f"Already stored: {len(stored_ids)}, still to embed: {len(missing)}")

BATCH_SIZE = 80      # stays under the free-tier limit of 100 chunks per minute
WAIT_SECONDS = 65
for start in range(0, len(missing), BATCH_SIZE):
    if start > 0:
        print(f"Waiting {WAIT_SECONDS} seconds for the rate limit...", flush=True)
        time.sleep(WAIT_SECONDS)
    batch = missing[start:start + BATCH_SIZE]
    store.add_documents(batch, ids=[chunk.id for chunk in batch])
    print(f"Embedded {start + len(batch)}/{len(missing)}", flush=True)

# 4. ASK: retrieve relevant chunks, then generate an answer from them
question = sys.argv[1] if len(sys.argv) > 1 else "How many earned leaves do I get per year?"
relevant = store.similarity_search(question, k=4)
context = "\n\n".join(c.page_content for c in relevant)

llm = ChatGoogleGenerativeAI(model="gemini-3.5-flash-lite")
prompt = f"""Answer the question using ONLY the context below.
If the answer is not in the context, say "I don't know."

Context:
{context}

Question: {question}"""

print(f"\nQuestion: {question}")
print(f"Answer: {llm.invoke(prompt).text}")
print("\nSources:")
for source in sorted({f"{c.metadata['policy']} > {c.metadata['section']}" for c in relevant}):
    print(f"  - {source}")
