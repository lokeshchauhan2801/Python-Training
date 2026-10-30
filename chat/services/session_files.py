"""Files attached to a chat session, before and after processing.

An attached file is an entry in ``ConversationSession.context``:
``"unprocessed_docs"`` until its Document is READY, then ``"processed_docs"``.
No ``Document`` row exists until the user processes the file; the bytes live
under ``media/unsettled/<session_id>/<entry_id>/`` until then.

This module is the only writer of those two context keys. Every write goes
through ``locked_files``, which locks the session row and replaces only the two
keys, so parallel requests never drop each other's entries.
"""
from __future__ import annotations

import copy
import logging
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from pathlib import Path, PurePosixPath
from typing import IO, Iterator

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from django.utils.text import get_valid_filename

from basics.ulid import new_ulid
from chat.models import ConversationSession
from documents import controls
from documents.models import Document
from documents.services import SUPPORTED_FILE_TYPES

logger = logging.getLogger(__name__)

UNPROCESSED_KEY = "unprocessed_docs"
PROCESSED_KEY = "processed_docs"
MAX_FILES = 200
MAX_IDS = 50
_ID_LEN = 26
_STEM_MAX = 60
_TITLE_MAX = 255
_CONTENT_TYPE_MAX = 150

UNSUPPORTED_MESSAGE = (
    "Unsupported file type. Supported: PDF, DOCX, TXT, MD, CSV, XLSX, JSON, "
    "PNG, JPG, TIFF."
)
FAILED_HINT = (
    "Say “try again” or “restart” to continue from the last completed step."
)

PUBLIC_FIELDS = (
    "id",
    "title",
    "file_type",
    "size",
    "attached_at",
    "document_id",
    "processed_at",
)


def max_bytes() -> int:
    return getattr(settings, "CHAT_ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024)


class UploadRejected(Exception):
    """An attach request failed validation; ``status`` is the HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class UnknownEntry(Exception):
    """An entry id is malformed or not in this session's lists."""


class TooManyIds(UnknownEntry):
    """More than ``MAX_IDS`` distinct entry ids in one request."""


class FileUnavailable(Exception):
    """The file's bytes cannot be read (missing, removed or a forged key)."""


# --------------------------------------------------------------------------
# Context state and the row lock
# --------------------------------------------------------------------------


def _parse_when(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=dt_timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _entries(value) -> list:
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(item) for item in value if isinstance(item, dict)]


def _sort_by_attach(entries: list) -> None:
    entries.sort(key=lambda e: _parse_when(e.get("attached_at")))


@dataclass
class FilesState:
    """Deep copies of the two lists, plus a ``dirty`` flag set by mutators."""

    unprocessed: list = field(default_factory=list)
    processed: list = field(default_factory=list)
    dirty: bool = False

    @classmethod
    def from_context(cls, context) -> "FilesState":
        context = context if isinstance(context, dict) else {}
        return cls(
            unprocessed=_entries(context.get(UNPROCESSED_KEY)),
            processed=_entries(context.get(PROCESSED_KEY)),
        )

    def all_entries(self) -> list:
        return [*self.unprocessed, *self.processed]

    def count(self) -> int:
        return len(self.unprocessed) + len(self.processed)

    def find(self, entry_id):
        for list_name, entries in (
            (UNPROCESSED_KEY, self.unprocessed),
            (PROCESSED_KEY, self.processed),
        ):
            for entry in entries:
                if entry.get("id") == entry_id:
                    return list_name, entry
        return None

    def _require(self, entry_id):
        found = self.find(entry_id)
        if found is None:
            raise UnknownEntry(entry_id)
        return found

    def append(self, entry: dict) -> None:
        self.unprocessed.append(copy.deepcopy(entry))
        self.dirty = True

    def set_document(self, entry_id, document_id) -> None:
        _, entry = self._require(entry_id)
        entry["document_id"] = str(document_id) if document_id else None
        self.dirty = True

    def move_to_processed(self, entry_id, when=None) -> None:
        list_name, entry = self._require(entry_id)
        if list_name == UNPROCESSED_KEY:
            self.unprocessed.remove(entry)
            self.processed.append(entry)
            _sort_by_attach(self.processed)
        if not entry.get("processed_at"):
            entry["processed_at"] = (when or timezone.now()).isoformat()
        self.dirty = True

    def move_to_unprocessed(self, entry_id) -> None:
        list_name, entry = self._require(entry_id)
        if list_name == PROCESSED_KEY:
            self.processed.remove(entry)
            self.unprocessed.append(entry)
            _sort_by_attach(self.unprocessed)
        entry.pop("processed_at", None)
        self.dirty = True


_lock_state = threading.local()


@contextmanager
def locked_files(session, *, actor=None) -> Iterator[FilesState]:
    """Lock the session row and yield its file lists; write them back if dirty.

    Not re-entrant: a nested block's write would be overwritten by the outer
    block, so nesting raises ``RuntimeError``. Never call ``store_upload`` or
    ``reconcile`` inside an open block.
    """
    if getattr(_lock_state, "depth", 0):
        raise RuntimeError("locked_files is not re-entrant")
    _lock_state.depth = getattr(_lock_state, "depth", 0) + 1
    try:
        with transaction.atomic():
            row = (
                ConversationSession.all_objects.select_for_update(no_key=True)
                .only("pk", "context")
                .get(pk=session.pk)
            )
            state = FilesState.from_context(row.context)
            yield state
            if state.dirty:
                ctx = {
                    **(row.context if isinstance(row.context, dict) else {}),
                    UNPROCESSED_KEY: state.unprocessed,
                    PROCESSED_KEY: state.processed,
                }
                fields = {"context": ctx, "updated_at": timezone.now()}
                if actor is not None:
                    fields["updated_by"] = actor
                ConversationSession.all_objects.filter(pk=row.pk).update(**fields)
                session.context = ctx
    finally:
        _lock_state.depth -= 1


# --------------------------------------------------------------------------
# Attach
# --------------------------------------------------------------------------


def _strip_controls(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cc")


def safe_name(raw_name: str, ext: str) -> str:
    """Stored file name: ``<stem, dots -> "_">[:60].<ext>`` (review-3 F7)."""
    try:
        valid = get_valid_filename(Path(raw_name).name)
    except SuspiciousFileOperation:
        valid = ""
    stem = valid.rsplit(".", 1)[0] if "." in valid else valid
    stem = stem.replace(".", "_")[:_STEM_MAX] or "file"
    return f"{stem}.{ext}"


def _delete_quietly(key: str) -> None:
    try:
        default_storage.delete(key)
    except OSError:
        logger.warning("Could not delete unsettled chat file %s", key, exc_info=True)


def store_upload(session, upload, actor) -> dict:
    """Validate and store an upload, then append its entry (no Document row)."""
    if upload is None:
        raise UploadRejected("A file is required.", 400)
    raw_name = Path(str(upload.name or "")).name
    size = int(getattr(upload, "size", 0) or 0)
    if size <= 0:
        raise UploadRejected("The file is empty.", 400)
    suffix = Path(raw_name).suffix.lower()
    if suffix not in SUPPORTED_FILE_TYPES:
        raise UploadRejected(UNSUPPORTED_MESSAGE, 400)
    if size > max_bytes():
        raise UploadRejected("File is too large (max 25 MB).", 413)

    ext = suffix[1:]
    entry_id = new_ulid()
    stored = safe_name(raw_name, ext)
    key = default_storage.save(f"unsettled/{session.pk}/{entry_id}/{stored}", upload)
    try:
        title = _strip_controls(raw_name).strip()[:_TITLE_MAX] or stored
        entry = {
            "id": entry_id,
            "title": title,
            "storage_key": key,
            "file_type": ext,
            "content_type": str(getattr(upload, "content_type", "") or "")[
                :_CONTENT_TYPE_MAX
            ],
            "size": size,
            "attached_at": timezone.now().isoformat(),
            "document_id": None,
        }
        with locked_files(session, actor=actor) as state:
            if state.count() >= MAX_FILES:
                raise UploadRejected(f"This chat already has {MAX_FILES} files.", 400)
            state.append(entry)
    except Exception:
        _delete_quietly(key)
        raise
    return entry


# --------------------------------------------------------------------------
# Reader
# --------------------------------------------------------------------------


def _key_is_safe(key, session_id) -> bool:
    if not isinstance(key, str) or not key:
        return False
    path = PurePosixPath(key)
    if path.is_absolute() or "\\" in key or ".." in path.parts:
        return False
    return key.startswith(f"unsettled/{session_id}/")


@dataclass(frozen=True)
class SessionFile:
    session_id: str
    company_id: str
    entry: dict
    id: str
    title: str
    file_type: str
    attached_at: datetime
    document_id: str | None
    document: Document | None

    @property
    def is_csv(self) -> bool:
        return self.file_type == "csv"

    @property
    def state(self) -> str:
        return derive_state(self.entry, self.document)

    @property
    def source_available(self) -> bool:
        try:
            return default_storage.exists(self._checked_key())
        except (FileUnavailable, OSError):
            return False

    @property
    def readable(self) -> bool:
        return self.state != "removed" or self.source_available

    def _checked_key(self) -> str:
        key = self.entry.get("storage_key")
        if not _key_is_safe(key, self.session_id):
            logger.warning(
                "Rejected storage key for chat file %s in session %s",
                self.id,
                self.session_id,
            )
            raise FileUnavailable(self.id)
        return key

    def open(self) -> IO[bytes]:
        if self.document_id and self.document is not None:
            if not self.document.file:
                raise FileUnavailable(self.id)
            try:
                return self.document.file.open("rb")
            except OSError as exc:
                raise FileUnavailable(self.id) from exc
        key = self._checked_key()
        try:
            return default_storage.open(key, "rb")
        except OSError as exc:
            raise FileUnavailable(self.id) from exc

    def as_extractable(self) -> Document:
        """A Document ``extract_document_text`` can read; never saved."""
        if self.document_id and self.document is not None:
            return self.document
        return Document(
            company_id=self.company_id,
            title=self.title,
            file=self._checked_key(),
            file_type=self.file_type,
        )


def _load_documents(session, entries) -> dict:
    ids = {str(e["document_id"]) for e in entries if e.get("document_id")}
    if not ids:
        return {}
    return {
        str(doc.pk): doc
        for doc in Document.objects.filter(pk__in=ids, company=session.company_id)
    }


def _session_file(session, entry, documents) -> SessionFile:
    document_id = str(entry["document_id"]) if entry.get("document_id") else None
    return SessionFile(
        session_id=str(session.pk),
        company_id=str(session.company_id),
        entry=copy.deepcopy(entry),
        id=str(entry.get("id") or ""),
        title=str(entry.get("title") or ""),
        file_type=str(entry.get("file_type") or "").lower(),
        attached_at=_parse_when(entry.get("attached_at")),
        document_id=document_id,
        document=documents.get(document_id) if document_id else None,
    )


def list_files(session) -> list[SessionFile]:
    """Every file of the session (both lists), oldest attach first."""
    entries = FilesState.from_context(session.context).all_entries()
    documents = _load_documents(session, entries)
    files = [_session_file(session, entry, documents) for entry in entries]
    files.sort(key=lambda f: f.attached_at)
    return files


def _dedupe(ids) -> list:
    return list(dict.fromkeys(ids or []))


def files_by_ids(session, ids) -> list[SessionFile]:
    """Resolve entry ids (deduped, in order); unknown ids raise ``UnknownEntry``."""
    ids = _dedupe(ids)
    if len(ids) > MAX_IDS:
        raise TooManyIds(len(ids))
    if not ids:
        return []
    state = FilesState.from_context(session.context)
    entries = []
    for entry_id in ids:
        if not isinstance(entry_id, str) or len(entry_id) != _ID_LEN:
            raise UnknownEntry(entry_id)
        found = state.find(entry_id)
        if found is None:
            raise UnknownEntry(entry_id)
        entries.append(found[1])
    documents = _load_documents(session, entries)
    return [_session_file(session, entry, documents) for entry in entries]


def attachment_chips(session, ids) -> list[dict]:
    """``[{"id", "title"}]`` for the known ids, in order (unknown ids skipped)."""
    state = FilesState.from_context(getattr(session, "context", None))
    chips = []
    for entry_id in _dedupe(str(i) for i in (ids or [])):
        found = state.find(entry_id)
        if found is not None:
            chips.append({"id": entry_id, "title": str(found[1].get("title") or "")})
    return chips


# --------------------------------------------------------------------------
# State and the public projections
# --------------------------------------------------------------------------


def derive_state(entry, document) -> str:
    """The file's state from its entry and live Document (design G.3)."""
    if not entry.get("document_id"):
        return "not_started"
    if document is None or getattr(document, "is_deleted", False):
        return "removed"
    status = document.status
    if status == Document.STATUS_UPLOADED:
        return "not_started"
    if status == Document.STATUS_QUEUED:
        return "queued"
    if status == Document.STATUS_PROCESSING:
        return "processing"
    if status == Document.STATUS_FAILED:
        return "stopped" if controls.is_stopped(document) else "failed"
    if status == Document.STATUS_READY:
        return "processed"
    return "removed"


_SELECTABLE = {"not_started", "stopped", "failed", "processed"}
_IN_FLIGHT = {"queued", "processing"}


def _label(state, stage, progress) -> str:
    if state == "not_started":
        return "Not processed"
    if state == "queued":
        return "Queued"
    if state == "processing":
        detail = " ".join(part for part in (stage, f"{progress}%") if part)
        return f"Processing · {detail}"
    if state == "stopped":
        return f"Stopped at {stage}" if stage else "Stopped"
    if state == "failed":
        return f"Failed at {stage}" if stage else "Failed"
    if state == "processed":
        return "Completed"
    return "Removed"


def file_row(session_file: SessionFile) -> dict:
    """The public FileRow for one file (design C). No paths or storage keys."""
    entry = session_file.entry
    document = session_file.document
    state = session_file.state
    stage = ""
    progress = 0
    error = ""
    hint = ""
    if document is not None and state in (_IN_FLIGHT | {"stopped", "failed"}):
        stage = controls.current_stage(document)
    if document is not None and state != "removed":
        progress = document.processing_progress_display
    if state == "failed":
        error = (document.processing_error or "").strip()[:200]
        hint = FAILED_HINT
    if state in _IN_FLIGHT:
        actions = ["stop"]
    elif state in ("stopped", "failed"):
        actions = ["resume"]
    else:
        actions = []
    recoverable = state == "removed" and session_file.source_available
    return {
        "id": session_file.id,
        "title": session_file.title,
        "file_type": session_file.file_type,
        "size": entry.get("size") or 0,
        "attached_at": entry.get("attached_at"),
        "state": state,
        "label": (
            "Removed — available to process again"
            if recoverable
            else _label(state, stage, progress)
        ),
        "stage": stage,
        "progress": progress,
        "error": error,
        "hint": hint,
        "selectable": state in _SELECTABLE or recoverable,
        "actions": actions,
        "terminal": state not in _IN_FLIGHT,
        "document_id": session_file.document_id,
    }


def public_entry(entry) -> dict:
    """Allowlisted entry fields for API output (no storage key)."""
    return {name: entry.get(name) for name in PUBLIC_FIELDS}


# --------------------------------------------------------------------------
# Reconcile, eligibility and purge
# --------------------------------------------------------------------------


def _moves(state: FilesState, documents: dict) -> list:
    moves = []
    for entry in state.unprocessed:
        doc = documents.get(str(entry.get("document_id") or ""))
        if derive_state(entry, doc) == "processed":
            moves.append(("processed", entry["id"]))
    for entry in state.processed:
        doc = documents.get(str(entry.get("document_id") or ""))
        if doc is not None and doc.status != Document.STATUS_READY:
            moves.append(("unprocessed", entry["id"]))
    return moves


def reconcile(session) -> list[SessionFile]:
    """Move entries between the lists to match their Documents (idempotent)."""
    state = FilesState.from_context(session.context)
    if not _moves(state, _load_documents(session, state.all_entries())):
        return list_files(session)
    with locked_files(session) as locked:
        moves = _moves(locked, _load_documents(session, locked.all_entries()))
        now = timezone.now()
        for target, entry_id in moves:
            if target == "processed":
                locked.move_to_processed(entry_id, now)
            else:
                locked.move_to_unprocessed(entry_id)
    return list_files(session)


_ELIGIBLE = {
    "stop": {"queued", "processing"},
    "resume": {"stopped", "failed"},
}


def has_eligible(session, kind) -> bool:
    """True when some file can be stopped (``"stop"``) or resumed (``"resume"``)."""
    wanted = _ELIGIBLE.get(kind, set())
    return any(f.state in wanted for f in list_files(session))


def unsettled_keys(session) -> list[str]:
    """Storage keys of entries in this session, read under a row lock.

    Call inside the caller's ``transaction.atomic()`` block, right before the
    hard delete, so an attach that committed meanwhile is included.
    """
    row = (
        ConversationSession.all_objects.select_for_update()
        .only("pk", "context")
        .filter(pk=session.pk)
        .first()
    )
    if row is None:
        return []
    keys = []
    for entry in FilesState.from_context(row.context).all_entries():
        key = entry.get("storage_key")
        if not _key_is_safe(key, row.pk):
            continue
        keys.append(key)
    return keys


def purge_unsettled(keys) -> None:
    for key in keys or []:
        _delete_quietly(key)
