"""Ask the OCR worker to read a PDF's scanned pages. It runs on a machine you run, never a cloud service: see D58."""
import httpx

from app import config


class OcrUnavailable(Exception):
    """The worker couldn't read the pages. The message is safe to show the person uploading."""


def ocr_enabled() -> bool:
    return bool(config.OCR_URL)


def read_scanned_pages(pdf: bytes, pages: list[int]) -> dict[int, list[dict]]:
    """Each page's lines, with their text, top, bottom and size in points, from the worker."""
    try:
        response = httpx.post(f"{config.OCR_URL}/ocr", content=pdf, params={"pages": ",".join(map(str, pages))},
                              headers={"Content-Type": "application/pdf", "X-OCR-Token": config.OCR_TOKEN},
                              timeout=config.OCR_TIMEOUT_SECONDS)
        response.raise_for_status()
        found = response.json()["pages"]
    except (httpx.HTTPError, ValueError, KeyError) as error:
        raise OcrUnavailable("The scanned pages couldn't be read: the OCR worker didn't answer. "
                             "Please try again later, or upload a PDF with text.") from error
    return {int(number): lines for number, lines in found.items()}
