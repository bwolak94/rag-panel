# TASK-018: OCR Support for Scanned Documents

**Status:** TODO
**Priority:** P1 — blocks indexing of scanned PDFs (common in medical clinics)
**Owner:** rag-engineer + backend-dev
**Reviewer:** python-reviewer
**Related docs:** `docs/architecture.md`, `docs/prd.md` §FR-2, `docs/roadmap.md` Phase 3
**Estimated effort:** 5–7 days

---

## Overview

Medical clinics frequently scan paper documents (referrals, handwritten notes, old protocols) into image-only PDFs. The current ingest pipeline rejects these as "low quality" because text extraction yields near-empty output.

This task adds an OCR stage to the ingest graph: detect image-only pages → run OCR (Tesseract via `pytesseract` or `docling` OCR engine) → inject extracted text back into the pipeline → continue normal chunking and embedding.

OCR is expensive (CPU/GPU). It runs **only** when `node_extract` detects that a page has < 50 characters of extractable text per page (heuristic). The decision is per-document and recorded in `ingestion_jobs.steps`.

---

## Usage

**Trigger:** `node_extract` sets `state.needs_ocr = True` when extracted text is sparse.

**Supported inputs:** PDF (image-only or mixed), PNG, JPEG, TIFF.

**Typical flow:**
1. Worker processes uploaded `skierowanie-skan.pdf`.
2. `node_extract` gets 3 chars/page → sets `needs_ocr=True`.
3. New `node_ocr` runs Tesseract on each page image.
4. Extracted text replaces sparse output; pipeline continues normally.
5. `ingestion_jobs.steps` records `{"stage": "ocr", "meta": {"engine": "tesseract", "pages": 4, "lang": "pol"}}`.

---

## Tech Stack

- **OCR engine:** `pytesseract` (Tesseract 5.x) — Polish + English language packs; fallback to `docling` OCR for complex layouts
- **PDF rasterization:** `pdf2image` (poppler backend) — rasterize at 300 DPI
- **Node:** `src/graphs/ingest_graph/nodes/node_ocr.py`
- **Config:** `collections.chunk_config` extended with `ocr_enabled: bool` (default `True`) and `ocr_lang: str` (default `"pol+eng"`)
- **Worker:** OCR runs in a `ProcessPoolExecutor` (CPU-bound) — never blocks the event loop
- **Langfuse:** trace OCR duration and page count per document

---

## Ingest Graph Change

```
node_fetch → node_extract → [node_ocr if needs_ocr] → node_validate → node_pii_scan → node_upsert
```

Routing in `src/graphs/ingest_graph/routing.py` — add edge: `after_extract → ocr | validate` based on `state.needs_ocr`.

**State extension (`src/graphs/ingest_graph/state.py`):**
```python
needs_ocr: bool = False
ocr_text: str | None = None  # populated by node_ocr
ocr_engine: str | None = None
ocr_page_count: int = 0
```

---

## Database / Config Changes

- `collections` table: add `ocr_enabled BOOLEAN DEFAULT TRUE` and `ocr_lang VARCHAR(20) DEFAULT 'pol+eng'`
- Alembic migration: `0008_ocr_collection_config.py`
- `ingestion_jobs.steps` — OCR stage added between extract and validate

---

## Implementation Steps

1. Add `ocr_enabled`, `ocr_lang` columns to `collections` (Alembic migration).
2. Create `src/graphs/ingest_graph/nodes/node_ocr.py`:
   - Accept `IngestState` with `needs_ocr=True`
   - Rasterize PDF pages via `pdf2image`
   - Run `pytesseract.image_to_string()` per page in `ProcessPoolExecutor`
   - Set `state.ocr_text`, `state.ocr_engine`, `state.ocr_page_count`
3. Modify `node_extract.py` — after extraction, if `chars_per_page < 50`, set `state.needs_ocr = True`.
4. Update `routing.py` — conditional edge `node_extract → node_ocr | node_validate`.
5. Update `node_validate.py` — use `state.ocr_text or state.extracted_text`.
6. Add Langfuse span `ocr` around OCR execution.
7. Update `docs/02-Architektura.md` graph diagram.

---

## Security

- OCR output is treated as untrusted content (prompt injection risk) — same guardrails as normal extraction.
- `ocr_text` never appears in application logs.
- Large image PDFs (>100 MB) are rejected at upload validation — existing size limit applies.

---

## Tests

**Unit (`tests/unit/ingest_graph/test_node_ocr.py`):**
- OCR node extracts text from a test image-only PDF fixture
- `needs_ocr=False` → node is skipped (routing test)
- OCR runs in executor, not blocking event loop
- `ocr_lang` from collection config is passed to Tesseract

**Integration:**
- Upload a scanned PDF → document reaches `status=ready` with OCR metadata in ingestion steps
- Scanned document is searchable via chat

---

## Definition of Done

- [ ] `node_ocr.py` implemented and tested in isolation (no LLM needed)
- [ ] Routing updated; `needs_ocr=False` skips OCR with zero overhead
- [ ] Alembic migration for `ocr_enabled`, `ocr_lang`
- [ ] OCR runs in `ProcessPoolExecutor` (async-safe)
- [ ] Langfuse trace includes OCR span with duration and page count
- [ ] `docs/02-Architektura.md` graph diagram updated
- [ ] Integration test: scanned PDF → `status=ready`
- [ ] `/skill /ingest-stage` checklist completed
