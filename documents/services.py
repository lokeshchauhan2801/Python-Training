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
from collections.abc import Callable
from io import BytesIO, TextIOWrapper
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from docx import Document as WordDocument
from openpyxl import load_workbook
from pypdf import PdfReader

from basics.llm import (
    LLMBackendError,
    LLMConfigurationError,
    get_llm_client,
)
from basics.vectorstore import ChromaVectorStore

from .models import Document, DocumentChunk, DocumentStage

try:
    from pdf2image import convert_from_bytes
except Exception:
    convert_from_bytes = None

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 180
TABLE_CHUNK_SIZE = 9_000
MAX_DOCUMENT_CHUNKS = 10_000

EMBEDDING_BATCH_SIZE = 96
EMBEDDING_WORKERS = 4
VECTOR_BATCH_SIZE = 100
VECTOR_COLLECTION = "pdf_chunks"
SUPPORTED_FILE_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json",

    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

IMAGE_FILE_TYPES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}

OCR_PDF_DPI = 300
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


def resolve_file_extension(document: Document) -> str:
    """Return the document's extraction extension (e.g. ``".pdf"``), robustly.

    The extraction pipeline dispatches by file type, so it needs a RELIABLE
    extension. Historically this was taken from ``document.title`` — but titles
    are LLM-derived free text (e.g. "Confirmation of Employment for Mr. Lokesh
    Chauhan"), and ``Path(title).suffix`` happily returns garbage like
    ``". Lokesh Chauhan"`` whenever the title contains a dot, or ``""`` when it
    has no extension at all. Reprocessing such a document then failed with the
    "Supported files" error even though the underlying file was a valid PDF.

    Resolution order (most to least reliable):
      1. The uploaded file's own name suffix (``document.file.name``).
      2. The stored ``document.file_type`` (stamped on first processing / by the
         URL downloader), normalised to a leading-dot extension.
      3. The ``source_url`` path suffix for URL-sourced documents.
      4. The title suffix — only as a last resort, and only when it maps to a
         known supported type (so free-text titles can't masquerade as one).
    """

    if document.file and document.file.name:
        suffix = Path(document.file.name).suffix.lower()
        if suffix in SUPPORTED_FILE_TYPES:
            return suffix


    stored = (document.file_type or "").strip().lower()
    if stored:
        candidate = stored if stored.startswith(".") else f".{stored}"

        if candidate in SUPPORTED_FILE_TYPES:
            return candidate


    if document.source_url:
        from urllib.parse import urlsplit

        url_suffix = Path(urlsplit(document.source_url).path).suffix.lower()
        if url_suffix in SUPPORTED_FILE_TYPES:
            return url_suffix


    title_suffix = Path(document.title or "").suffix.lower()
    if title_suffix in SUPPORTED_FILE_TYPES:
        return title_suffix

    return ""


def extract_document_chunks(document: Document) -> list[tuple[int, str, str]]:
    file_type = resolve_file_extension(document)
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
        except Exception:
            logger.exception(
                "Unexpected error deriving title for document %s", document.pk
            )


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


def purge_document(document: Document) -> None:
    """Permanently remove a document and ALL of its data.

    Unlike the soft delete (which only flips ``is_deleted``), this removes the
    real, user-visible artefacts so a deleted document actually disappears:

      1. Chroma vectors for every chunk (keyed by the deterministic per-chunk id
         ``f"{document_id}:{chunk_index}"``) in ``pdf_chunks``.
      2. Every ``DocumentChunk`` row (incl. soft-deleted ones).
      3. The uploaded file from storage (best effort).
      4. The ``Document`` row itself (hard delete — real SQL DELETE).

    Each external cleanup step is best-effort (a missing Chroma collection or an
    already-removed file never blocks the row deletion), but the DB rows are
    always removed so the changelist reflects the deletion.
    """


    vector_ids = [
        f"{document.pk}:{chunk_index}"
        for chunk_index in DocumentChunk.all_objects.filter(
            document=document
        ).values_list("chunk_index", flat=True)
    ]
    if vector_ids:
        try:
            ChromaVectorStore().delete_documents(
                vector_ids, collection=VECTOR_COLLECTION
            )
        except Exception:
            logger.exception(
                "Could not remove vectors for document %s during purge", document.pk
            )


    _delete_chunks(document)


    if document.file:
        try:
            document.file.delete(save=False)
        except Exception:
            logger.exception(
                "Could not remove file for document %s during purge", document.pk
            )


    document.hard_delete()


def _set_processing_progress(document: Document, stage: str, progress: int) -> None:
    """Persist an in-flight processing stage/progress update."""
    document.processing_stage = stage
    document.processing_progress = progress
    document.save(
        update_fields=["processing_stage", "processing_progress", "updated_at"]
    )


def _embed_chunks(
    extracted_chunks: list[tuple[int, str, str]],
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[list[float]]:
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
            if progress_callback is not None:
                progress_callback(len(batch_results), len(embedding_batches))
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


    file_basename = os.path.basename(document.file.name) if document.file else ""
    user_supplied_title = bool(document.title) and document.title != file_basename

    embed = document.source == Document.SOURCE_LIBRARY
    vector_store: ChromaVectorStore | None = None
    vector_ids: list[str] = []
    try:
        extracted_chunks = extract_document_chunks(document)
        _set_processing_progress(document, "extracting", 25)


        embeddings: list[list[float]] | None = None
        if embed:
            _set_processing_progress(document, "embedding", 30)

            def update_embedding_progress(completed_batches, total_batches):
                progress = 30 + int(45 * completed_batches / total_batches)
                _set_processing_progress(document, "embedding", progress)

            try:
                embeddings = _embed_chunks(
                    extracted_chunks,
                    progress_callback=update_embedding_progress,
                )
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


        with transaction.atomic():
            _delete_chunks(document)
            DocumentChunk.objects.bulk_create(chunk_rows)

        if embed:
            _set_processing_progress(document, "indexing", 75)
            vector_store = ChromaVectorStore()
            total_batches = max(
                1,
                (len(chunk_rows) + VECTOR_BATCH_SIZE - 1) // VECTOR_BATCH_SIZE,
            )
            for batch_index, offset in enumerate(
                range(0, len(chunk_rows), VECTOR_BATCH_SIZE), start=1
            ):
                rows = chunk_rows[offset : offset + VECTOR_BATCH_SIZE]
                vectors = embeddings[offset : offset + VECTOR_BATCH_SIZE]
                vector_ids.extend(
                    vector_store.add_documents(
                        [
                            {


                                "id": f"{document.pk}:{chunk.chunk_index}",
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
                progress = 75 + int(23 * batch_index / total_batches)
                _set_processing_progress(document, "indexing", progress)


        resolved_suffix = resolve_file_extension(document).lstrip(".").lower()
        if resolved_suffix:
            document.file_type = resolved_suffix
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


RAG_RELEVANCE_MAX_DISTANCE = 0.60


def _relevance_max_distance() -> float:
    from django.conf import settings

    try:
        return float(
            getattr(settings, "RAG_RELEVANCE_MAX_DISTANCE", RAG_RELEVANCE_MAX_DISTANCE)
        )
    except (TypeError, ValueError):
        return RAG_RELEVANCE_MAX_DISTANCE


def find_relevant_chunks(
    user, question: str, company, k: int = 5
) -> list[DocumentChunk]:
    """Return the top-``k`` chunks most relevant to ``question`` (similarity order).

    Active-company scope: the query is filtered to ``company`` in Chroma and the
    returned hit ids are re-scoped against the relational DB to that same
    ``company`` — but only after authorizing the user against it on the project's
    single authorization axis (a **superuser** may act within any active company,
    otherwise the user must **own** the company). This mirrors admin changelists,
    the active-company switcher, and the eligibility guard, all of which already
    treat a superuser as able to act within the resolved active company. The
    previous ``document__company__owner=user`` re-scope ignored the superuser
    bypass and silently dropped every Chroma hit for a superuser acting within a
    company they did not personally own.

    RELEVANCE FILTER: Chroma returns the k NEAREST vectors regardless of whether
    any are actually on-topic, so hits whose cosine ``distance`` exceeds
    ``_relevance_max_distance()`` are discarded here. An off-topic question thus
    returns ``[]`` (no spurious citations) and the caller falls back to a general
    answer; a genuinely relevant question keeps its close hits unchanged.
    """

    if not (getattr(user, "is_superuser", False) or company.owner_id == user.pk):
        return []
    query_embedding = get_llm_client().embed_query(question)
    hits = ChromaVectorStore().similarity_search(
        query_embedding,
        collection=VECTOR_COLLECTION,
        k=k,
        filter={"company_id": str(company.id)},
    )


    max_distance = _relevance_max_distance()


    ranked_ids = [
        hit["metadata"]["chunk_id"]
        for hit in hits
        if (hit.get("distance") is None or hit["distance"] <= max_distance)
        and hit.get("metadata")
        and hit["metadata"].get("chunk_id")
    ]
    if not ranked_ids:
        return []
    chunks = {
        str(chunk.pk): chunk
        for chunk in DocumentChunk.objects.filter(
            id__in=ranked_ids,
            document__company=company,
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

    if vector_store is not None and vector_ids:
        try:
            vector_store.delete_documents(vector_ids, collection=VECTOR_COLLECTION)
        except Exception:
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


STAGE_MARK_PROCESSING = "mark_processing"
STAGE_EXTRACT_TEXT = "extract_text"
STAGE_CHUNK_TEXT = "chunk_text"
STAGE_DELETE_EXISTING_CHUNKS = "delete_existing_chunks"
STAGE_EMBED_AND_STORE_CHUNKS = "embed_and_store_chunks"
STAGE_MARK_COMPLETED = "mark_completed"
STAGE_NOTIFY_COMPLETED = "notify_completed"

DOCUMENT_STAGES = (
    STAGE_MARK_PROCESSING,
    STAGE_EXTRACT_TEXT,
    STAGE_CHUNK_TEXT,
    STAGE_DELETE_EXISTING_CHUNKS,
    STAGE_EMBED_AND_STORE_CHUNKS,
    STAGE_MARK_COMPLETED,
    STAGE_NOTIFY_COMPLETED,
)


def _ensure_stages(document: Document) -> None:
    """Idempotently create the canonical DocumentStage rows for a document.

    Only creates rows that do not already exist (so it is safe to call on every
    stage entry); never resets existing rows.
    """
    existing = set(
        DocumentStage.all_objects.filter(document=document).values_list(
            "name", flat=True
        )
    )
    missing = [
        DocumentStage(
            company=document.company,
            document=document,
            name=name,
            order=order,
        )
        for order, name in enumerate(DOCUMENT_STAGES)
        if name not in existing
    ]
    if missing:
        DocumentStage.objects.bulk_create(missing)


def _reset_stages(document: Document) -> None:
    """Re-arm ALL stage rows to pending, clearing started/finished/detail.

    Called by ``stage_mark_processing`` on (re)entry so a reprocess restarts the
    whole pipeline from a clean slate.
    """
    DocumentStage.objects.filter(document=document).update(
        status=DocumentStage.STATUS_PENDING,
        detail="",
        started_at=None,
        finished_at=None,
        updated_at=timezone.now(),
    )


def _touch_document_stage(
    document: Document, stage: str, progress: int | None = None
) -> None:
    """Disjoint-update ONLY Document.processing_stage/processing_progress."""
    fields = {"processing_stage": stage, "updated_at": timezone.now()}
    if progress is not None:
        fields["processing_progress"] = progress
    Document.objects.filter(pk=document.pk).update(**fields)


def _stage_start(
    document: Document, name: str, progress: int | None = None
) -> None:
    """Mark a stage running. Disjoint queryset update + document stage touch."""
    DocumentStage.objects.filter(document=document, name=name).update(
        status=DocumentStage.STATUS_RUNNING,
        detail="",
        started_at=timezone.now(),
        finished_at=None,
        updated_at=timezone.now(),
    )
    _touch_document_stage(document, name, progress)


def _stage_complete(
    document: Document,
    name: str,
    detail: str = "",
    progress: int | None = None,
) -> None:
    """Mark a stage completed. Disjoint queryset update + document stage touch."""
    DocumentStage.objects.filter(document=document, name=name).update(
        status=DocumentStage.STATUS_COMPLETED,
        detail=detail,
        finished_at=timezone.now(),
        updated_at=timezone.now(),
    )
    _touch_document_stage(document, name, progress)


def _stage_fail(document: Document, name: str, detail: str) -> None:
    """Mark a stage failed. Disjoint queryset update (does NOT touch Document)."""
    DocumentStage.objects.filter(document=document, name=name).update(
        status=DocumentStage.STATUS_FAILED,
        detail=detail[:2000],
        finished_at=timezone.now(),
        updated_at=timezone.now(),
    )


def _mark_document_failed(document: Document, message: str) -> None:
    """Flip ONLY the Document failure fields (no chunk/stage cleanup).

    The Temporal path deliberately avoids ``_mark_failed``'s blanket cleanup:
    chunk rows and other stages are left intact so a resume can short-circuit
    the stages that already completed.
    """
    Document.objects.filter(pk=document.pk).update(
        status=Document.STATUS_FAILED,
        processing_stage="failed",
        processing_error=message[:2000],
        updated_at=timezone.now(),
    )


def _stage_is_completed(document: Document, name: str) -> bool:
    return DocumentStage.objects.filter(
        document=document, name=name, status=DocumentStage.STATUS_COMPLETED
    ).exists()


def stage_mark_processing(document_id: str) -> dict:
    """Stage 1: re-arm stages, download any URL source, flip to PROCESSING.

    EXEMPT from the completed short-circuit (decision 2): it always re-arms ALL
    DocumentStage rows via ``_reset_stages`` so a resume/reprocess starts clean.
    ``_record_attempt`` is NOT called here — the activity wrapper owns it.

    Dispatch-ordering note (decision 2): the admin runs ``_reset_for_reprocess``
    and the QUEUED flip BEFORE the async workflow starts, so this stage only
    re-arms the DocumentStage rows, not ``Document.status``/``processing_stage``
    (beyond flipping to PROCESSING below).
    """
    document = Document.objects.get(pk=document_id)
    _ensure_stages(document)
    _reset_stages(document)


    if document.source_url and not document.file:
        from .url_source import UrlSourceError, download_source_to_document

        try:
            download_source_to_document(document)
        except UrlSourceError as exc:
            _stage_fail(document, STAGE_MARK_PROCESSING, str(exc))
            _mark_document_failed(document, str(exc))
            raise DocumentIndexError(str(exc)) from exc
        document.save(
            update_fields=[
                "file",
                "title",
                "file_type",
                "content_type",
                "file_size",
                "updated_at",
            ]
        )

    _stage_start(document, STAGE_MARK_PROCESSING, progress=5)
    try:
        Document.objects.filter(pk=document.pk).update(
            status=Document.STATUS_PROCESSING,
            processing_error="",
            updated_at=timezone.now(),
        )
    except Exception as exc:
        logger.exception(
            "stage_mark_processing failed for document %s", document_id
        )
        _stage_fail(document, STAGE_MARK_PROCESSING, str(exc))
        raise DocumentIndexError(str(exc)) from exc
    _stage_complete(document, STAGE_MARK_PROCESSING, progress=5)
    return {"ok": True}


def stage_extract_text(document_id: str) -> dict:
    """Stage 2: extract + persist the authoritative DocumentChunk rows."""
    document = Document.objects.get(pk=document_id)
    _ensure_stages(document)
    if _stage_is_completed(document, STAGE_EXTRACT_TEXT):
        return {"skipped": True, "chunk_count": document.chunks.count()}

    _stage_start(document, STAGE_EXTRACT_TEXT, progress=25)
    try:
        extracted_chunks = extract_document_chunks(document)
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
        with transaction.atomic():
            _delete_chunks(document)
            DocumentChunk.objects.bulk_create(chunk_rows)
    except (DocumentValidationError, DocumentIndexError) as exc:
        _stage_fail(document, STAGE_EXTRACT_TEXT, str(exc))
        raise
    except Exception as exc:
        logger.exception(
            "stage_extract_text failed for document %s", document_id
        )
        message = (
            "Document extraction failed. Check that the file is valid and retry."
        )
        _stage_fail(document, STAGE_EXTRACT_TEXT, message)
        raise DocumentIndexError(message) from exc
    _stage_complete(
        document,
        STAGE_EXTRACT_TEXT,
        detail=f"{len(chunk_rows)} chunks",
        progress=30,
    )
    return {"chunk_count": len(chunk_rows)}


def stage_chunk_text(document_id: str) -> dict:
    """Stage 3: thin verify stage.

    Chunking is inseparable from extraction today (``split_page_text`` runs
    inside ``extract_document_chunks``), so this stage only verifies that chunk
    rows exist and records the count; it does no additional work. The workflow
    ignores the return value.
    """
    document = Document.objects.get(pk=document_id)
    _ensure_stages(document)
    if _stage_is_completed(document, STAGE_CHUNK_TEXT):
        return {"skipped": True, "chunk_count": document.chunks.count()}

    _stage_start(document, STAGE_CHUNK_TEXT, progress=35)
    try:
        chunk_count = document.chunks.count()
        if chunk_count == 0:
            raise DocumentIndexError(
                "No chunks were produced during extraction."
            )
    except DocumentIndexError as exc:
        _stage_fail(document, STAGE_CHUNK_TEXT, str(exc))
        raise
    except Exception as exc:
        logger.exception(
            "stage_chunk_text failed for document %s", document_id
        )
        _stage_fail(document, STAGE_CHUNK_TEXT, str(exc))
        raise DocumentIndexError(str(exc)) from exc
    _stage_complete(
        document,
        STAGE_CHUNK_TEXT,
        detail=f"{chunk_count} chunks",
        progress=40,
    )
    return {"chunk_count": chunk_count}


def stage_delete_existing_chunks(document_id: str) -> dict:
    """Stage 4: vector cleanup ONLY (never chunk rows).

    Deletes the deterministic vector ids for the CURRENT chunk set so a re-index
    starts clean. The embed stage re-adds the SAME ids (upsert), so this never
    creates duplicates; it never deletes ``DocumentChunk`` rows.
    """
    document = Document.objects.get(pk=document_id)
    _ensure_stages(document)
    if _stage_is_completed(document, STAGE_DELETE_EXISTING_CHUNKS):
        return {"skipped": True}

    _stage_start(document, STAGE_DELETE_EXISTING_CHUNKS, progress=45)
    try:
        vector_ids = [
            f"{document.pk}:{chunk_index}"
            for chunk_index in document.chunks.values_list(
                "chunk_index", flat=True
            )
        ]
        if vector_ids:
            try:
                ChromaVectorStore().delete_documents(
                    vector_ids, collection=VECTOR_COLLECTION
                )
            except Exception:
                logger.exception(
                    "Could not remove existing vectors for document %s",
                    document_id,
                )
    except Exception as exc:
        logger.exception(
            "stage_delete_existing_chunks failed for document %s", document_id
        )
        _stage_fail(document, STAGE_DELETE_EXISTING_CHUNKS, str(exc))
        raise DocumentIndexError(str(exc)) from exc
    _stage_complete(document, STAGE_DELETE_EXISTING_CHUNKS, progress=50)
    return {"ok": True}


def stage_embed_and_store_chunks(document_id: str) -> dict:
    """Stage 5: embed chunk rows and upsert their deterministic vectors.

    Only library documents are embedded; chat attachments short-circuit. On
    failure only the vectors pushed THIS run are removed (scoped partial cleanup).
    """
    document = Document.objects.get(pk=document_id)
    _ensure_stages(document)
    if _stage_is_completed(document, STAGE_EMBED_AND_STORE_CHUNKS):
        return {"skipped": True}

    if document.source != Document.SOURCE_LIBRARY:
        _stage_complete(document, STAGE_EMBED_AND_STORE_CHUNKS, progress=75)
        return {"skipped": True}

    _stage_start(document, STAGE_EMBED_AND_STORE_CHUNKS, progress=55)
    vector_store: ChromaVectorStore | None = None
    pushed_ids: list[str] = []
    try:
        chunk_rows = list(
            DocumentChunk.objects.filter(document=document).order_by("chunk_index")
        )
        extracted_chunks = [
            (chunk.page_number, chunk.source_location, chunk.content)
            for chunk in chunk_rows
        ]
        try:
            embeddings = _embed_chunks(extracted_chunks)
        except LLMConfigurationError as exc:
            raise DocumentIndexError(
                "LLM key required to embed library documents. Set "
                "OPENAI_API_KEY and retry."
            ) from exc

        vector_store = ChromaVectorStore()
        total_batches = max(
            1, (len(chunk_rows) + VECTOR_BATCH_SIZE - 1) // VECTOR_BATCH_SIZE
        )
        for batch_index, offset in enumerate(
            range(0, len(chunk_rows), VECTOR_BATCH_SIZE), start=1
        ):
            rows = chunk_rows[offset : offset + VECTOR_BATCH_SIZE]
            vectors = embeddings[offset : offset + VECTOR_BATCH_SIZE]
            pushed_ids.extend(
                vector_store.add_documents(
                    [
                        {


                            "id": f"{document.pk}:{chunk.chunk_index}",
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
            progress = 55 + int(20 * batch_index / total_batches)
            _touch_document_stage(
                document, STAGE_EMBED_AND_STORE_CHUNKS, progress
            )
    except (DocumentValidationError, DocumentIndexError) as exc:
        if vector_store is not None and pushed_ids:
            try:
                vector_store.delete_documents(
                    pushed_ids, collection=VECTOR_COLLECTION
                )
            except Exception:
                logger.exception(
                    "Could not remove partial vectors for document %s",
                    document_id,
                )
        _stage_fail(document, STAGE_EMBED_AND_STORE_CHUNKS, str(exc))
        raise
    except Exception as exc:
        logger.exception(
            "stage_embed_and_store_chunks failed for document %s", document_id
        )
        if vector_store is not None and pushed_ids:
            try:
                vector_store.delete_documents(
                    pushed_ids, collection=VECTOR_COLLECTION
                )
            except Exception:
                logger.exception(
                    "Could not remove partial vectors for document %s",
                    document_id,
                )
        message = "Embedding/indexing failed. Retry once the LLM provider is reachable."
        _stage_fail(document, STAGE_EMBED_AND_STORE_CHUNKS, message)
        raise DocumentIndexError(message) from exc
    _stage_complete(
        document,
        STAGE_EMBED_AND_STORE_CHUNKS,
        detail=f"{len(pushed_ids)} vectors",
        progress=75,
    )
    return {"chunks_stored": len(pushed_ids)}


def stage_mark_completed(document_id: str) -> dict:
    """Stage 6: derive title, stamp file_type, flip to READY.

    The completed short-circuit returns the LIVE ``document.chunk_count``
    (decision 3): that count stays authoritative because the only later stage
    (``notify_completed``) never mutates chunk rows.
    """
    document = Document.objects.get(pk=document_id)
    _ensure_stages(document)
    if _stage_is_completed(document, STAGE_MARK_COMPLETED):
        return {"chunk_count": document.chunk_count}

    _stage_start(document, STAGE_MARK_COMPLETED, progress=90)
    try:
        chunk_rows = list(
            DocumentChunk.objects.filter(document=document).order_by("chunk_index")
        )
        extracted_text = "\n\n".join(
            chunk.content for chunk in chunk_rows if chunk.content
        ).strip()
        file_basename = (
            os.path.basename(document.file.name) if document.file else ""
        )
        user_supplied_title = (
            bool(document.title) and document.title != file_basename
        )
        document.title = derive_document_title(
            document, extracted_text, user_supplied_title
        )
        resolved_suffix = resolve_file_extension(document).lstrip(".").lower()
        if resolved_suffix:
            document.file_type = resolved_suffix
        document.status = Document.STATUS_READY
        document.processing_stage = "ready"
        document.processing_progress = 100
        document.chunk_count = document.chunks.count()
        document.processing_error = ""
        document.save(
            update_fields=[
                "title",
                "file_type",
                "status",
                "processing_stage",
                "processing_progress",
                "chunk_count",
                "processing_error",
                "updated_at",
            ]
        )
    except Exception as exc:
        logger.exception(
            "stage_mark_completed failed for document %s", document_id
        )
        _stage_fail(document, STAGE_MARK_COMPLETED, str(exc))
        raise DocumentIndexError(str(exc)) from exc
    _stage_complete(document, STAGE_MARK_COMPLETED, progress=100)
    return {"chunk_count": document.chunk_count}


def stage_notify_completed(document_id: str) -> dict:
    """Stage 7: notification hook (no-op); mark completed and return result."""
    document = Document.objects.get(pk=document_id)
    _ensure_stages(document)
    if _stage_is_completed(document, STAGE_NOTIFY_COMPLETED):
        return {
            "status": "ready",
            "chunk_count": document.chunk_count,
            "error_message": "",
        }

    _stage_start(document, STAGE_NOTIFY_COMPLETED, progress=100)
    try:
        pass
    except Exception as exc:
        logger.exception(
            "stage_notify_completed failed for document %s", document_id
        )
        _stage_fail(document, STAGE_NOTIFY_COMPLETED, str(exc))
        raise DocumentIndexError(str(exc)) from exc
    _stage_complete(document, STAGE_NOTIFY_COMPLETED, progress=100)
    return {
        "status": "ready",
        "chunk_count": document.chunk_count,
        "error_message": "",
    }
