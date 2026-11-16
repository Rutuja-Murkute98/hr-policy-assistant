# Phase 12 report: better documents and more sources

Branch `phase-12-documents`, built on `phase-11-personas`, 16 November 2026. Phase 12 is the plan's largest, in three
parts. **Part A1 is built and measured:** PDFs are read with `pdfplumber`, so headings come from the document's own fonts
and tables come out as Markdown tables, and Word tables do too. **Part A2 is built and tested, except Docling:** scanned
pages are read by a Tesseract worker you run, off unless set up, and HR can preview how a file is read beside its pages.
Tesseract hasn't run on this machine, as it isn't installed, so a real scan is only read in CI. Part B (retrieval
experiments) and part C (connectors, contradictions, change summaries and review dates) are still to do.

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

## Phase 12 status

| Item from the plan | Status | Evidence or gap |
|---|---|---|
| DOCX heading styles for sections | Implemented and tested | Built-in and based-on styles. Done before, extended here |
| PDF tables as Markdown tables with `pdfplumber`, measured on policies with tables | Implemented and measured | Above |
| OCR for scanned PDFs as an optional worker | Implemented and tested | Tesseract only, by the owner's choice. Real scans only read in CI |
| Docling as an optional local extractor, compared with the default | Not started | Waits for the owner: over a gigabyte to download |
| Side-by-side view of the original and the extracted sections | Implemented and tested | Not clicked in a browser |
| Small-to-big, contextual headers, query decomposition, each measured | Planned | Part B. Needs reviewed labels and quota |
| Connectors, scheduled sync, contradictions, change summaries, review dates | Planned | Part C |

## What the owner must do

1. Check the GitHub Actions run for this branch: the real Tesseract test and the OCR worker's image only run there.
2. Decide whether to try Docling: it needs PyTorch and over a gigabyte of downloads, for a comparison only.
3. To read scans yourself, install Docker, then: `OCR_URL=http://ocr:8080 docker compose --profile ocr up --build`.
