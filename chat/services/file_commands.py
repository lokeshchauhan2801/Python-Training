"""File commands typed in the chat (design F.1 step 5, F.2, F.3, G.4).

* ``classify(text) -> Command`` is pure: it never reads the session.
* ``effective_kind(cmd, session)`` gates STOP/RESUME/weak STATUS on the
  session's files, so ordinary chat use of those words reaches the LLM.
* ``handle_file_command(...) -> CommandReply`` answers a command
  deterministically (no LLM). ``views.send`` and the two LangGraph KB tools
  both call it, so both orchestration paths give the same reply.
* ``process_entries(...)`` creates / starts / resumes / reprocesses the
  Documents behind the chosen entries.
"""
from __future__ import annotations

import functools
import logging
import os
import re
from dataclasses import dataclass, field

from django.db import transaction

from chat.services import kb, session_files
from chat.services.chat_service import ChatService
from chat.services.file_matching import (
    COMMAND_STOPWORDS,
    explicit_name_tokens,
    match_command_segments,
    name_tokens,
    prefix_hits,
)
from documents import controls
from documents.models import Document

logger = logging.getLogger(__name__)


class Kind:
    STOP = "stop"
    RESUME = "resume"
    PROCESS = "process"
    STATUS = "status"
    LIST = "list"
    NONE = "none"


@dataclass(frozen=True)
class Command:
    kind: str
    bare: bool = False
    strong: bool = False
    weak: bool = False
    needs_name: bool = False
    loose_tokens: tuple = ()


_I = re.IGNORECASE

EXT = r"(?:pdf|docx|txt|md|csv|xlsx|json|png|jpe?g|tiff?)"
FILE_CUE = re.compile(
    r"\b(process\w*|files?|docs?|documents?|attachments?|upload\w*|tasks?|jobs?|"
    r"workflows?|index\w*|embed\w*|" + EXT + r")\b",
    _I,
)
PROC_WORD = re.compile(
    r"\b(process\w*|index\w*|embed\w*|upload\w*|tasks?|jobs?|workflows?)\b", _I
)
REF_CUE = re.compile(r"\b(it|them|my\s+files)\b|\b(this|that)\b(?=\W*$)", _I)
DOC_WORD = re.compile(r"\b(documents?|docs?|files?|attachments?|uploads?)\b", _I)
KB_WORDS = re.compile(
    r"knowledge\s*base|\bkb\b|library|company\s+(docs|documents|knowledge)", _I
)
QUESTION_LEAD = re.compile(r"^\W*(how|why|what|when|where)\b", _I)
POLITE = (
    r"(?:(?:ok(?:ay)?|yes|so|now|please|pls|kindly|can\s+you|could\s+you|"
    r"would\s+you|go\s+ahead\s+and)\W+)*"
)


def BARE(verbs: str) -> re.Pattern:
    return re.compile(
        r"^\W*" + POLITE + r"(?:" + verbs + r")"
        r"(?:\W+(?:it|that|this|them|now|please|all|everything))*\W*$",
        _I,
    )


STOP_V = r"stop|cancel|halt|abort|pause"
RESUME_V = r"resume|continue|restart|re-?start|retry|re-?try|try\s+again"

_STOP_RE = re.compile(r"\b(?:" + STOP_V + r")\b", _I)
_RESUME_RE = re.compile(r"\b(?:" + RESUME_V + r")\b", _I)
_STOP_BARE = BARE(STOP_V)
_RESUME_BARE = BARE(RESUME_V)
_PROCESS_ANY = re.compile(r"\bprocess\w*", _I)

# PROCESS (review-3 F2 replaces kb.is_add_to_kb_request here).
ADD_DEST = re.compile(
    r"\b(add|save|store|upload|put|index|ingest)\b.*\b(?:to|into|in|on)\s+"
    r"(?:the\s+|my\s+|our\s+)?(?:company\s+)?(?:knowledge\s*base|knowledgebase|kb|"
    r"document\s*library|library|documents|company\s*(?:docs|documents|knowledge))\b",
    _I,
)
ADD_IMP = re.compile(  # whole-message imperative: "add this document", "index flow csv"
    r"^\W*" + POLITE + r"(?:add|save|store|upload|index|ingest|embed)\s+"
    r"(?:(?:this|that|these|those|it|them|the|my|all|both)\b\s*)*(?:[\w.\-]+\s+){0,2}?"
    r"(?:(?:files?|documents?|docs?|attachments?|" + EXT + r")\b)?\W*$",
    _I,
)
PROCESS_DOC = re.compile(r"\bprocess\b.*\b(document|doc|file|attachment)s?\b", _I)
_REPROCESS = re.compile(r"\breprocess\b", _I)
_START_PROCESSING = re.compile(r"\b(start|begin)\s+processing\b", _I)
_LEAD_PROCESS = re.compile(r"^\W*" + POLITE + r"(?:re-?)?process\b", _I)
_PROCESS_EXT = re.compile(r"\b(?:re-?)?process\b.*\b" + EXT + r"\b", _I)
_PROCESS_RULES = (
    ADD_DEST, ADD_IMP, PROCESS_DOC, _REPROCESS, _START_PROCESSING,
    _LEAD_PROCESS, _PROCESS_EXT,
)

# STATUS (R3 + review-3 F3).
_STATUS_BARE = re.compile(
    r"^\W*(?:(?:what(?:'s|\s+is)|show|check|get)\s+(?:me\s+)?(?:the\s+)?)?"
    r"(?:current\s+)?(?:status|progress)(?:\W+(?:please|pls|now))*\W*$",
    _I,
)
_STATUS_WORD = re.compile(r"\b(status|progress)\b", _I)
_STATUS_EXCEPT_LEAD = re.compile(r"^\W*(which|what|how\s+many)\b", _I)
_STATUS_STATE_WORDS = re.compile(
    r"\b(status|progress|processing|processed|done|ready|finished|queued|"
    r"pending|stopped|failed)\b",
    _I,
)
STATE_Q = re.compile(
    r"^\W*(?:so\W+|and\W+)?(?:is|are|has|have|did|does|was|were|how|why|what)\b", _I
)
STATE_WORD = re.compile(
    r"\b(done|ready|finished|complete(?:d)?|processed|indexed|embedded|stuck|going|"
    r"fail(?:ed|ing|ure)?|happen(?:ed|ing)?|working|running)\b",
    _I,
)
REF_WORD = re.compile(r"\b(it|they|them|this|that)\b", _I)
_GOING = re.compile(r"\bgoing\b", _I)

# LIST (V3).
_LIST_SHOW = re.compile(
    r"^\W*(?:please\s+|can\s+you\s+|could\s+you\s+)?(show|list|view|display)(?:\s+me)?"
    r"(?:\s+(?:all|the|my|every|attached|uploaded|chat))*\s+"
    r"(files|documents|docs|attachments|uploads)"
    r"(?:\s+(?:in\s+this\s+chat|here|attached|uploaded|i\s+(?:attached|uploaded)))?\W*$",
    _I,
)
_LIST_ASK = re.compile(
    r"\b(what|which)\s+(files|documents|docs|attachments|uploads)\s+"
    r"(do\s+i|did\s+i|have\s+i|are\s+(here|there|attached|uploaded|in\s+this\s+chat))\b",
    _I,
)


def _command(kind: str, text: str, **flags) -> Command:
    loose = tuple(name_tokens(text, COMMAND_STOPWORDS))
    strong = (
        bool(PROC_WORD.search(text))
        or bool(explicit_name_tokens(text, COMMAND_STOPWORDS))
        or not loose
    )
    if flags.get("needs_name"):
        strong = False
    return Command(kind, strong=strong, loose_tokens=loose, **flags)


def _verb_command(kind, text, verb_re, bare_re) -> Command | None:
    if not verb_re.search(text):
        return None
    if bare_re.search(text):
        return _command(kind, text, bare=True)
    if FILE_CUE.search(text):
        return _command(kind, text)
    return _command(kind, text, needs_name=True)  # review-3 F5


def _is_status(text: str) -> Command | None:
    if _STATUS_BARE.search(text):
        return _command(Kind.STATUS, text)
    if _STATUS_WORD.search(text) and (
        FILE_CUE.search(text) or REF_CUE.search(text) or kb._extract_doc_id(text)
    ):
        return _command(Kind.STATUS, text)
    if kb.is_status_request(text) and not (
        _STATUS_EXCEPT_LEAD.search(text)
        and DOC_WORD.search(text)
        and not _STATUS_STATE_WORDS.search(text)
    ):
        return _command(Kind.STATUS, text)
    if (
        STATE_Q.search(text)
        and STATE_WORD.search(text)
        and (
            PROC_WORD.search(text)
            or FILE_CUE.search(text)
            or (REF_WORD.search(text) and not _GOING.search(text))
        )
    ):
        return _command(Kind.STATUS, text, weak=True)
    return None


def classify(text: str | None) -> Command:
    """Classify a chat message as a file command; first matching row wins."""
    text = (text or "").strip()
    if not text:
        return Command(Kind.NONE)
    is_chart = ChatService._is_chart_request(text)

    # STOP / RESUME: a chart request without a process word is never one.
    if not (is_chart and not _PROCESS_ANY.search(text)):
        for kind, verb_re, bare_re in (
            (Kind.STOP, _STOP_RE, _STOP_BARE),
            (Kind.RESUME, _RESUME_RE, _RESUME_BARE),
        ):
            command = _verb_command(kind, text, verb_re, bare_re)
            if command is not None:
                return command

    # PROCESS precedes STATUS: "start processing this file" also looks like status.
    skip_process = not KB_WORDS.search(text) and (
        is_chart or QUESTION_LEAD.search(text)
    )
    if not skip_process and any(rule.search(text) for rule in _PROCESS_RULES):
        return _command(Kind.PROCESS, text)

    status = _is_status(text)
    if status is not None:
        return status

    if not is_chart and (_LIST_SHOW.search(text) or _LIST_ASK.search(text)):
        return _command(Kind.LIST, text)

    return Command(Kind.NONE)


# --------------------------------------------------------------------------
# Session gate (F.1 step 5, review-3 F3/F5)
# --------------------------------------------------------------------------


def _has_started_file(session) -> bool:
    state = session_files.FilesState.from_context(session.context)
    return any(entry.get("document_id") for entry in state.all_entries())


def effective_kind(cmd: Command, session) -> str | None:
    """The command kind to run on this session, or None to route as chat."""
    if cmd.kind == Kind.NONE:
        return None
    if cmd.kind == Kind.STATUS:
        if cmd.weak and not _has_started_file(session):
            return None
        return Kind.STATUS
    if cmd.kind in (Kind.PROCESS, Kind.LIST):
        return cmd.kind
    # STOP / RESUME
    files = session_files.reconcile(session)
    if cmd.needs_name:
        if cmd.loose_tokens and all(prefix_hits(t, files) for t in cmd.loose_tokens):
            return cmd.kind
        return None
    if cmd.bare:
        return cmd.kind if session_files.has_eligible(session, cmd.kind) else None
    if not cmd.strong and any(not prefix_hits(t, files) for t in cmd.loose_tokens):
        return None
    return cmd.kind


# --------------------------------------------------------------------------
# Replies (F.3, G.4)
# --------------------------------------------------------------------------

PICKER_NOTE = (
    "Here is the list of files in this chat. Select the file(s) you want to "
    "process."
)
NO_FILES_REPLY = (
    "I don't see any files in this chat yet. Attach one and ask me again."
)
NOTHING_TO_STOP = "Nothing is processing in this chat right now."
NOTHING_TO_RESUME = "There is nothing stopped or failed to resume."
START_FAILED = (
    "I couldn't start processing. Nothing was changed. Please try again."
)
STATUS_HEADER = "Here is the status of the files in this chat:"

PLURAL_CUE = re.compile(
    r"\b(all|both|these|those|them|everything|files|documents|docs|attachments)\b",
    _I,
)

_IN_FLIGHT = {"queued", "processing"}
_RESUMABLE = {"stopped", "failed"}


@dataclass(frozen=True)
class CommandReply:
    content: str
    metadata: dict = field(default_factory=dict)


def _panel(mode: str, entry_ids=None, note: str = "") -> dict:
    """``metadata.file_panel``: ``entry_ids=None`` means every file."""
    return {
        "mode": mode,
        "entry_ids": list(entry_ids) if entry_ids is not None else None,
        "note": note,
    }


def _q(title) -> str:
    return f"“{title}”"


def _ids(files) -> list:
    return list(dict.fromkeys(f.id for f in files))


def _company_status(session, message) -> CommandReply:
    """Today's company-wide report, with its live progress rows (V10)."""
    content = kb.document_status_report(
        company=session.company, message_text=message
    )
    track = kb.status_document_ids(company=session.company, message_text=message)
    return CommandReply(content, {"live_status_document_ids": track} if track else {})


def _status_line(f) -> str:
    state = f.state
    doc = f.document
    if state == "not_started":
        text = "not started"
    elif state == "queued":
        text = "queued"
    elif state == "processing":
        stage = controls.current_stage(doc)
        progress = f"{doc.processing_progress_display}%"
        text = f"processing ({stage}, {progress})" if stage else f"processing ({progress})"
    elif state == "stopped":
        stage = controls.current_stage(doc)
        text = (f"stopped at {stage}" if stage else "stopped") + (
            f" — say “resume {f.title}” to continue"
        )
    elif state == "failed":
        stage = controls.current_stage(doc)
        text = f"failed at {stage}" if stage else "failed"
        error = (doc.processing_error or "").strip()[:200]
        if error:
            text += f" — {error}"
        text += ("" if text.endswith((".", "!", "?")) else ".") + " "
        text += session_files.FAILED_HINT
    elif state == "processed":
        text = "processed"
    else:
        text = "removed from Documents"
    return f"- {f.title}: {text}"


def _status_reply(targets, lines, entry_ids) -> CommandReply:
    body = [STATUS_HEADER, *(_status_line(f) for f in targets)] if targets else []
    content = "\n".join([*lines, *body]) if (lines or body) else STATUS_HEADER
    return CommandReply(content, {"file_panel": _panel("tracker", entry_ids)})


def _stop_lines(targets) -> list:
    lines = []
    for f in targets:
        if f.state not in _IN_FLIGHT:
            lines.append(f"{_q(f.title)} isn't processing, so there is nothing to stop.")
            continue
        try:
            controls.stop_document(f.document)
        except controls.ControlError as exc:
            lines.append(f"Couldn't stop {_q(f.title)}: {exc.message}")
            continue
        except Exception:  # noqa: BLE001 - one file must not break the reply
            logger.exception("Could not stop chat file %s", f.id)
            lines.append(f"Couldn't stop {_q(f.title)}. Please try again.")
            continue
        lines.append(f"Stopped {_q(f.title)}.")
    return lines


def _temporal_links(outcomes) -> list:
    links = []
    for outcome in outcomes:
        if outcome.result not in _QUEUED_RESULTS or not outcome.document_id:
            continue
        wf = kb.workflow_id_for(outcome.document_id)
        url = kb.temporal_ui_url(wf)
        if url:
            links.append({"title": outcome.title, "url": url, "workflow_id": wf})
    return links


def _run_processing(session, targets, actor, allowed):
    """``(lines, metadata_extra, failed)`` for PROCESS / RESUME targets."""
    try:
        outcomes = process_entries(session, _ids(targets), actor, allowed=allowed)
    except Exception:  # noqa: BLE001 - rolled back; tell the user nothing changed
        logger.exception("Could not process chat files in session %s", session.pk)
        return [START_FAILED], {}, True
    extra = {}
    links = _temporal_links(outcomes)
    if links:
        extra["temporal_links"] = links
    return [o.line for o in outcomes], extra, False


@dataclass
class _NameTargets:
    files: list = field(default_factory=list)
    ambiguous: list = field(default_factory=list)  # [(tokens, files)]
    missing: list = field(default_factory=list)  # [tokens]

    @property
    def named(self) -> bool:
        return bool(self.files or self.ambiguous or self.missing)


def _hit_tokens(m) -> tuple:
    hits = tuple(t for t in m.name_tokens if prefix_hits(t, m.files, start_only=False))
    return hits or m.name_tokens


def _name_targets(message, files) -> _NameTargets:
    """The F.3 name-match table, per segment (review-3 F6)."""
    found = _NameTargets()
    seen = set()
    for m in match_command_segments(message, files, stopwords=COMMAND_STOPWORDS):
        if m.kind == "exact" or (m.kind == "prefix" and len(m.files) == 1):
            for f in m.files:
                if f.id not in seen:
                    seen.add(f.id)
                    found.files.append(f)
        elif m.kind == "prefix":
            found.ambiguous.append((" ".join(_hit_tokens(m)), list(m.files)))
        elif m.explicit_tokens:
            found.missing.append(" ".join(m.explicit_tokens))
    return found


def handle_file_command(
    *, session, kind, message, attachment_ids=None, entry_ids=None, actor=None
) -> CommandReply:
    """Answer one file command deterministically (design F.3)."""
    message = message or ""
    files = session_files.reconcile(session)
    if kind == Kind.STATUS and (kb._extract_doc_id(message) or not files):
        return _company_status(session, message)
    if not files:
        return CommandReply(NO_FILES_REPLY)
    if kind == Kind.LIST:
        return CommandReply(PICKER_NOTE, {"file_panel": _panel("picker", None, PICKER_NOTE)})

    by_id = {f.id: f for f in files}

    # 1. The picker selection.
    if entry_ids:
        targets = [by_id[i] for i in dict.fromkeys(entry_ids) if i in by_id]
        return _act(session, kind, targets, actor, entry_ids=_ids(targets))

    # 2. Names in the message.
    names = _name_targets(message, files)
    if names.named:
        return _named_reply(session, kind, names, actor)

    # 3. PROCESS only: this turn's attachments.
    if kind == Kind.PROCESS:
        attached = [by_id[i] for i in dict.fromkeys(attachment_ids or []) if i in by_id]
        if len(attached) == 1 or (attached and PLURAL_CUE.search(message)):
            return _act(session, kind, attached, actor, entry_ids=_ids(attached))
        picker_ids = _ids(attached) if attached else None
        return CommandReply(
            PICKER_NOTE, {"file_panel": _panel("picker", picker_ids, PICKER_NOTE)}
        )

    # 4. No file named: the last table row.
    if kind == Kind.STOP:
        targets = [f for f in files if f.state in _IN_FLIGHT]
        if not targets:
            return CommandReply(NOTHING_TO_STOP)
    elif kind == Kind.RESUME:
        targets = [f for f in files if f.state in _RESUMABLE]
        if not targets:
            return CommandReply(NOTHING_TO_RESUME)
    else:
        return _status_reply(files, [], None)
    return _act(session, kind, targets, actor, entry_ids=_ids(targets))


def _act(session, kind, targets, actor, *, entry_ids, lines=()) -> CommandReply:
    """Run ``kind`` on ``targets``; ``lines`` are name-table lines to prepend."""
    lines = list(lines)
    if kind == Kind.STATUS:
        return _status_reply(targets, lines, entry_ids)
    extra = {}
    if kind == Kind.STOP:
        lines += _stop_lines(targets)
    elif targets:
        allowed = _RESUMABLE if kind == Kind.RESUME else None
        result_lines, extra, failed = _run_processing(session, targets, actor, allowed)
        if failed:
            return CommandReply(START_FAILED)
        lines += result_lines
    if not lines:
        lines = [{
            Kind.STOP: NOTHING_TO_STOP,
            Kind.RESUME: NOTHING_TO_RESUME,
        }.get(kind, "There is nothing to process.")]
    return CommandReply(
        "\n".join(lines),
        {**extra, "file_panel": _panel("tracker", entry_ids)},
    )


def _named_reply(session, kind, names: _NameTargets, actor) -> CommandReply:
    """Targets from names; ambiguous / not-found segments add their own line."""
    targets = list(names.files)
    candidates = [f for _tokens, group in names.ambiguous for f in group]
    notes = []
    if kind == Kind.PROCESS:
        for tokens, _group in names.ambiguous:
            notes.append(
                f"I found several files matching “{tokens}”. Select the ones you "
                "want to process."
            )
        for tokens in names.missing:
            notes.append(
                f"I couldn't find a file matching “{tokens}”. Here is the list of "
                "files in this chat."
            )
        if not notes:
            return _act(session, kind, targets, actor, entry_ids=_ids(targets))
        lines = []
        extra = {}
        if targets:
            lines, extra, failed = _run_processing(session, targets, actor, None)
            if failed:
                return CommandReply(START_FAILED)
        note = " ".join(notes)
        picker_ids = None if names.missing else _ids([*targets, *candidates])
        return CommandReply(
            "\n".join([*lines, *notes]),
            {**extra, "file_panel": _panel("picker", picker_ids, note)},
        )

    lines = []
    if kind == Kind.STATUS:
        targets = list(dict.fromkeys([*targets, *candidates]))  # report matches
    else:
        button = "Stop" if kind == Kind.STOP else "Resume"
        for tokens, group in names.ambiguous:
            titles = ", ".join(f.title for f in group)
            lines.append(
                f"I found several files matching “{tokens}”: {titles}. Use the "
                f"{button} button on the one you mean, or name it in full."
            )
    for tokens in names.missing:
        lines.append(f"I couldn't find a file matching “{tokens}” in this chat.")
    if names.missing:
        panel_ids = None
    else:
        panel_ids = _ids([*targets, *candidates])
    if kind != Kind.STATUS and not targets:
        return CommandReply(
            "\n".join(lines), {"file_panel": _panel("tracker", panel_ids)}
        )
    return _act(session, kind, targets, actor, entry_ids=panel_ids, lines=lines)


# --------------------------------------------------------------------------
# Processing (F.3 phases 0 and 1, review-3 F7)
# --------------------------------------------------------------------------

_QUEUED_RESULTS = {"queued", "resumed", "reprocessed"}


@dataclass(frozen=True)
class Outcome:
    entry_id: str
    title: str
    result: str  # queued|resumed|reprocessed|processing|removed|not_allowed|missing|blocked|unknown
    document_id: str | None = None
    detail: str = ""

    @property
    def line(self) -> str:
        name = _q(self.title)
        return {
            "queued": f"Queued {name} for processing.",
            "resumed": f"Resuming {name} from the last completed step.",
            "reprocessed": f"Queued {name} for processing again from the start.",
            "processing": f"{name} is already processing.",
            "removed": f"{name} was removed from Documents.",
            "not_allowed": f"{name} isn't stopped or failed.",
            "missing": f"{name}: the file is missing; please attach it again.",
            "blocked": f"Couldn't process {name}: {self.detail}",
        }.get(self.result, f"{name} is no longer in this chat.")


def _delete_unsettled(key) -> None:
    """``on_commit`` callback: drop the unsettled copy once the Document exists."""
    session_files.purge_unsettled([key])


def _delete_precopies(names) -> None:
    storage = Document._meta.get_field("file").storage
    for name in names:
        try:
            storage.delete(name)
        except OSError:
            logger.warning("Could not delete unused document copy %s", name, exc_info=True)


def _precopy(session, ids) -> dict:
    """Phase 0, no lock: copy each unstarted entry's bytes under documents/."""
    file_field = Document._meta.get_field("file")
    by_id = {f.id: f for f in session_files.list_files(session)}
    precopies = {}
    try:
        for entry_id in ids:
            f = by_id.get(entry_id)
            if f is None or f.document_id:
                continue
            key = str(f.entry.get("storage_key") or "")
            name = file_field.generate_filename(None, os.path.basename(key))
            try:
                with f.open() as src:
                    precopies[entry_id] = file_field.storage.save(
                        name, src, max_length=file_field.max_length
                    )
            except session_files.FileUnavailable:
                # No outcome yet: phase 1 decides (another request may have
                # processed it meanwhile).
                logger.warning("Unsettled bytes missing for chat file %s", entry_id)
    except Exception:
        _delete_precopies(precopies.values())
        raise
    return precopies


def _process_one(session, state, entry_id, actor, allowed, precopies, used) -> Outcome:
    found = state.find(entry_id)
    if found is None:
        return Outcome(entry_id, entry_id, "unknown")
    entry = found[1]
    title = str(entry.get("title") or "")
    document = None
    if entry.get("document_id"):
        document = (
            Document.objects.select_for_update()
            .filter(pk=entry["document_id"], company=session.company_id)
            .first()
        )
    doc_id = str(document.pk) if document is not None else None
    current = session_files.derive_state(entry, document)
    if current in _IN_FLIGHT:
        return Outcome(entry_id, title, "processing", doc_id)
    if current == "removed":
        return Outcome(entry_id, title, "removed")
    if allowed is not None and current not in allowed:
        return Outcome(entry_id, title, "not_allowed", doc_id)
    try:
        if current == "not_started" and document is None:
            final = precopies.get(entry_id)
            if final is None:
                logger.warning("Chat file %s has no bytes to process", entry_id)
                return Outcome(entry_id, title, "missing")
            document = Document(
                company=session.company,
                uploaded_by=actor,
                session=session,
                source=Document.SOURCE_LIBRARY,
                status=Document.STATUS_UPLOADED,
                title=title,
                file=final,
                file_type=str(entry.get("file_type") or ""),
                content_type=str(entry.get("content_type") or ""),
                file_size=int(entry.get("size") or 0),
            )
            document.save(actor=actor)
            used.add(entry_id)
            state.set_document(entry_id, document.pk)
            transaction.on_commit(
                functools.partial(_delete_unsettled, entry.get("storage_key"))
            )
            controls.start_document(document)
            return Outcome(entry_id, title, "queued", str(document.pk))
        if current == "not_started":  # a legacy UPLOADED row
            controls.start_document(document)
            return Outcome(entry_id, title, "queued", doc_id)
        if current in _RESUMABLE:
            controls.resume_document(document)
            return Outcome(entry_id, title, "resumed", doc_id)
        # processed: same row, clean full reprocess
        controls.reprocess_document(document)
        state.move_to_unprocessed(entry_id)
        return Outcome(entry_id, title, "reprocessed", doc_id)
    except controls.ControlError as exc:
        return Outcome(entry_id, title, "blocked", doc_id, exc.message)


def process_entries(session, entry_ids, actor, allowed=None) -> list[Outcome]:
    """Create / start / resume / reprocess the Documents behind ``entry_ids``.

    Phase 0 copies unstarted files under ``documents/`` without a lock; phase 1
    acts inside ``locked_files`` so a second request sees ``queued`` and skips.
    The Temporal call itself runs on commit (``enqueue_document_processing``).
    """
    ids = list(dict.fromkeys(str(i) for i in (entry_ids or [])))
    if not ids:
        return []
    precopies = {}
    if allowed is None or "not_started" in allowed:
        precopies = _precopy(session, ids)
    used: set = set()
    try:
        with session_files.locked_files(session, actor=actor) as state:
            outcomes = [
                _process_one(session, state, entry_id, actor, allowed, precopies, used)
                for entry_id in ids
            ]
    except Exception:
        _delete_precopies(precopies.values())
        raise
    _delete_precopies(v for k, v in precopies.items() if k not in used)
    return outcomes
