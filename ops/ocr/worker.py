"""The OCR worker: reads scanned PDF pages with Tesseract, on a machine you run. See docs/DECISIONS.md, D58.

    docker compose --profile ocr up --build

The API sends it a PDF and the numbers of the pages with no text. It renders those pages, reads them, and returns each
line with its position and height in points, so the API finds headings by size, as for a normal PDF. Nothing is stored.
Set OCR_TOKEN on both sides so only the API can use it.
"""
import hmac
import io
import os
import statistics
from collections import defaultdict

from fastapi import FastAPI, Header, HTTPException, Request

DPI = 300  # enough for Tesseract on printed text
POINTS_PER_PIXEL = 72 / DPI
MAX_BYTES = 10_000_000
MAX_PAGES = 50  # pages read in one request
MIN_CONFIDENCE = 30  # Tesseract's 0 to 100 score: below this, a "word" is usually a speck or a line of a table

app = FastAPI(title="OCR worker", docs_url=None, redoc_url=None)


def lines_from_data(data: dict, scale: float = POINTS_PER_PIXEL) -> list[dict]:
    """Tesseract's words, as image_to_data gives them, grouped into lines with their text, position and height."""
    words: dict[tuple, list[int]] = defaultdict(list)
    for index, text in enumerate(data["text"]):
        if text.strip() and float(data["conf"][index]) >= MIN_CONFIDENCE:
            words[(data["block_num"][index], data["par_num"][index], data["line_num"][index])].append(index)
    lines = []
    for indexes in words.values():
        top = min(data["top"][index] for index in indexes)
        bottom = max(data["top"][index] + data["height"][index] for index in indexes)
        indexes.sort(key=lambda index: data["left"][index])
        lines.append({"text": " ".join(data["text"][index].strip() for index in indexes),
                      "top": round(top * scale, 1), "bottom": round(bottom * scale, 1),
                      # The tallest words show the font size best: short ones like "a" or "on" have no ascenders
                      "size": round(statistics.median(sorted(data["height"][index] for index in indexes)[-3:])
                                    * scale, 1)})
    return sorted(lines, key=lambda line: line["top"])


def read_pages(pdf: bytes, pages: list[int]) -> dict[str, list[dict]]:
    import pypdfium2
    import pytesseract

    document = pypdfium2.PdfDocument(io.BytesIO(pdf))
    try:
        result = {}
        for number in pages:
            if not 0 <= number < len(document):
                continue
            image = document[number].render(scale=DPI / 72).to_pil()
            data = pytesseract.image_to_data(image, lang="eng", output_type=pytesseract.Output.DICT)
            result[str(number)] = lines_from_data(data)
        return result
    finally:
        document.close()


def check_token(given: str) -> None:
    expected = os.environ.get("OCR_TOKEN", "")
    if expected and not hmac.compare_digest(given, expected):
        raise HTTPException(401, "Wrong or missing OCR token")


@app.get("/health")
def health():
    import pytesseract

    return {"status": "ok", "tesseract": str(pytesseract.get_tesseract_version())}


@app.post("/ocr")
async def ocr(request: Request, pages: str = "", x_ocr_token: str = Header(default="")):
    """Read the given pages, such as pages=0,2, of the PDF in the request body."""
    check_token(x_ocr_token)
    body = await request.body()
    if len(body) > MAX_BYTES or not body.startswith(b"%PDF-"):
        raise HTTPException(422, "Send a PDF of up to 10 MB")
    try:
        wanted = sorted({int(page) for page in pages.split(",") if page.strip()})
    except ValueError:
        raise HTTPException(422, "Pages are numbers, such as 0,2") from None
    if not wanted or len(wanted) > MAX_PAGES:
        raise HTTPException(422, f"Ask for between 1 and {MAX_PAGES} pages")
    try:
        return {"pages": read_pages(body, wanted)}
    except Exception:
        raise HTTPException(422, "The PDF couldn't be read") from None
