# Build Your Own HR Helpdesk RAG — A Beginner's Guide

**Who this is for:** A junior intern / fresher who knows basic Python but has never built an AI project.
**What you will build:** A chatbot that answers employee questions like *"How many leaves do I get?"* using the 33 Rutuja Talent Nexus HR policy documents in `data/policies/`.
**Time needed:** ~4 weeks, a few hours a day. You will build it in small steps, and every step produces something you can run and see working.

---

## Part 1 — Understand What You Are Building (Day 1-2)

### The problem

Employees ask HR the same questions every day: *"What is my notice period?"*, *"Can I work from home?"*, *"How do I claim insurance?"*. All the answers already exist in the policy documents — but nobody reads 33 documents.

### The naive idea, and why it fails

"Just send the question to ChatGPT/Gemini!" — but the LLM has never seen Rutuja Talent Nexus's policies. It will either say "I don't know" or, worse, **make up an answer** (this is called *hallucination*). An HR bot that invents leave rules is dangerous.

"Then paste all 33 documents into the prompt!" — the documents are ~280 KB of text. That is slow, expensive, and mostly irrelevant to any single question.

### The RAG idea

**RAG = Retrieval-Augmented Generation.** Instead of sending everything, we first *find* the 4-6 paragraphs that are actually relevant to the question, then send *only those* to the LLM with the instruction: "Answer ONLY from this text."

```
Question: "How many earned leaves do I get?"
        |
        v
[1. RETRIEVE]  Search all policy chunks --> find the "Earned Leave" section
        |
        v
[2. AUGMENT]   Build a prompt:  "Context: <that section>  Question: <question>"
        |
        v
[3. GENERATE]  LLM writes the answer using only the context
        |
        v
Answer: "Confirmed employees get 18 days of Earned Leave per calendar year..."
```

### The one concept you must really understand: embeddings

A computer cannot compare "leaves" and "vacation" as words — they share no letters. An **embedding model** converts any text into a list of numbers (a *vector*) such that texts with similar *meaning* get similar numbers. "How many leaves do I get?" and "Earned Leave entitlement is 18 days" end up close together in vector space, even though the words differ.

So the plan is:
1. Convert every policy paragraph into a vector **once** and store them (this is *indexing*).
2. When a question comes in, convert it into a vector too, and find the stored vectors closest to it (this is *similarity search*).

A **vector database** is just a database that is good at "find the closest vectors". This project uses PostgreSQL with the **pgvector** extension.

**Checkpoint ✅:** Explain RAG to a friend in 2 minutes without notes. If you can't, re-read this part.

---

## Part 2 — Setup (Day 3)

1. **Python 3.12+** installed, and an editor (VS Code).
2. Create a project folder and a virtual environment:
   ```bash
   mkdir my_hr_rag && cd my_hr_rag
   python -m venv venv
   venv\Scripts\activate        # Windows
   pip install langchain langchain-google-genai langchain-postgres langchain-community langchain-text-splitters python-dotenv fastapi uvicorn streamlit
   ```
3. **Get a free Gemini API key** from Google AI Studio (aistudio.google.com) → "Get API key". Put it in a `.env` file:
   ```
   GOOGLE_API_KEY=your_key_here
   ```
   **Never** commit `.env` to git. Add it to `.gitignore` immediately.
4. **Run PostgreSQL with pgvector** using Docker:
   ```bash
   docker run -d --name pgvector -e POSTGRES_PASSWORD=postgres -p 5432:5432 pgvector/pgvector:pg16
   ```
5. Copy the `data/policies/` folder (the 33 `.md` files) into your project.

**Checkpoint ✅:** `python -c "import langchain; print('ok')"` prints ok, and `docker ps` shows the pgvector container running.

---

## Part 3 — Your First Working RAG in ~40 Lines (Week 1)

Before building anything fancy, make the smallest thing that works, end to end. Type this yourself (don't copy-paste — typing is how you learn what each line does):

```python
# mini_rag.py — the whole idea in one file
import os
from dotenv import load_dotenv
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from langchain_postgres import PGVector

load_dotenv()

# 1. LOAD the policy documents
docs = DirectoryLoader("data/policies", glob="**/*.md",
                       loader_cls=TextLoader,
                       loader_kwargs={"encoding": "utf-8"}).load()
print(f"Loaded {len(docs)} documents")

# 2. CHUNK them into retrievable pieces
splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
chunks = splitter.split_documents(docs)
print(f"Created {len(chunks)} chunks")

# 3. EMBED + STORE the chunks in the vector database (the slow part, do once)
embeddings = GoogleGenerativeAIEmbeddings(model="models/embedding-001")
store = PGVector.from_documents(
    chunks, embeddings, collection_name="hr_policies",
    connection="postgresql+psycopg://postgres:postgres@localhost:5432/postgres")

# 4. ASK: retrieve relevant chunks, then generate an answer from them
question = "How many earned leaves do I get per year?"
relevant = store.similarity_search(question, k=4)
context = "\n\n".join(c.page_content for c in relevant)

llm = ChatGoogleGenerativeAI(model="gemini-2.0-flash")
prompt = f"""Answer the question using ONLY the context below.
If the answer is not in the context, say "I don't know."

Context:
{context}

Question: {question}"""
print(llm.invoke(prompt).content)
```

Run it. When it prints *"...18 days of Earned Leave per calendar year..."*, you have built a RAG system. 🎉

**Exercises (do these before moving on):**
- Ask 10 different questions. Find 2 where the answer is wrong or missing.
- Change `k=4` to `k=1` and `k=10`. What changes and why?
- Ask something NOT in the policies ("what is the capital of France?"). Does it say "I don't know"? This is why the prompt says *ONLY the context*.

---

## Part 4 — Make Each Stage Better, One at a Time (Week 2-3)

The mini version works but has weaknesses. The real project in this repo fixes them stage by stage. Study each stage, then rebuild it in your own project.

### Stage A: Smarter chunking (`pipeline/chunk_documents.py`)

**Problem:** `RecursiveCharacterTextSplitter` cuts blindly every 1000 characters — sometimes mid-sentence, mixing two unrelated sections.
**Fix:** Our policy documents have numbered headings (`## 4.1. Earned Leave`). Split on *headings* so each chunk is one coherent section, and attach **metadata** to every chunk: which file it came from, which heading, a unique chunk id. Metadata lets you later (a) show sources to the user and (b) filter search to one policy.
**Read the real code:** `pipeline/chunk_documents.py` — see how it splits on headings first, and only uses the character splitter for oversized sections.

### Stage B: A proper indexing script (`pipeline/embed_and_store.py`)

Indexing (embedding all chunks) is slow and costs API calls, so it should be a **separate script you run once**, not something the app does at startup. The app then just *connects* to the already-filled database. This separation — offline indexing pipeline vs online query service — is how every production RAG works.

### Stage C: Better retrieval (`app/rag/service.py`)

Three upgrades over plain `similarity_search`:
- **Score threshold:** every result comes with a distance score. Ignore chunks that are too far away — bad context creates bad answers.
- **MMR (Maximal Marginal Relevance):** plain search often returns 4 near-identical chunks. MMR picks results that are relevant *and different from each other*, covering more ground.
- **Deduplication and filtering:** drop empty/heading-only chunks, and never send the same chunk twice.
Read `_retrieve_pairs()` in `app/rag/service.py` — it looks long, but it is just these ideas applied carefully.

### Stage D: A strict prompt (`app/rag/prompt.py`)

Read the real prompt. Notice it: gives the bot a role, forbids outside knowledge, and gives an *exact* fallback sentence for "not found". For an HR bot, refusing to answer is a feature — a made-up leave policy is worse than no answer.

### Stage E: Wrap it in an API (`app/main.py`)

A script only you can run isn't a product. **FastAPI** turns it into a web service:

```python
from fastapi import FastAPI
app = FastAPI()

@app.post("/ask")
def ask(question: str):
    ...retrieve + generate...
    return {"answer": answer, "sources": sources}
```

Run with `uvicorn app.main:app --reload`, then open `http://localhost:8000/docs` — FastAPI gives you a free testing UI. Study in the real `app/main.py`: the `/health` endpoint, request logging with a request id, API-key checks, and rate limiting (`app/security.py`).

### Stage F: A simple UI (`ui/streamlit_app.py`)

Streamlit gives a chat UI in ~50 lines of Python. Important design rule: the UI calls the FastAPI backend over HTTP — it does **not** import the RAG code. One brain (the API), many faces (UI, Slack bot, whatever comes later).

### Stage G: Evaluation — how do you KNOW it works? (`evaluation/rag_evaluation.py`)

"It looked fine when I tried it" is not engineering. Build a test set of ~20 questions with known correct answers (you already found some in Part 3!). Then measure with **RAGAS**:
- **Faithfulness** — did the answer only use the retrieved context? (catches hallucination)
- **Context recall / precision** — did retrieval fetch the right chunks, and only the right chunks?

Run the eval after every change. If a change makes answers worse, the numbers tell you before your users do.

---

## Part 5 — Map: Guide Stage → Real Repo File

| Stage | Concept | File in this repo |
|---|---|---|
| A | Heading-aware chunking + metadata | `pipeline/chunk_documents.py` |
| B | Offline indexing into pgvector | `pipeline/embed_and_store.py` |
| C | Retrieval: MMR, thresholds, dedupe, routing | `app/rag/service.py`, `app/rag/router.py` |
| D | Strict grounded prompt | `app/rag/prompt.py` |
| E | API, auth, rate limit, logging | `app/main.py`, `app/security.py`, `app/config.py` |
| F | Chat UI calling the API | `ui/streamlit_app.py` |
| G | RAGAS evaluation | `evaluation/rag_evaluation.py` |
| — | Tests | `tests/unit/`, `tests/integration/` |

Suggested reading order: A → B → D → C (hardest) → E → F → G.

---

## Part 6 — Common Beginner Errors

| Symptom | Likely cause |
|---|---|
| `401 / API key not valid` | `.env` not loaded (`load_dotenv()` missing) or wrong key |
| `connection refused` on 5432 | Postgres container not running — `docker ps`, then `docker start pgvector` |
| Answers are generic / ignore policies | Retrieval returned nothing useful — print the retrieved chunks and look at them; debug retrieval before blaming the LLM |
| Bot answers questions it shouldn't | Prompt not strict enough; lower temperature (~0.1), tighten instructions |
| Right answer exists but isn't retrieved | Chunking cut the section badly, or `k` too small — inspect chunks for that policy |
| Everything is slow | You are re-embedding all documents on every run — index once (Stage B) |

**Golden debugging rule:** when the answer is wrong, ALWAYS look at the retrieved chunks first. 80% of RAG bugs are retrieval bugs, not LLM bugs.

---

## Part 7 — Glossary (interview-ready)

- **LLM** — Large Language Model (Gemini, GPT, Claude). Generates text from a prompt.
- **Hallucination** — the LLM confidently making things up. RAG + strict prompts reduce it.
- **Embedding** — text converted to a vector of numbers capturing its meaning.
- **Vector database** — stores embeddings and finds the nearest ones fast (here: pgvector).
- **Chunking** — splitting documents into retrievable pieces.
- **Metadata** — data about a chunk (source file, heading, id) used for filtering and citations.
- **Similarity search / MMR** — finding the closest chunks / closest *and diverse* chunks.
- **Grounding** — forcing the answer to come only from the retrieved context.
- **RAGAS / Faithfulness / Context recall** — evaluation framework and its core metrics.
- **Indexing vs querying** — the offline "build the database" phase vs the online "answer a question" phase.

Questions to be able to answer at the end (these are real interview questions):
1. Why RAG instead of fine-tuning the model on the policies?
2. Why do we chunk documents at all, and why by headings?
3. What happens if retrieval returns wrong chunks? How would you detect it?
4. Why is the prompt told to say "I don't know"?
5. How would you measure that your change made the system better?

---

## Part 8 — Milestone Checklist

- [ ] Week 1: `mini_rag.py` answers leave-policy questions correctly end to end
- [ ] Week 2: separate chunking + indexing scripts with metadata; retrieval shows sources (file + heading)
- [ ] Week 2: strict prompt with "I don't know" fallback; verified on off-topic questions
- [ ] Week 3: FastAPI `/ask` + `/health` working in the `/docs` playground
- [ ] Week 3: Streamlit chat UI talking to the API
- [ ] Week 4: 20-question test set + RAGAS scores recorded before/after one improvement
- [ ] Week 4: README written in your own words; demo the project to someone

When every box is ticked, you haven't just used AI — you've built, served, and *measured* an AI system. That is exactly the skill set GenAI engineering interviews look for. Good luck! 🚀
