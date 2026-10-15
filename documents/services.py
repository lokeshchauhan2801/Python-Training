"""Offline document extraction pipeline (Phase 1).

Ported faithfully in behavior from the OLD ``docs/services.py`` extraction
surface: the same constants, the same word-boundary overlap chunker, the same
per-format extractors, and the same best-effort OCR fallback for scanned
PDFs/images. The embedding/vector half of OLD's ``process_document`` is OMITTED
(that is Phase 2) — here ``process_document`` runs synchronously and only
produces ``DocumentChunk`` rows.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO, TextIOWrapper
from pathlib import Path

from django.db import transaction
from docx import Document as WordDocument
from openpyxl import load_workbook
from pypdf import PdfReader

from basics.llm import (
    LLMBackendError,
    LLMConfigurationError,
    get_llm_client,
)
from basics.vectorstore import ChromaVectorStore

from .models import Document, DocumentChunk

try:  # Optional OCR dependency (needs the poppler system package).
    from pdf2image import convert_from_bytes
except Exception:  # pragma: no cover - import guard
    convert_from_bytes = None

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 180
TABLE_CHUNK_SIZE = 9_000
MAX_DOCUMENT_CHUNKS = 10_000

# Embedding/vector constants (ported from OLD docs/services.py). Chunk texts are
# embedded in offset-ordered batches of EMBEDDING_BATCH_SIZE across
# EMBEDDING_WORKERS threads, then pushed into Chroma in slices of
# VECTOR_BATCH_SIZE.
EMBEDDING_BATCH_SIZE = 96
EMBEDDING_WORKERS = 4
VECTOR_BATCH_SIZE = 100
# Chroma collection every chunk vector lives in (OLD convention).
VECTOR_COLLECTION = "pdf_chunks"
SUPPORTED_FILE_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json",
    # Image formats — extracted via local OCR (Tesseract).
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

# Image extensions that are OCR'd on upload.
IMAGE_FILE_TYPES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}

# DPI used when rasterising scanned PDF pages for OCR. 300 balances accuracy
# against memory/time for typical letter-size documents.
OCR_PDF_DPI = 300
# Message shown when OCR is required but the Tesseract binary is not installed.
OCR_UNAVAILABLE_MESSAGE = (
    "This file needs OCR because it has no selectable text, but the OCR engine "
    "(Tesseract) is not installed on the server. Install 'tesseract-ocr' and "
    "'poppler-utils', then retry — or upload a text-based PDF instead."
)


class DocumentValidationError(Exception):
    """Raised when an uploaded document cannot be converted into searchable text."""


class DocumentIndexError(Exception):
    """Raised when a document cannot be persisted as searchable chunks."""


def split_page_text(text: str, page_number: int) -> list[tuple[int, str]]:
    normalized = re.sub(r"\s+", " ", text).strip()
    chunks = []
    start = 0
    while start < len(normalized):
        end = min(start + CHUNK_SIZE, len(normalized))
        if end < len(normalized):
            boundary = normalized.rfind(" ", start, end)
            if boundary > start + CHUNK_SIZE // 2:
                end = boundary
        content = normalized[start:end].strip()
        if content:
            chunks.append((page_number, content))
        if end >= len(normalized):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)
    return chunks


def _labelled_chunks(text: str, page_number: int, label: str) -> list[tuple[int, str, str]]:
    return [
        (page, f"{label} · section {index}", content)
        for index, (page, content) in enumerate(
            split_page_text(text, page_number),
            start=1,
        )
    ]


def _tesseract_available() -> bool:
    """Whether the Tesseract OCR binary is installed and runnable."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _ocr_image(image) -> str:
    """Run Tesseract OCR on a single PIL image and return extracted text."""
    import pytesseract

    return pytesseract.image_to_string(image) or ""


def _ocr_pdf(document: Document, raw: bytes | None = None) -> list[tuple[int, str, str]]:
    """OCR a scanned/image-only PDF by rasterising each page then reading text.

    Requires the Tesseract binary and Poppler (``pdf2image``). Raises a clear,
    actionable ``DocumentValidationError`` when OCR is unavailable or produces
    no text, so the failure is understandable from the Documents UI.
    """
    if not _tesseract_available():
        raise DocumentValidationError(OCR_UNAVAILABLE_MESSAGE)

    if convert_from_bytes is None:
        raise DocumentValidationError(
            "Could not rasterise this PDF for OCR. Ensure 'poppler-utils' and "
            "the 'pdf2image' package are installed on the server, then retry."
        )

    if raw is None:
        with document.file.open("rb") as pdf_file:
            raw = pdf_file.read()
    try:
        images = convert_from_bytes(raw, dpi=OCR_PDF_DPI)
    except Exception as exc:
        raise DocumentValidationError(
            "Could not rasterise this PDF for OCR. Ensure 'poppler-utils' is "
            "installed on the server, then retry."
        ) from exc

    chunks: list[tuple[int, str, str]] = []
    for page_number, image in enumerate(images, start=1):
        try:
            text = _ocr_image(image)
        finally:
            image.close()
        chunks.extend(
            (page, f"Page {page} (OCR)", content)
            for page, content in split_page_text(text, page_number)
        )

    if not chunks:
        raise DocumentValidationError(
            "OCR did not detect any readable text in this PDF. The scan may be "
            "too low quality or blank."
        )
    return chunks


def _extract_image(document: Document) -> list[tuple[int, str, str]]:
    """Extract text from an uploaded image file (PNG/JPG/TIFF) via OCR."""
    if not _tesseract_available():
        raise DocumentValidationError(OCR_UNAVAILABLE_MESSAGE)

    try:
        from PIL import Image

        with document.file.open("rb") as image_file:
            with Image.open(image_file) as image:
                image.load()
                text = _ocr_image(image)
    except DocumentValidationError:
        raise
    except Exception as exc:
        raise DocumentValidationError(
            "This image could not be read. Upload a valid PNG, JPG, or TIFF."
        ) from exc

    chunks = [
        (page, f"Image (OCR) · section {index}", content)
        for index, (page, content) in enumerate(
            split_page_text(text, 1), start=1
        )
    ]
    if not chunks:
        raise DocumentValidationError(
            "OCR did not detect any readable text in this image."
        )
    return chunks


def _extract_pdf(document: Document) -> list[tuple[int, str, str]]:
    try:
        with document.file.open("rb") as pdf_file:
            raw = pdf_file.read()
        reader = PdfReader(BytesIO(raw))
        chunks = []
        for page_number, page in enumerate(reader.pages, start=1):
            chunks.extend(
                (page, f"Page {page}", content)
                for page, content in split_page_text(
                    page.extract_text() or "",
                    page_number,
                )
            )
    except Exception as exc:
        raise DocumentValidationError(
            "This PDF could not be read. Check that it is a valid, unencrypted PDF."
        ) from exc

    # No selectable text layer → the PDF is almost certainly scanned/image-only.
    # Fall back to local OCR so scanned documents are supported automatically.
    if not chunks:
        logger.info(
            "No text layer in PDF %s; falling back to OCR.", document.pk
        )
        return _ocr_pdf(document, raw=raw)
    return chunks


def _decode_text(document: Document) -> str:
    try:
        with document.file.open("rb") as source:
            return source.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentValidationError(
            "Text files must use UTF-8 encoding."
        ) from exc


def _table_chunks(headers, rows, location_prefix: str) -> list[tuple[int, str, str]]:
    column_context = "Columns: " + " | ".join(headers)
    if len(column_context) > TABLE_CHUNK_SIZE:
        raise DocumentValidationError(
            "This table has too many or too-long column names to index safely."
        )

    output = []
    lines = []
    start_row = None
    end_row = None
    content_size = len(column_context) + len("\nRows:\n")

    def flush():
        nonlocal lines, start_row, end_row, content_size
        if not lines or start_row is None or end_row is None:
            return
        output.append(
            (
                1,
                f"{location_prefix} rows {start_row}-{end_row}",
                f"{column_context}\nRows:\n" + "\n".join(lines),
            )
        )
        lines = []
        start_row = None
        end_row = None
        content_size = len(column_context) + len("\nRows:\n")

    for row_number, row in rows:
        values = [
            str(value).strip() if value is not None and str(value).strip() else ""
            for value in row[: len(headers)]
        ]
        if len(row) > len(headers):
            values.extend(
                f"Extra column {index}: {str(value).strip()}"
                for index, value in enumerate(row[len(headers) :], start=len(headers) + 1)
            )
        if not any(values):
            continue

        row_line = f"{row_number}: " + " | ".join(values)
        row_size = len(row_line) + 1
        if len(column_context) + len("\nRows:\n") + row_size > TABLE_CHUNK_SIZE:
            flush()
            oversized_text = f"{column_context}\n{row_line}"
            fragments = split_page_text(oversized_text, page_number=1)
            output.extend(
                (
                    1,
                    f"{location_prefix} row {row_number}, part {part}",
                    fragment,
                )
                for part, (_page, fragment) in enumerate(fragments, start=1)
            )
            continue

        if lines and content_size + row_size > TABLE_CHUNK_SIZE:
            flush()
        if start_row is None:
            start_row = row_number
        end_row = row_number
        lines.append(row_line)
        content_size += row_size

    flush()
    return output


def _extract_docx(document: Document) -> list[tuple[int, str, str]]:
    try:
        with document.file.open("rb") as source:
            word_document = WordDocument(source)
            sections = [
                paragraph.text.strip()
                for paragraph in word_document.paragraphs
                if paragraph.text.strip()
            ]
            for table_number, table in enumerate(word_document.tables, start=1):
                for row_number, row in enumerate(table.rows, start=1):
                    cells = [
                        re.sub(r"\s+", " ", cell.text).strip()
                        for cell in row.cells
                    ]
                    if any(cells):
                        sections.append(
                            f"Table {table_number}, row {row_number}: "
                            + " | ".join(cells)
                        )
    except Exception as exc:
        raise DocumentValidationError(
            "This DOCX file could not be read. Check that it is a valid Word document."
        ) from exc

    if not sections:
        raise DocumentValidationError("No readable text was found in this DOCX file.")
    return _labelled_chunks("\n".join(sections), 1, "DOCX")


def _extract_csv(document: Document) -> list[tuple[int, str, str]]:
    try:
        with document.file.open("rb") as source:
            text_source = TextIOWrapper(source, encoding="utf-8-sig", newline="")
            sample = text_source.read(8192)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            text_source.seek(0)
            reader = csv.reader(text_source, dialect)
            header_row_number = 0
            headers = None
            for row_number, row in enumerate(reader, start=1):
                if not row or not any(cell.strip() for cell in row):
                    continue
                if len(row) == 1 and row[0].lstrip().startswith("#"):
                    continue
                headers = row
                header_row_number = row_number
                break
            if headers is None:
                raise DocumentValidationError(
                    "The CSV file must contain a header and at least one data row."
                )
            normalized_headers = [
                cell.strip() or f"Column {index}"
                for index, cell in enumerate(headers, 1)
            ]
            chunks = _table_chunks(
                normalized_headers,
                enumerate(reader, start=header_row_number + 1),
                "CSV",
            )
    except UnicodeDecodeError as exc:
        raise DocumentValidationError("Text files must use UTF-8 encoding.") from exc
    except csv.Error as exc:
        raise DocumentValidationError("This CSV file could not be parsed.") from exc

    if not chunks:
        raise DocumentValidationError(
            "The CSV file must contain a header and at least one data row."
        )
    return chunks


def _extract_xlsx(document: Document) -> list[tuple[int, str, str]]:
    try:
        with document.file.open("rb") as source:
            workbook = load_workbook(source, read_only=True, data_only=True)
            chunks = []
            try:
                for sheet in workbook.worksheets:
                    rows = sheet.iter_rows(values_only=True)
                    headers = next(rows, None)
                    if headers is None:
                        continue
                    labels = [
                        str(value).strip() if value is not None else f"Column {index}"
                        for index, value in enumerate(headers, 1)
                    ]
                    chunks.extend(
                        _table_chunks(
                            labels,
                            enumerate(rows, start=2),
                            f"Sheet {sheet.title}",
                        )
                    )
            finally:
                workbook.close()
    except Exception as exc:
        raise DocumentValidationError(
            "This XLSX file could not be read. Check that it is a valid Excel workbook."
        ) from exc

    if not chunks:
        raise DocumentValidationError("No readable worksheet data was found in this XLSX file.")
    return chunks


def _extract_json(document: Document) -> list[tuple[int, str, str]]:
    text = _decode_text(document)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DocumentValidationError("This JSON file contains invalid JSON.") from exc
    return _labelled_chunks(
        json.dumps(data, ensure_ascii=False, indent=2),
        1,
        "JSON",
    )


def extract_document_chunks(document: Document) -> list[tuple[int, str, str]]:
    file_type = Path(document.title).suffix.lower()
    if file_type == ".pdf":
        chunks = _extract_pdf(document)
    elif file_type == ".docx":
        chunks = _extract_docx(document)
    elif file_type in {".txt", ".md"}:
        text = _decode_text(document)
        chunks = _labelled_chunks(text, 1, file_type[1:].upper())
        if not chunks:
            raise DocumentValidationError("This file does not contain searchable text.")
    elif file_type == ".csv":
        chunks = _extract_csv(document)
    elif file_type == ".xlsx":
        chunks = _extract_xlsx(document)
    elif file_type == ".json":
        chunks = _extract_json(document)
    elif file_type in IMAGE_FILE_TYPES:
        chunks = _extract_image(document)
    else:
        raise DocumentValidationError(
            "Supported files are PDF, DOCX, TXT, MD, CSV, XLSX, JSON, and images "
            "(PNG, JPG, TIFF)."
        )

    if len(chunks) > MAX_DOCUMENT_CHUNKS:
        raise DocumentValidationError(
            f"This file is too large to index. Limit it to {MAX_DOCUMENT_CHUNKS} text chunks."
        )
    return chunks


def extract_document_text(document: Document) -> str:
    """Return the full extracted text of a document as a single string.

    Chat attachments are never embedded in the vector store, so when the chat
    needs to answer FROM an attached text/pdf/docx/image file it reads the file
    directly. This reuses the same dispatch-by-suffix extraction pipeline the
    library indexer uses (``extract_document_chunks`` — which already handles
    pdf/docx/txt/md/csv/xlsx/json and image OCR via tesseract), then joins the
    chunk bodies into one blank-line-separated string.

    A bad or edge attachment (OCR found no readable text, unsupported type, or
    an unreadable file) degrades to an empty string rather than raising, so the
    caller can simply skip a document with no usable content.
    """
    try:
        chunks = extract_document_chunks(document)
    except DocumentValidationError:
        return ""
    return "\n\n".join(content for _, _, content in chunks if content).strip()


def derive_document_title(
    document: Document,
    extracted_text: str,
    user_supplied_title: bool,
) -> str:
    """Return a title for a document, LLM-derived when the user supplied none.

    When ``user_supplied_title`` is True the current ``document.title`` is kept
    verbatim. Otherwise the LLM is asked for a concise (<=8 word) title from the
    first slice of extracted text. ANY failure — no key configured
    (``LLMConfigurationError``), a backend error, or anything else — falls back
    to the uploaded file's name stem, or the source URL host, or the current
    title. This helper NEVER raises and is import-safe/no-key.
    """
    if user_supplied_title:
        return document.title

    text = (extracted_text or "").strip()
    if text:
        try:
            raw = get_llm_client().complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "Return a concise <=8 word document title, "
                            "no quotes."
                        ),
                    },
                    {"role": "user", "content": text[:1500]},
                ]
            )
            title = (raw or "").strip().splitlines()[0].strip().strip('"').strip()
            if title:
                return title[:255]
        except (LLMConfigurationError, LLMBackendError):
            pass
        except Exception:  # noqa: BLE001 - title derivation never raises
            logger.exception(
                "Unexpected error deriving title for document %s", document.pk
            )

    # No-key / failure fallback: filename stem, then URL host, then current.
    working_name = document.file.name if document.file else ""
    stem = Path(os.path.basename(working_name)).stem if working_name else ""
    if stem:
        return stem[:255]
    if document.source_url:
        from urllib.parse import urlsplit

        host = urlsplit(document.source_url).hostname or ""
        if host:
            return host[:255]
    return document.title


def _delete_chunks(document: Document) -> None:
    """Hard-delete every chunk row for a document (incl. soft-deleted ones)."""
    DocumentChunk.all_objects.filter(document=document).hard_delete()


def _set_processing_progress(document: Document, stage: str, progress: int) -> None:
    """Persist an in-flight processing stage/progress update."""
    document.processing_stage = stage
    document.processing_progress = progress
    document.save(
        update_fields=["processing_stage", "processing_progress", "updated_at"]
    )


def _embed_chunks(extracted_chunks: list[tuple[int, str, str]]) -> list[list[float]]:
    """Embed chunk texts in offset-ordered batches across a worker pool.

    Batches of ``EMBEDDING_BATCH_SIZE`` are submitted to a
    ``ThreadPoolExecutor(max_workers=EMBEDDING_WORKERS)`` and reassembled in
    offset order so the returned vectors line up 1:1 with ``extracted_chunks``.
    A short/long batch (provider returned an incomplete result) raises
    ``DocumentIndexError``.
    """
    client = get_llm_client()
    embedding_batches = [
        (
            offset,
            [
                text
                for _page, _location, text in extracted_chunks[
                    offset : offset + EMBEDDING_BATCH_SIZE
                ]
            ],
        )
        for offset in range(0, len(extracted_chunks), EMBEDDING_BATCH_SIZE)
    ]
    batch_results: dict[int, list[list[float]]] = {}
    with ThreadPoolExecutor(max_workers=EMBEDDING_WORKERS) as executor:
        futures = {
            executor.submit(client.embed_documents, texts): offset
            for offset, texts in embedding_batches
        }
        for future in as_completed(futures):
            offset = futures[future]
            batch_embeddings = future.result()
            expected_count = min(
                EMBEDDING_BATCH_SIZE, len(extracted_chunks) - offset
            )
            if len(batch_embeddings) != expected_count:
                raise DocumentIndexError(
                    "The embedding service returned an incomplete result."
                )
            batch_results[offset] = batch_embeddings
    embeddings = [
        embedding
        for offset in sorted(batch_results)
        for embedding in batch_results[offset]
    ]
    if len(embeddings) != len(extracted_chunks):
        raise DocumentIndexError(
            "The embedding service returned an incomplete result."
        )
    return embeddings


def process_document(document: Document) -> Document:
    """Validate → extract → persist chunks → (library) embed+index → mark ready.

    Synchronous and offline (no async broker). Library documents are also
    embedded and pushed into Chroma; chat-source documents stay store/extract
    only (never embedded — Phase 3 wires that flow). On any extraction,
    validation, or embedding error the document is marked ``failed`` (with a
    user-facing message), partial chunks AND any partial vectors are removed,
    and the error is re-raised.
    """
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    document.status = Document.STATUS_PROCESSING
    document.processing_stage = "extracting"
    document.processing_progress = 5
    document.processing_error = ""
    document.save(
        update_fields=[
            "status",
            "processing_stage",
            "processing_progress",
            "processing_error",
            "updated_at",
        ]
    )

    # A user-supplied title is one that is non-blank AND already carries a file
    # suffix (the admin add form may leave title blank). When it is blank or has
    # no suffix, fall back to the uploaded file's basename so the suffix-based
    # dispatch in extract_document_chunks works; the real title is derived from
    # the extracted text after extraction.
    user_supplied_title = bool(document.title and Path(document.title).suffix)
    if not user_supplied_title and document.file:
        document.title = os.path.basename(document.file.name)

    embed = document.source == Document.SOURCE_LIBRARY
    vector_store: ChromaVectorStore | None = None
    vector_ids: list[str] = []
    try:
        extracted_chunks = extract_document_chunks(document)

        # Embed BEFORE persisting rows so a key/provider failure leaves no chunk
        # rows behind (NO-KEY path surfaces a clear, user-facing message).
        embeddings: list[list[float]] | None = None
        if embed:
            _set_processing_progress(document, "embedding", 10)
            try:
                embeddings = _embed_chunks(extracted_chunks)
            except LLMConfigurationError as exc:
                raise DocumentIndexError(
                    "LLM key required to embed library documents. Set "
                    "OPENAI_API_KEY and retry."
                ) from exc

        chunk_rows = [
            DocumentChunk(
                company=document.company,
                document=document,
                chunk_index=index,
                page_number=page_number,
                source_location=source_location,
                content=text,
            )
            for index, (page_number, source_location, text) in enumerate(
                extracted_chunks
            )
        ]

        # Purge any chunks left over from a previous (failed) attempt, then
        # insert the fresh set atomically so a crash cannot leave a half-written
        # chunk set behind.
        with transaction.atomic():
            _delete_chunks(document)
            DocumentChunk.objects.bulk_create(chunk_rows)

        if embed:
            _set_processing_progress(document, "indexing", 85)
            vector_store = ChromaVectorStore()
            for offset in range(0, len(chunk_rows), VECTOR_BATCH_SIZE):
                rows = chunk_rows[offset : offset + VECTOR_BATCH_SIZE]
                vectors = embeddings[offset : offset + VECTOR_BATCH_SIZE]
                vector_ids.extend(
                    vector_store.add_documents(
                        [
                            {
                                "id": str(chunk.pk),
                                "text": chunk.content,
                                "embedding": embedding,
                                "metadata": {
                                    "user_id": str(document.uploaded_by_id),
                                    "company_id": str(document.company_id)
                                    if document.company_id
                                    else "",
                                    "document_id": str(document.pk),
                                    "chunk_id": str(chunk.pk),
                                    "page_number": chunk.page_number,
                                    "source_location": chunk.source_location,
                                    "file_type": document.file_type,
                                },
                            }
                            for chunk, embedding in zip(rows, vectors, strict=True)
                        ],
                        collection=VECTOR_COLLECTION,
                    )
                )

        # Auto-fill file_type from the working filename's suffix when it was
        # not already set (url_source stamps it for URL docs). Then derive the
        # title from the extracted text when the user supplied none.
        working_name = document.title if user_supplied_title else (
            document.file.name if document.file else document.title
        )
        suffix = Path(working_name).suffix.lstrip(".").lower()
        if suffix:
            document.file_type = suffix
        extracted_text = "\n\n".join(
            text for _page, _loc, text in extracted_chunks if text
        ).strip()
        document.title = derive_document_title(
            document, extracted_text, user_supplied_title
        )

        document.status = Document.STATUS_READY
        document.processing_stage = "ready"
        document.processing_progress = 100
        document.chunk_count = len(chunk_rows)
        document.processing_error = ""
        document.processing_duration_ms = _elapsed_ms()
        document.save(
            update_fields=[
                "title",
                "file_type",
                "status",
                "processing_stage",
                "processing_progress",
                "chunk_count",
                "processing_error",
                "processing_duration_ms",
                "updated_at",
            ]
        )
        return document
    except (DocumentValidationError, DocumentIndexError) as exc:
        _mark_failed(document, str(exc), _elapsed_ms(), vector_store, vector_ids)
        raise
    except Exception as exc:
        message = (
            "Document extraction failed. Check that the file is valid and "
            "retry."
        )
        _mark_failed(document, message, _elapsed_ms(), vector_store, vector_ids)
        raise DocumentIndexError(message) from exc


def find_relevant_chunks(
    user, question: str, company, k: int = 5
) -> list[DocumentChunk]:
    """Return the top-``k`` chunks most relevant to ``question`` (similarity order).

    Strict owner scope: the query is filtered to ``company`` in Chroma and the
    returned hit ids are re-scoped against the relational DB with
    ``document__company__owner=user`` — a user only ever retrieves chunks from
    companies they own, even if Chroma surfaces an out-of-scope id.
    """
    query_embedding = get_llm_client().embed_query(question)
    hits = ChromaVectorStore().similarity_search(
        query_embedding,
        collection=VECTOR_COLLECTION,
        k=k,
        filter={"company_id": str(company.id)},
    )
    ranked_ids = [hit["id"] for hit in hits]
    if not ranked_ids:
        return []
    chunks = {
        str(chunk.pk): chunk
        for chunk in DocumentChunk.objects.filter(
            id__in=ranked_ids,
            document__company__owner=user,
        ).select_related("document")
    }
    return [chunks[chunk_id] for chunk_id in ranked_ids if chunk_id in chunks]


def _mark_failed(
    document: Document,
    message: str,
    elapsed_ms: int,
    vector_store: ChromaVectorStore | None = None,
    vector_ids: list[str] | None = None,
) -> None:
    # Best-effort removal of any partial vectors written before the failure.
    if vector_store is not None and vector_ids:
        try:
            vector_store.delete_documents(vector_ids, collection=VECTOR_COLLECTION)
        except Exception:  # pragma: no cover - best effort
            logger.exception(
                "Could not remove partial vectors for document %s", document.pk
            )
    _delete_chunks(document)
    document.status = Document.STATUS_FAILED
    document.processing_stage = "failed"
    document.processing_progress = 0
    document.chunk_count = 0
    document.processing_error = message
    document.processing_duration_ms = elapsed_ms
    document.save(
        update_fields=[
            "status",
            "processing_stage",
            "processing_progress",
            "chunk_count",
            "processing_error",
            "processing_duration_ms",
            "updated_at",
        ]
    )
