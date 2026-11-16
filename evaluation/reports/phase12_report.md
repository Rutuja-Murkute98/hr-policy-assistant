# Phase 12 report: better documents and more sources

Branch `phase-12-documents`, built on `phase-11-personas`, 16 November 2026. Phase 12 is the plan's largest, in three
parts. **Part A1 is built and measured:** PDFs are read with `pdfplumber`, so headings come from the document's own fonts
and tables come out as Markdown tables, and Word tables do too. **Part A2 is built and tested, except Docling:** scanned
pages are read by a Tesseract worker you run, off unless set up, and HR can preview how a file is read beside its pages.
Tesseract hasn't run on this machine, as it isn't installed, so a real scan is only read in CI. **Part C1 is built and
tested:** policies sync from a watched folder, Google Drive or SharePoint, on a schedule and through the same path as an
upload. Drive and SharePoint haven't been called for real, as there are no credentials. Part B (retrieval experiments) and
part C2 (contradictions, change summaries and review dates) are still to do.

## Baseline

The `phase11` run is the baseline, recorded on the code this phase started from: recall@5 0.943, expected facts 0.912,
correct refusals 0.925. Part A1 changes only how uploads are read. The sample policies are Markdown files, which aren't
read by the changed code, so the golden set's answers can't change and it wasn't run again.

## What changed

| File | Change | Why |
|---|---|---|
| `pipeline/extract.py` | PDFs read with `pdfplumber`: a line larger than the body text, or bold and short, is a heading, and tables become Markdown tables. A table carried onto the next page with its header repeated is joined up. A PDF whose fonts show no headings falls back to the old numbered-line guess | D57 |
| `pipeline/extract.py` | Word: a custom heading style based on a built-in one counts as a heading, tables become Markdown tables, a one-cell box stays text, and bullets stay bullets | |
| `requirements-api.txt`, `requirements-dev.txt` | `pdfplumber` replaces `pypdf`. `fpdf2` for development only, to make test PDFs | No PyTorch |
| `evaluation/test_documents.py` | Makes a PDF, with heading fonts and drawn tables, and a Word file, with heading styles and real tables, from any policy's Markdown | So both readers are compared on the same files |
| `evaluation/data/table_documents/Benefits_by_Band_Summary.md` | A test document of six tables: room rent, training budget, salary advance, referral bonus and probation by band, and leave | None of the 33 sample policies has a table. Every figure is from those policies |
| `evaluation/extraction_check.py`, `evaluation/table_answer_check.py` | The measurements below | |

## Reading documents, measured

`evaluation/extraction_check.py` makes every sample policy and the table document into a PDF and a Word file, reads
them back with the old and new readers, and compares the result with the Markdown it came from. It uses no model. Each
document is also made with its section numbers taken out, as many real policies have none.

| 34 documents | Headings found | Table rows kept together | Table rows as Markdown tables | Words found |
|---|---:|---:|---:|---:|
| PDF, old reader | 1.000 | 0.926 | 0 | 1.000 |
| **PDF, new reader** | 1.000 | **1.000** | **1.000** | 1.000 |
| PDF without section numbers, old reader | **0.000** | 0.926 | 0 | 1.000 |
| **PDF without section numbers, new reader** | **1.000** | 1.000 | 1.000 | 1.000 |
| Word, old reader | 1.000 | 1.000 | 0 | 1.000 |
| **Word, new reader** | 1.000 | 1.000 | **1.000** | 1.000 |

There are 500 headings and 27 table rows. Without section numbers, the old reader found no headings at all, so a whole
policy became one section called "Content".

## Answering from a PDF of tables

`evaluation/table_answer_check.py` reads the table document's PDF, indexes it next to the 33 sample policies for a
throwaway organisation, asks the real model six questions whose answers are each in one table row, then deletes
everything under that organisation. 24 answer calls.

| Reader | Right | Citing the table document | Sections |
|---|---:|---:|---:|
| Old | 6 of 6 | 6 | 6 |
| New | 6 of 6 | 6 | 6 |
| Old, without section numbers | 6 of 6 | 6 | **1** |
| New, without section numbers | 6 of 6 | 6 | 6 |

- **The answers were equally right.** On these clean PDFs, the old reader's table rows read as loose words were still
  enough. The plan's "a PDF with tables can be uploaded and answered from correctly" holds for both.
- **What the new reader adds is structure.** Without section numbers, the old reader's citations all say "Content", where
  the new one's say "Hospital Room Rent". Phase 11's tags, and the review screen in part A2, work section by section.
- **The first try of this check was wrong:** in a collection of its own, six chunks can't stand out from each other, so
  every answer was a refusal for both readers. A chunk is judged against its organisation's other chunks, so it has to
  sit with the real policies.

## Tests

| Run | Result |
|---|---|
| New tests | 8 for the readers: font headings without numbers, a bold subheading, a PDF table, a table over two pages, a password-protected PDF, a scanned PDF, a Word heading style based on a built-in one, Word tables, boxes and lists |
| Existing extraction tests | All 23 pass. Their hand-made PDFs have no fonts, and fall back to the old guess |
| More new tests | 8 for the measurement tools: parsing, a round trip through both readers, scoring, and a whole run on a temporary folder |
| Full suite with coverage | 965 passed. Coverage 86.75%. It fell to 83.90%, below CI's floor of 85%, until the measurement tools had tests. `pipeline/extract.py` 98% |
| ruff, mypy | Passed |

## Not done, or not checked

- **These PDFs are clean.** They were made by a program, with one font and drawn table lines. Scanned pages, two-column
  layouts and tables without lines haven't been tried, and real HR files will have them.
- **The test document is mine,** made from the policies' own figures, and not reviewed by the owner.
- **No answer was checked through the upload screen.** The readers are the same code the upload uses, and the integration
  tests upload files, but nobody uploaded a PDF in a browser.

## Part A2: scanned PDFs and checking before publishing

The owner chose local OCR only: no page is sent to a cloud service (D58).

| File | Change | Why |
|---|---|---|
| `ops/ocr/worker.py`, `ops/ocr/Dockerfile` | A small web app in its own container, with Tesseract. It renders the pages it's asked for, reads them, and returns each line's text, position and height in points. It checks a shared token, and refuses files over 10 MB or 50 pages. Nothing is stored | Too heavy for the free API host, and a scan can hold personal data |
| `pipeline/ocr.py`, `pipeline/extract.py`, `app/config.py` | With `OCR_URL` set, only a PDF's pages with no text of their own go to the worker. OCR line heights are grouped into bands and scaled to the PDF's own body text, so headings are found by size. With it empty, the default, a scan is refused as before | A scan inside a normal PDF is common |
| `docker-compose.yml`, `.env.example` | The worker as an `ocr` profile, with no ports open: only the API reaches it | Off unless asked for |
| `.github/workflows/ci.yml`, `requirements-dev.txt` | CI installs Tesseract, so a test reads a real scan there, and builds and health-checks the worker's image | Tesseract isn't on this machine |
| `app/knowledge.py`, `app/admin.py`, `ui/pages/admin.py` | `POST /admin/documents/preview`, and a "Preview how it's read" button on the Upload tab: the sections, with any too short to search, beside images of a PDF's first 8 pages. Nothing is stored. The file's words are shown as plain text | The plan's side-by-side check before publishing |

### Tests for part A2

| Run | Result |
|---|---|
| OCR | 14 unit tests, with a stand-in for Tesseract: words to lines in points, size bands, headings found by size, only scanned pages sent, a page with text never sent, a worker that doesn't answer, a scan with nothing readable, the client's token and pages, and the worker's token and limits. 1 more reads a real scan with Tesseract, skipped here and run in CI |
| Full suite with coverage | 982 passed and 1 skipped, the real Tesseract test. Coverage 86.77%. `pipeline/ocr.py` 100%, `ops/ocr/worker.py` 73%: the part that calls Tesseract runs only in CI |
| Preview | 3 tests on real PostgreSQL through the API: a PDF's sections and page images with nothing stored, a Word file with no page images, admins only, and bad files refused |

### Found while building part A2

- **The body text size for scanned pages was wrong when there were as many headings as body lines.** It was the size
  with the most lines. Like the PDF's own text, it's now the size with the most characters.
- **Three type errors from Phase 11** were in the persona evaluation tools, so CI's type check fails on the pushed
  `phase-11-personas`. They're fixed on this branch.

### Not done in part A2

- **Docling isn't compared.** It brings PyTorch and downloads models from Hugging Face on first use, over a gigabyte for a
  comparison, so it waits for the owner's agreement.
- **Tesseract's real accuracy isn't measured.** Its one real test runs only in CI, where I can't read the results. A
  scan's tables come out as lines of text, as Tesseract doesn't find tables.
- **The preview wasn't clicked in a browser.** The screen's test tool can't choose a file.

## Part C1: syncing from where HR keeps policies

| File | Change | Why |
|---|---|---|
| `app/connectors/` | One interface, `list_files` and `fetch`, and three sources behind it. A watched folder must be inside `SOURCES_ROOT/<organisation>`, and links out of it are skipped. Google Drive is read with a service account, and Google Docs come out as Word files. SharePoint is read through Microsoft Graph with an app registration. Both use `httpx` and `PyJWT`, which the API already has | D59 |
| `migrations/versions/0014_knowledge_sources.py`, `app/sources.py` | Each organisation's sources, and each file seen at a source with its version, all with Row-Level Security. A sync fetches only new or changed files and gives each one to the upload path, so versions, jobs and only embedding changed sections work as for an upload. A file gone from the source stops being answered from, unless the source keeps it | |
| `app/source_scheduler.py`, `app/main.py` | A thread in the API syncs the sources that are due, each claimed in the database first so two processes never run one. Organisations with no sources are skipped without building their workflow | The plan's sync on a schedule |
| `app/admin.py`, `ui/pages/admin.py` | Routes and a Sources tab: the kinds this server can read, add, pause, sync now, stop syncing, and each source's files. Every change is audited. Removing a source keeps its policies | |
| `app/config.py`, `.env.example` | `SOURCES_ROOT`, the Drive and SharePoint credentials, and `SOURCE_SYNC_SECONDS`. Each kind is off until its settings are filled in | The owner decides what the server can reach |

### Tests for part C1

| Run | Result |
|---|---|
| Connectors | 21 unit tests. Drive and SharePoint against stand-ins that answer as their APIs do: the signed token, paging, Google Docs exported as Word files, the token reused, and a missing folder or refused account explained. The folder: files listed with versions that change, nothing read outside it, and a folder outside the organisation's refused. A Drive folder id that could change Drive's query is refused. 1 more, a link out of the folder, is skipped here, as Windows needs extra rights to make one |
| Syncing | 9 tests on real PostgreSQL through the API: new, changed and removed files, keeping removed ones, a broken file reported while the rest sync, a folder outside the organisation's refused, one organisation's sources hidden from another, auditing, no sync started twice, the scheduler, and a source that can't be reached marked failed |

### Not done in part C1

- **Google Drive and SharePoint haven't been called for real.** There are no credentials for this project, so they're
  tested against my reading of their documented replies. The first real use should be watched.
- **The Sources tab wasn't clicked in a browser.**
- **The same file name in two sources is one policy,** so the later sync's copy becomes a new version of it.

## Part C2: clashes, what changed, and review dates

| File | Change | Why |
|---|---|---|
| `app/policy_checks.py`, `migrations/versions/0015_policy_checks.py` | After each upload or sync, three checks run, each on its own so one failing never fails the upload. **Clashes:** a sentence that states a figure, such as days, an amount or years, is paired with any sentence elsewhere that shares at least 3 of its main words, and half the shorter one's, but states a different figure. Those pairs of sections go to the answer model 15 at a time, and it says whether both can be true for the same employee. Every verdict is kept in `policy_pairs` against hashes of both texts. **What changed:** the model gets only the sections that changed, were added or were removed, and writes two or three sentences. **Review dates:** read from a policy's "Next Review:" line, or given on the upload form | Contradicting policies give contradicting answers, and a policy past its review date may be out of date. See D60 |
| `app/admin.py`, `ui/pages/admin.py` | `GET` and `PATCH /admin/clashes`, `POST /admin/clashes/check`, and a Clashes tab: both sections and the model's reason, marked fixed or not a clash. The Policies tab shows the next review and warns of overdue ones, and each version shows what changed | HR decides, not the model |
| `pipeline/chunk_documents.py`, `pipeline/embed_and_store.py`, `app/knowledge.py`, `app/rag/store.py` | The review date is read with or without bold markers, and stored with each version and policy | |
| `evaluation/conflict_check.py` | Checks every sample policy with the real model, and counts the five known contradictions found | The measurement below |

### Finding contradictions, measured

The five contradictions listed in `docs/FINAL_REPORT.md` were put into the sample policies on purpose. All 539 sections
were checked with the real answer model.

| Way of choosing pairs | Pairs of sections | Known contradictions in the pairs | Found by the model |
|---|---:|---:|---:|
| Each section's 4 nearest by stored vector, both with a figure | 1,526 | 3 of 5. Two sat 10th and 14th nearest | Not run |
| **Sentences sharing words, with different figures** | **352** | **5 of 5** | **5 of 5** |

The model raised 12 clashes in all: the 5 known, and 7 more, which I read against the policies:

| Raised | My reading |
|---|---|
| Maternity cover: Rs. 75,000 a delivery in the Compensation policy, Rs. 50,000 in the Health Insurance policy | **A real contradiction, not on the known list** |
| Laptop ready 3 working days before joining for remote staff (Onboarding), 2 days for everyone (IT Assets) | Arguable. Shipping needs longer, but the two don't mention each other |
| Employer PF contribution, rating 1 and the bonus range, probation letter timing, review outcome timing (two) | Not clashes: different things that share words |

So every known contradiction was found, plus one real one nobody had listed, and 5 of the 12 were false alarms that HR
dismisses with one click, after which they're never raised again unless a text changes. It took about 24 answer-model
calls. The first attempt stopped at its 8th call when Gemini returned 503, "high demand". A failed batch is now skipped and
asked about next time, and as verdicts are kept, the second attempt only asked about the 245 pairs left.

### Tests for part C2

| Run | Result |
|---|---|
| Unit | 8 tests: only sentences with a figure, an amount in rupees isn't a sentence break, alike and not alike sentences, word endings, the model sees headings and matched sentences fenced, only changed sections sent for a summary, spacing alone needs none, review dates with and without bold |
| Through the API | 6 tests on real PostgreSQL with a stand-in model: two contradicting policies flagged, a dismissed clash not asked about again until a text changes and then reopened, a new version's summary, review dates from the document or the form and the overdue flag, one organisation's clashes hidden from another and from employees, and a model failure never failing the upload |
| Admin screen | A clash shows both sections, and Fixed sends the decision |
| Isolation | `policy_pairs` added to the table lists: it has Row-Level Security like the others |

### Not done in part C2

- **The Clashes tab wasn't clicked in a browser.**
- **The 7 other clashes are my reading, not HR's.** The maternity amounts should be fixed in the sample policies, or added
  to the known list.
- **Only figures are compared.** A clash in words alone, such as "allowed" against "not allowed", isn't looked for.
- **A change summary is the model's.** It's shown as written, beside the version, and not checked.

## Phase 12 status

| Item from the plan | Status | Evidence or gap |
|---|---|---|
| DOCX heading styles for sections | Implemented and tested | Built-in and based-on styles. Done before, extended here |
| PDF tables as Markdown tables with `pdfplumber`, measured on policies with tables | Implemented and measured | Above |
| OCR for scanned PDFs as an optional worker | Implemented and tested | Tesseract only, by the owner's choice. Real scans only read in CI |
| Docling as an optional local extractor, compared with the default | Not started | Waits for the owner: over a gigabyte to download |
| Side-by-side view of the original and the extracted sections | Implemented and tested | Not clicked in a browser |
| Small-to-big, contextual headers, query decomposition, each measured | Planned | Part B. Needs reviewed labels and quota |
| A `Connector` interface, a watched folder and Google Drive, SharePoint behind a setting, with a fake | Implemented and tested | Drive and SharePoint against stand-ins only |
| Sync on a schedule through the job table, embedding only changed sections | Implemented and tested | Through the upload path and its indexing jobs |
| Contradictions, change summaries, review dates | Implemented, tested and measured | 5 of 5 known contradictions found with the real model, and 1 more. Not clicked in a browser |

## What the owner must do

1. Check the GitHub Actions run for this branch: the real Tesseract test and the OCR worker's image only run there.
2. Decide whether to try Docling: it needs PyTorch and over a gigabyte of downloads, for a comparison only.
3. To read scans yourself, install Docker, then: `OCR_URL=http://ocr:8080 docker compose --profile ocr up --build`.
4. Decide what to do about the maternity amounts, Rs. 75,000 against Rs. 50,000: fix a sample policy, or add it to the
   known contradictions.
5. Phase 12 changed `app/config.py` and `app/rag/store.py`, which the quality gate fingerprints, so it needs a new
   200-question run, about 400 answer calls, before the phase is complete.
