"""Chat-only processing of a chat file through Temporal.

"Process this file" runs the file through ``ChatFileProcessingWorkflow``
(``infrastructure/temporal/chat_file_workflow.py``) without touching the
company knowledge base: no ``Document`` row, no ``DocumentChunk`` rows and no
vectors. The extracted text stays in the session:

* the entry in ``ConversationSession.context`` carries ``destination="chat"``
  and a ``processing`` dict (status, stage, progress, error, workflow id);
* the run keeps only an LLM summary of the file, in the entry's ``summary``.
  The full extracted text is a temporary file next to the upload
  (``extracted.txt``) that the summarize stage deletes.

Only "add this file to the company knowledge base" creates a ``Document``
(``file_commands.process_entries``). Both kinds are stopped, resumed and
cancelled the same way, and their status is read from the Temporal API.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from django.db import close_old_connections, transaction
from django.utils import timezone

from basics.ulid import new_ulid
from chat.models import ConversationSession
from documents.controls import ControlError

logger = logging.getLogger(__name__)

DEST_CHAT = "chat"
DEST_KB = "kb"
CHUNKS_KEY = "chat_file_chunks"
WORKFLOW = "ChatFileProcessingWorkflow"

QUEUED = "queued"
PROCESSING = "processing"
PAUSED = "paused"
FAILED = "failed"
READY = "ready"

STAGE_MARK = "mark_processing"
STAGE_EXTRACT = "extract_text"
STAGE_SUMMARIZE = "summarize"
STAGE_COMPLETE = "mark_completed"

# Summary map-reduce bounds: at most this many pieces of the text are
# summarised (spread over the whole file), then merged into one summary.
_PIECE_CHARS = 12000
_MAX_PIECES = 8
SUMMARY_MAX_CHARS = 4000

_STATE_OF = {
    QUEUED: "queued",
    PROCESSING: "processing",
    PAUSED: "paused",
    FAILED: "failed",
    READY: "processed",
}
# Temporal states that mean the run is over without completing the file.
_DEAD = {"FAILED", "TERMINATED", "TIMED_OUT"}


def workflow_id_for(entry_id) -> str:
    return f"chatfile-{entry_id}"


def processing(entry) -> dict:
    value = (entry or {}).get("processing")
    return value if isinstance(value, dict) else {}


def is_chat_run(entry) -> bool:
    """The entry is processed (or being processed) only in this chat."""
    return not (entry or {}).get("document_id") and bool(processing(entry))


def entry_state(entry) -> str:
    """The FileRow state of a chat-only entry."""
    return _STATE_OF.get(processing(entry).get("status"), "not_started")


def stage_label(entry) -> str:
    stage = str(processing(entry).get("stage") or "")
    return "" if stage in ("", QUEUED, READY, FAILED) else stage.replace("_", " ")


def _now() -> str:
    return timezone.now().isoformat()


# --------------------------------------------------------------------------- #
# Writes from activities and after-commit callbacks (no session object)
# --------------------------------------------------------------------------- #
@contextmanager
def _locked(session_id):
    """Lock the session row; yield ``(files_state, None)`` or ``(None, None)``.

    Writes the two file lists back. Never call inside
    ``session_files.locked_files`` (it would overwrite this write).
    """
    from chat.services.session_files import PROCESSED_KEY, UNPROCESSED_KEY, FilesState

    with transaction.atomic():
        row = (
            ConversationSession.all_objects.select_for_update(no_key=True)
            .only("pk", "context")
            .filter(pk=session_id)
            .first()
        )
        if row is None:
            yield None, None
            return
        context = row.context if isinstance(row.context, dict) else {}
        state = FilesState.from_context(context)
        yield state, None
        new_context = {
            **context,
            UNPROCESSED_KEY: state.unprocessed,
            PROCESSED_KEY: state.processed,
        }
        new_context.pop(CHUNKS_KEY, None)  # older runs kept full chunks here
        ConversationSession.all_objects.filter(pk=row.pk).update(
            context=new_context,
            updated_at=timezone.now(),
        )


def _update(session_id, entry_id, token=None, **fields) -> bool:
    """Merge ``fields`` into the entry's ``processing`` (only for ``token``'s run)."""
    with _locked(session_id) as (state, _chunks):
        found = state.find(entry_id) if state is not None else None
        if found is None:
            return False
        proc = processing(found[1])
        if not proc or (token and proc.get("token") != token):
            return False
        proc.update(fields)
        found[1]["processing"] = proc
        return True


def read_entry(session_id, entry_id):
    from chat.services.session_files import FilesState

    row = (
        ConversationSession.all_objects.only("pk", "context")
        .filter(pk=session_id)
        .first()
    )
    if row is None:
        return None
    found = FilesState.from_context(row.context).find(entry_id)
    return found[1] if found else None


def stored_text(session, entry_id) -> str:
    """The summary a chat-only run kept for ``entry_id`` ("" when none)."""
    from chat.services.session_files import FilesState

    found = FilesState.from_context(session.context).find(str(entry_id))
    return str((found[1].get("summary") if found else "") or "").strip()


def _sidecar_key(entry) -> str:
    """Where the extract stage leaves the full text for the summarize stage."""
    key = str((entry or {}).get("storage_key") or "")
    return key.rsplit("/", 1)[0] + "/extracted.txt" if "/" in key else ""


def _delete_sidecar(entry) -> None:
    from django.core.files.storage import default_storage

    key = _sidecar_key(entry)
    if key:
        try:
            default_storage.delete(key)
        except OSError:
            logger.warning("Could not delete %s", key, exc_info=True)


# --------------------------------------------------------------------------- #
# Starting a run (inside ``session_files.locked_files``)
# --------------------------------------------------------------------------- #
def begin(state, entry, *, fresh: bool) -> str:
    """Queue a chat-only run for ``entry``; return the run token.

    ``fresh`` starts from the first stage; otherwise completed stages are
    skipped (resume after a failure). Call inside ``locked_files`` and then
    :func:`dispatch_on_commit`.
    """
    proc = {} if fresh else dict(processing(entry))
    token = new_ulid()
    proc.update(
        status=QUEUED,
        stage=QUEUED,
        error="",
        workflow_id=workflow_id_for(entry["id"]),
        token=token,
        queued_at=_now(),
    )
    if fresh:
        proc.update(progress=0, completed_stages=[], chunk_count=0)
        entry.pop("summary", None)
    entry["processing"] = proc
    entry["destination"] = DEST_CHAT
    # A file removed from Documents is processed again in this chat only.
    entry["document_id"] = None
    state.dirty = True
    return token


def dispatch_on_commit(session_id, entry_id, token) -> None:
    from documents.tasks import _dispatch_executor

    session_id, entry_id = str(session_id), str(entry_id)
    transaction.on_commit(
        lambda: _dispatch_executor.submit(_dispatch, session_id, entry_id, token)
    )


def _dispatch(session_id, entry_id, token) -> None:
    from infrastructure.processing.temporal_backend import TemporalProcessingBackend

    close_old_connections()
    try:
        proc = processing(read_entry(session_id, entry_id))
        if proc.get("token") != token or proc.get("status") != QUEUED:
            return  # stopped or re-queued since
        backend = TemporalProcessingBackend()
        params = {"session_id": session_id, "entry_id": entry_id, "token": token}
        wf = workflow_id_for(entry_id)
        try:
            backend.start(WORKFLOW, params, wf)
        except Exception as exc:  # an old run still open under this id
            if "already" not in str(exc).lower() and type(exc).__name__ != (
                "WorkflowAlreadyStartedError"
            ):
                raise
            backend.terminate(wf)
            backend.start(WORKFLOW, params, wf)
    except Exception as exc:
        logger.exception("Could not dispatch chat file %s", entry_id)
        _update(
            session_id,
            entry_id,
            token,
            status=FAILED,
            stage=FAILED,
            error=f"Could not queue the file for processing: {exc}",
        )
    finally:
        close_old_connections()


# --------------------------------------------------------------------------- #
# Controls (stop / resume / cancel) for a chat-only run
# --------------------------------------------------------------------------- #
def _signal(entry_id, name) -> bool:
    from infrastructure.processing.temporal_backend import TemporalProcessingBackend

    return TemporalProcessingBackend().signal(workflow_id_for(entry_id), name)


def pause(session, entry_id, actor=None) -> str:
    from chat.services.session_files import locked_files

    with locked_files(session, actor=actor) as state:
        found = state.find(entry_id)
        proc = processing(found[1]) if found else {}
        if proc.get("status") not in (QUEUED, PROCESSING):
            raise ControlError("Only a queued or running file can be stopped.")
        proc.update(status=PAUSED)
        found[1]["processing"] = proc
        state.dirty = True
    _signal(entry_id, "pause")  # a queued run that never started just stays paused
    return "Paused. Resume to continue from here, or cancel to discard it."


def _resume_or_restart(session_id, entry_id) -> None:
    if _signal(entry_id, "resume"):
        return
    # No open run (never started, or it closed): start one that skips the
    # stages already completed.
    from chat.services.session_files import FilesState

    with _locked(session_id) as (state, _chunks):
        found = state.find(entry_id) if state is not None else None
        if found is None or processing(found[1]).get("status") != PROCESSING:
            return
        token = begin(FilesState(), found[1], fresh=False)
    _dispatch(str(session_id), str(entry_id), token)


def resume(session, entry_id, actor=None) -> str:
    from chat.services.session_files import locked_files

    with locked_files(session, actor=actor) as state:
        found = state.find(entry_id)
        entry = found[1] if found else {}
        proc = processing(entry)
        status = proc.get("status")
        if status == PAUSED:
            proc.update(status=PROCESSING, error="")
            entry["processing"] = proc
            state.dirty = True
            session_id = str(session.pk)
            transaction.on_commit(
                lambda: _resume_or_restart(session_id, str(entry_id))
            )
        elif status == FAILED:
            token = begin(state, entry, fresh=False)
            dispatch_on_commit(session.pk, entry_id, token)
        else:
            raise ControlError("Only a paused or failed file can be resumed.")
    return "Resuming processing from where it stopped."


def cancel(session, entry_id, actor=None) -> str:
    from chat.services.session_files import locked_files
    from infrastructure.processing.temporal_backend import TemporalProcessingBackend

    with locked_files(session, actor=actor) as state:
        found = state.find(entry_id)
        entry = found[1] if found else {}
        if processing(entry).get("status") not in (QUEUED, PROCESSING, PAUSED, FAILED):
            raise ControlError("Only a stopped, failed or running file can be cancelled.")
        entry.pop("processing", None)
        entry.pop("destination", None)
        entry.pop("summary", None)
        state.move_to_unprocessed(entry_id)
        sidecar_entry = dict(entry)
    _delete_sidecar(sidecar_entry)
    TemporalProcessingBackend().cancel(workflow_id_for(entry_id))
    return "Cancelled. Its progress was discarded; process it again to start over."


def discard_summary(session_id, entry_id) -> None:
    with _locked(session_id) as (state, _chunks):
        found = state.find(str(entry_id)) if state is not None else None
        if found is not None:
            found[1].pop("summary", None)
            _delete_sidecar(found[1])


# --------------------------------------------------------------------------- #
# Status from the Temporal API
# --------------------------------------------------------------------------- #
def temporal_status(session, entry, workflow_id) -> dict:
    """The workflow's status from Temporal (see ``TemporalProcessingBackend.describe``).

    A chat-only run whose workflow died in Temporal while the entry still says
    queued / processing / paused is marked failed, so the chat matches Temporal.
    """
    from infrastructure.processing.temporal_backend import TemporalProcessingBackend

    info = TemporalProcessingBackend().describe(workflow_id)
    proc = processing(entry)
    if (
        is_chat_run(entry)
        and info.get("found")
        and info.get("status") in _DEAD
        and proc.get("status") in (QUEUED, PROCESSING, PAUSED)
    ):
        message = f"The Temporal workflow ended as {info['status'].lower()}."
        _update(session.pk, entry["id"], proc.get("token"), status=FAILED, stage=FAILED, error=message)
        proc.update(status=FAILED, error=message)
        entry["processing"] = proc
    return info


# --------------------------------------------------------------------------- #
# Stages (run by the activities in chat_file_workflow.py)
# --------------------------------------------------------------------------- #
class StaleRun(Exception):
    """The entry was cancelled or re-queued: this run has nothing to do."""


def _current(params) -> dict:
    proc = processing(read_entry(params["session_id"], params["entry_id"]))
    if not proc or proc.get("token") != params.get("token"):
        raise StaleRun()
    return proc


def stage_mark_processing(params) -> dict:
    try:
        proc = _current(params)
    except StaleRun:
        return {"stale": True}
    if proc.get("status") in (QUEUED, FAILED):
        _update(
            params["session_id"], params["entry_id"], params["token"],
            status=PROCESSING, stage=STAGE_MARK, progress=max(int(proc.get("progress") or 0), 5),
            started_at=proc.get("started_at") or _now(),
        )
    return {}


def _complete_stage(params, name, **fields) -> None:
    with _locked(params["session_id"]) as (state, _chunks):
        found = state.find(params["entry_id"]) if state is not None else None
        if found is None:
            return
        proc = processing(found[1])
        if proc.get("token") != params["token"]:
            return
        done = list(proc.get("completed_stages") or [])
        if name not in done:
            done.append(name)
        proc.update(completed_stages=done, **fields)
        found[1]["processing"] = proc


def stage_extract_text(params) -> dict:
    """Extract the file's text (OCR for scans/images) into the session."""
    from documents.services import DocumentValidationError, extract_document_chunks
    from chat.services.session_files import FileUnavailable, files_by_ids

    session_id, entry_id, token = params["session_id"], params["entry_id"], params["token"]
    try:
        proc = _current(params)
    except StaleRun:
        return {"stale": True}
    if STAGE_EXTRACT in (proc.get("completed_stages") or []):
        return {"skipped": True, "chunk_count": proc.get("chunk_count") or 0}
    fields = {"stage": STAGE_EXTRACT, "progress": 20, "error": ""}
    if proc.get("status") in (QUEUED, FAILED):
        fields["status"] = PROCESSING
    _update(session_id, entry_id, token, **fields)

    def on_page(page, pages):
        _update(session_id, entry_id, token, progress=20 + (60 * page // max(pages, 1)),
                detail=f"OCR page {page}/{pages}")

    try:
        session = ConversationSession.all_objects.get(pk=session_id)
        session_file = files_by_ids(session, [entry_id])[0]
        extracted = extract_document_chunks(
            session_file.as_extractable(), ocr_progress_callback=on_page
        )
    except (DocumentValidationError, FileUnavailable) as exc:
        message = str(exc) or "The file could not be read; attach it again."
        _update(session_id, entry_id, token, status=FAILED, stage=STAGE_EXTRACT, error=message)
        raise PermanentFailure(message) from exc
    except Exception as exc:
        logger.exception("Chat file %s extraction failed", entry_id)
        _update(
            session_id, entry_id, token, status=FAILED, stage=STAGE_EXTRACT,
            error="Text extraction failed. Check that the file is valid and retry.",
        )
        raise RuntimeError(str(exc)) from exc

    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    text = "\n\n".join(
        f"[{location}]\n{content}" if "(OCR)" in location else content
        for _page, location, content in extracted
        if content
    ).strip()
    key = _sidecar_key(session_file.entry)
    if not key:
        raise PermanentFailure("The file could not be read; attach it again.")
    if default_storage.exists(key):
        default_storage.delete(key)
    default_storage.save(key, ContentFile(text.encode("utf-8")))
    _complete_stage(params, STAGE_EXTRACT, progress=50, chunk_count=len(extracted), detail="")
    return {"chunk_count": len(extracted)}


def _pieces(text: str) -> list[str]:
    """Up to ``_MAX_PIECES`` slices of ``text``, spread evenly over all of it."""
    total = len(text)
    if total <= _PIECE_CHARS:
        return [text]
    count = min(_MAX_PIECES, -(-total // _PIECE_CHARS))
    step = (total - _PIECE_CHARS) / max(count - 1, 1)
    return [text[int(i * step): int(i * step) + _PIECE_CHARS] for i in range(count)]


_SUMMARY_SYSTEM = (
    "You summarise a file a user attached to a chat. Write a faithful, "
    "plain-text summary: what the file is, its main sections or columns, the "
    "key facts, names, numbers and dates. Do not invent anything. No Markdown."
)


def summarize_text(text: str, title: str, llm=None) -> str:
    """An LLM summary of ``text`` (map-reduce over evenly spread pieces)."""
    from basics.llm import LLMClient

    llm = llm or LLMClient()
    pieces = _pieces(text)
    partial = len(pieces) > 1 and len(text) > _PIECE_CHARS * _MAX_PIECES
    notes = []
    for n, piece in enumerate(pieces, start=1):
        if len(pieces) == 1:
            return _clip(llm.complete([
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": (
                    f"File: {title}. Summarise it in at most 400 words.\n\n{piece}"
                )},
            ]))
        notes.append(llm.complete([
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": (
                f"File: {title} (part {n} of {len(pieces)}). Summarise this part "
                f"in at most 150 words.\n\n{piece}"
            )},
        ]))
    merged = llm.complete([
        {"role": "system", "content": _SUMMARY_SYSTEM},
        {"role": "user", "content": (
            f"File: {title}. Merge these part summaries into one summary of the "
            "whole file (at most 400 words)."
            + (" The file is long, so the parts are samples spread across it; "
               "say so in one short line." if partial else "")
            + "\n\n" + "\n\n".join(notes)
        )},
    ])
    return _clip(merged)


def _clip(text) -> str:
    text = str(text or "").strip()
    return text if len(text) <= SUMMARY_MAX_CHARS else text[:SUMMARY_MAX_CHARS].rstrip() + "…"


def stage_summarize(params) -> dict:
    """Summarise the extracted text with the LLM; keep only the summary."""
    from django.core.files.storage import default_storage

    session_id, entry_id, token = params["session_id"], params["entry_id"], params["token"]
    try:
        proc = _current(params)
    except StaleRun:
        return {"stale": True}
    entry = read_entry(session_id, entry_id)
    if STAGE_SUMMARIZE in (proc.get("completed_stages") or []):
        return {"skipped": True}
    _update(session_id, entry_id, token, stage=STAGE_SUMMARIZE, progress=60, error="")
    key = _sidecar_key(entry)
    try:
        with default_storage.open(key, "rb") as fh:
            text = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError) as exc:
        # The extract stage's text is gone: extract again on the next try.
        _update(session_id, entry_id, token, status=FAILED, stage=STAGE_SUMMARIZE,
                error="The extracted text was lost; resume to extract it again.",
                completed_stages=[s for s in proc.get("completed_stages") or [] if s != STAGE_EXTRACT])
        raise PermanentFailure("The extracted text was lost.") from exc
    if not text.strip():
        summary = "The file has no readable text."
    else:
        try:
            summary = summarize_text(text, str(entry.get("title") or ""))
        except Exception as exc:
            logger.exception("Chat file %s summary failed", entry_id)
            _update(session_id, entry_id, token, status=FAILED, stage=STAGE_SUMMARIZE,
                    error="The summary could not be written. Resume to try again.")
            raise RuntimeError(str(exc)) from exc
    with _locked(session_id) as (state, _chunks):
        found = state.find(entry_id) if state is not None else None
        if found is None or processing(found[1]).get("token") != token:
            return {"stale": True}
        found[1]["summary"] = summary
    _complete_stage(params, STAGE_SUMMARIZE, progress=90)
    _delete_sidecar(entry)
    return {"summary_chars": len(summary)}


class PermanentFailure(Exception):
    """A failure a retry cannot fix (unsupported or unreadable file)."""


def stage_mark_completed(params) -> dict:
    with _locked(params["session_id"]) as (state, _chunks):
        found = state.find(params["entry_id"]) if state is not None else None
        if found is None:
            return {"stale": True}
        entry = found[1]
        proc = processing(entry)
        if proc.get("token") != params["token"]:
            return {"stale": True}
        done = list(proc.get("completed_stages") or [])
        done.append(STAGE_COMPLETE)
        proc.update(
            status=READY, stage=READY, progress=100, error="",
            completed_stages=done, completed_at=_now(),
        )
        entry["processing"] = proc
        state.move_to_processed(params["entry_id"])
    return {}


def stage_discard_cancelled(params) -> dict:
    """Cancel cleanup: drop the summary unless the file was processed again."""
    entry = read_entry(params["session_id"], params["entry_id"])
    if entry is None or processing(entry):
        return {"discarded": False}
    discard_summary(params["session_id"], params["entry_id"])
    return {"discarded": True}
