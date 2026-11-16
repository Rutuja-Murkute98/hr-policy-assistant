# The OCR worker: Tesseract reads scanned PDF pages on a machine you run. See docs/DECISIONS.md, D58.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY ops/ocr/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ops/ocr/worker.py .

# Not root, as it reads files people upload
RUN useradd --create-home ocr
USER ocr
EXPOSE 8080
CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
