"""File commands typed in the chat (design F.1 step 5, F.2, F.3, G.4).

* ``classify(text) -> Command`` is pure: it never reads the session.
* ``effective_kind(cmd, session)`` gates STOP/RESUME/weak STATUS on the
  session's files, so ordinary chat use of those words reaches the LLM.
* ``handle_file_command(...) -> CommandReply`` answers a command
  deterministically (no LLM). ``views.send`` and the two LangGraph KB tools
  both call it, so both orchestration paths give the same reply.
* ``process_entries(...)`` creates / starts / resumes / reprocesses the
  Documents behind the chosen entries (knowledge-base processing).
* ``process_in_chat(...)`` processes entries only inside the chat
  (``chat_processing``): no Document, no chunks in the vector store.

A PROCESS command carries a ``destination``: ``"kb"`` when the user names the
company knowledge base / documents, ``"chat"`` for a plain "process this
file", and ``"ask"`` when the wording does not say (then the reply asks).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from chat.services import chat_processing, kb, session_files
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
    CANCEL = "cancel"
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
    # PROCESS only: "chat", "kb" or "ask".
    destination: str = ""


DEST_CHAT = chat_processing.DEST_CHAT
DEST_KB = chat_processing.DEST_KB
DEST_ASK = "ask"


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
    r"would\s+you|go\s+ahead\s+and|just|only)\W+)*"
)


def BARE(verbs: str) -> re.Pattern:
    return re.compile(
        r"^\W*" + POLITE + r"(?:" + verbs + r")"
        r"(?:\W+(?:it|that|this|them|now|please|all|everything))*\W*$",
        _I,
    )


STOP_V = r"stop|halt|pause"
CANCEL_V = r"cancel|abort|discard"
RESUME_V = r"resume|continue|restart|re-?start|retry|re-?try|try\s+again"

_STOP_RE = re.compile(r"\b(?:" + STOP_V + r")\b", _I)
_RESUME_RE = re.compile(r"\b(?:" + RESUME_V + r")\b", _I)
_CANCEL_RE = re.compile(r"\b(?:" + CANCEL_V + r")\b", _I)
_STOP_BARE = BARE(STOP_V)
_RESUME_BARE = BARE(RESUME_V)
_CANCEL_BARE = BARE(CANCEL_V)
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
# Wording that only asks to process (not to add to the knowledge base).
_PROCESS_ONLY_RULES = (
    PROCESS_DOC, _REPROCESS, _START_PROCESSING, _LEAD_PROCESS, _PROCESS_EXT,
)
_NEGATED_KB = re.compile(
    r"\b(?:don'?t|do\s+not|not|without|never)\b[^.]*"
    r"(?:knowledge\s*base|\bkb\b|company|documents)",
    _I,
)
_CHAT_ONLY_ANSWER = re.compile(r"\b(only|just|chat|here|process\w*)\b", _I)


def _wants_kb(text: str) -> bool:
    return not _NEGATED_KB.search(text) and bool(
        ADD_DEST.search(text) or KB_WORDS.search(text)
    )


def process_destination(text: str) -> str:
    """Where a PROCESS command sends the file: ``chat``, ``kb`` or ``ask``."""
    text = text or ""
    if _wants_kb(text):
        return DEST_KB
    if _NEGATED_KB.search(text) or any(r.search(text) for r in _PROCESS_ONLY_RULES):
        return DEST_CHAT
    return DEST_ASK


_AFFIRM = re.compile(
    r"^\W*(yes|yeah|yep|sure|ok(?:ay)?|do\s+it|go\s+ahead|please\s+do|confirm\w*)\b", _I
)


def kb_requested(session, message: str) -> bool:
    """True only when the user really asked for the knowledge base.

    Either the message names it, or it is a short "yes" to a previous reply
    that asked about adding the file to the knowledge base. The model's own
    choice is never enough: "process this file" stays in the chat.
    """
    message = message or ""
    if process_destination(message) == DEST_KB:
        return True
    if not _AFFIRM.search(message) or len(message.split()) > 8:
        return False
    turn = session.turns.order_by("-created_at").only("response").first()
    return bool(turn is not None and KB_WORDS.search(turn.response or ""))


def answer_destination(text: str) -> str | None:
    """The destination a reply to the "chat or knowledge base?" question picks."""
    text = text or ""
    if _wants_kb(text):
        return DEST_KB
    if _NEGATED_KB.search(text) or _CHAT_ONLY_ANSWER.search(text):
        return DEST_CHAT
    return None

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
    r"pending|stopped|paused|failed)\b",
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

    # STOP / CANCEL / RESUME: a chart request without a process word is never one.
    if not (is_chart and not _PROCESS_ANY.search(text)):
        for kind, verb_re, bare_re in (
            (Kind.STOP, _STOP_RE, _STOP_BARE),
            (Kind.CANCEL, _CANCEL_RE, _CANCEL_BARE),
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
        return _command(Kind.PROCESS, text, destination=process_destination(text))

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


def effective_kind_from_files(cmd: Command, session, files) -> str | None:
    """The command kind to run, decided against the passed-in ``files``.

    This is the READ-ONLY core of :func:`effective_kind`. It carries the exact
    STOP/RESUME eligibility and name-target logic but operates on the already
    listed ``files`` sequence instead of fetching session state, so it NEVER
    calls :func:`session_files.reconcile`, :func:`session_files.locked_files`,
    :func:`session_files.has_eligible`, or any ``save()``. The STATUS-weak
    branch still reads only the session ``context`` (no writes) via
    :func:`_has_started_file`.

    The capability preflight uses this directly so it can run against a stale
    processed/unprocessed list without mutating it.
    """
    if cmd.kind == Kind.NONE:
        return None
    if cmd.kind == Kind.STATUS:
        if cmd.weak and not _has_started_file(session):
            return None
        return Kind.STATUS
    if cmd.kind in (Kind.PROCESS, Kind.LIST):
        return cmd.kind
    # STOP / RESUME / CANCEL
    if cmd.needs_name:
        if cmd.loose_tokens and all(prefix_hits(t, files) for t in cmd.loose_tokens):
            return cmd.kind
        return None
    if cmd.bare:
        wanted = session_files._ELIGIBLE.get(cmd.kind, set())
        return cmd.kind if any(f.state in wanted for f in files) else None
    if not cmd.strong and any(not prefix_hits(t, files) for t in cmd.loose_tokens):
        return None
    return cmd.kind


def effective_kind(cmd: Command, session) -> str | None:
    """The command kind to run on this session, or None to route as chat.

    Normal-path wrapper: for STOP/RESUME it reconciles the session first (the
    real execution boundary), then delegates the decision to
    :func:`effective_kind_from_files` so the eligibility logic lives in exactly
    one place. Behaviour is byte-for-byte unchanged from before the split.
    """
    if cmd.kind in (Kind.STOP, Kind.RESUME, Kind.CANCEL):
        files = session_files.reconcile(session)
    else:
        # STATUS/PROCESS/LIST/NONE never consult the files sequence; avoid the
        # extra query/reconcile the old code also skipped for these kinds.
        files = ()
    return effective_kind_from_files(cmd, session, files)


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
NOTHING_TO_CANCEL = "There is nothing stopped, failed or processing to cancel."
START_FAILED = (
    "I couldn't start processing. Nothing was changed. Please try again."
)
STATUS_HEADER = "Here is the status of the files in this chat:"
PROCESS_ASK = (
    "Do you want me to process {names} only in this chat, or add {it} to the "
    "company knowledge base? Processing in this chat keeps the text in this "
    "conversation only; adding to the knowledge base also lists {it} in "
    "Documents for everyone in the company. Say “process only” or “add to "
    "knowledge base”."
)

PLURAL_CUE = re.compile(
    r"\b(all|both|these|those|them|everything|files|documents|docs|attachments)\b",
    _I,
)

_IN_FLIGHT = {"queued", "processing"}
_RESUMABLE = {"paused", "stopped", "failed"}
_CANCELLABLE = {"queued", "processing", "paused", "stopped", "failed"}


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


def _picker(entry_ids, note, destination) -> dict:
    """A picker panel whose Process button processes to ``destination``."""
    return {**_panel("picker", entry_ids, note), "destination": destination or DEST_CHAT}


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


def _chat_status_text(f) -> str:
    """``_status_text`` for a file processed only in this chat."""
    state = f.state
    proc = chat_processing.processing(f.entry)
    stage = chat_processing.stage_label(f.entry)
    progress = f"{int(proc.get('progress') or 0)}%"
    where = f"{stage}, {progress}" if stage else progress
    if state == "queued":
        return "queued"
    if state == "processing":
        return f"processing ({where})"
    if state == "paused":
        return (
            f"paused ({where}) — say “resume {f.title}” to continue or "
            f"“cancel {f.title}” to discard it"
        )
    if state == "failed":
        text = f"failed at {stage}" if stage else "failed"
        error = str(proc.get("error") or "").strip()[:200]
        if error:
            text += f" — {error}"
        text += ("" if text.endswith((".", "!", "?")) else ".") + " "
        return text + session_files.FAILED_HINT
    if state == "processed":
        return "completed (this chat only)"
    return "not started"


def _status_text(f) -> str:
    if f.destination == DEST_CHAT:
        return _chat_status_text(f)
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
    elif state == "paused":
        stage = controls.current_stage(doc)
        progress = f"{doc.processing_progress_display}%"
        where = f"{stage}, {progress}" if stage else progress
        text = (
            f"paused ({where}) — say “resume {f.title}” to continue or "
            f"“cancel {f.title}” to discard it"
        )
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
        text = "completed"
    else:
        text = "removed from Documents"
    return text


def _display_titles(files) -> dict:
    """Entry id -> title, with “(copy N)” on repeats of the same title."""
    seen = {}
    titles = {}
    for f in files:
        seen[f.title] = seen.get(f.title, 0) + 1
        n = seen[f.title]
        titles[f.id] = f"{f.title} (copy {n})" if n > 1 else f.title
    return titles


def _status_summary(targets) -> list:
    """A direct answer first; only files that aren't completed get a line.

    The tracker panel under the reply shows every row, so the text does not
    repeat the completed ones.
    """
    total = len(targets)
    done = sum(1 for f in targets if f.state == "processed")
    noun = "file" if total == 1 else "files"
    if done == total:
        if total == 1:
            return [f"{_q(targets[0].title)} is processed."]
        return [f"All {total} {noun} in this chat are processed."]
    titles = _display_titles(targets)
    lines = [f"{done} of {total} {noun} are processed. Not processed yet:"]
    lines += [
        f"- {titles[f.id]}: {_status_text(f)}"
        for f in targets
        if f.state != "processed"
    ]
    return lines


_TEMPORAL_WORDS = {
    "RUNNING": "running",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELED": "cancelled",
    "TERMINATED": "terminated",
    "TIMED_OUT": "timed out",
    "CONTINUED_AS_NEW": "continued as a new run",
}
_MAX_TEMPORAL_LOOKUPS = 10


def _temporal_lines(session, targets) -> list:
    """Each started file's workflow id and its status from the Temporal API.

    Reading the status also brings a chat-only file whose workflow died in
    Temporal in line with it (``chat_processing.temporal_status``).
    """
    started = [f for f in targets if f.workflow_id]
    if not started:
        return []
    lines = ["Workflows (status from Temporal):"]
    unreachable = False
    for f in started[:_MAX_TEMPORAL_LOOKUPS]:
        where = "this chat only" if f.destination == DEST_CHAT else "knowledge base"
        if unreachable:
            info = {"reachable": False}
        else:
            info = chat_processing.temporal_status(session, f.entry, f.workflow_id)
        if not info.get("reachable"):
            unreachable = True
            lines.append(f"- {f.title} ({where}): workflow {f.workflow_id}")
            continue
        if not info.get("found"):
            word = "not started in Temporal yet" if f.state == "queued" else "not found in Temporal"
        else:
            word = _TEMPORAL_WORDS.get(info.get("status"), str(info.get("status")).lower())
            state = info.get("state") or {}
            if info.get("status") == "RUNNING" and state.get("paused"):
                word = "running, paused"
            elif info.get("status") == "RUNNING" and state.get("stage"):
                word += f", at {str(state['stage']).replace('_', ' ')}"
        lines.append(f"- {f.title} ({where}): workflow {f.workflow_id} — {word}")
    if len(started) > _MAX_TEMPORAL_LOOKUPS:
        lines.append(f"(and {len(started) - _MAX_TEMPORAL_LOOKUPS} more)")
    if unreachable:
        lines.append("Temporal could not be reached, so this shows the last saved state.")
    return lines


def _status_reply(session, targets, lines, entry_ids) -> CommandReply:
    temporal = _temporal_lines(session, targets) if targets else []
    if temporal:
        # Temporal may have corrected a chat-only file's state.
        refreshed = {f.id: f for f in session_files.list_files(session)}
        targets = [refreshed.get(f.id, f) for f in targets]
    body = _status_summary(targets) if targets else []
    parts = [*lines, *body, *temporal]
    content = "\n".join(parts) if parts else STATUS_HEADER
    return CommandReply(content, {"file_panel": _panel("tracker", entry_ids)})


def _stop_lines(session, targets, actor=None) -> list:
    lines = []
    for f in targets:
        if f.state not in _IN_FLIGHT:
            lines.append(f"{_q(f.title)} isn't processing, so there is nothing to stop.")
            continue
        try:
            if f.destination == DEST_CHAT:
                chat_processing.pause(session, f.id, actor)
            else:
                controls.pause_document(f.document)
        except controls.ControlError as exc:
            lines.append(f"Couldn't stop {_q(f.title)}: {exc.message}")
            continue
        except Exception:  # noqa: BLE001 - one file must not break the reply
            logger.exception("Could not stop chat file %s", f.id)
            lines.append(f"Couldn't stop {_q(f.title)}. Please try again.")
            continue
        lines.append(
            f"Paused {_q(f.title)}{_wf(f.workflow_id)}. Say “resume” to continue "
            "from here or “cancel” to discard it."
        )
    return lines


def _cancel_lines(session, targets, actor=None) -> list:
    lines = []
    for f in targets:
        if f.state not in _CANCELLABLE:
            lines.append(f"{_q(f.title)} isn't stopped or processing, so there is nothing to cancel.")
            continue
        try:
            if f.destination == DEST_CHAT:
                chat_processing.cancel(session, f.id, actor)
            else:
                controls.cancel_document(f.document)
        except controls.ControlError as exc:
            lines.append(f"Couldn't cancel {_q(f.title)}: {exc.message}")
            continue
        except Exception:  # noqa: BLE001 - one file must not break the reply
            logger.exception("Could not cancel chat file %s", f.id)
            lines.append(f"Couldn't cancel {_q(f.title)}. Please try again.")
            continue
        lines.append(
            f"Cancelled {_q(f.title)}. Its progress was discarded; process it "
            "again to start over."
        )
    return lines


def _wf(workflow_id) -> str:
    return f" (workflow {workflow_id})" if workflow_id else ""


def _temporal_links(outcomes) -> list:
    links = []
    for outcome in outcomes:
        if outcome.result not in _QUEUED_RESULTS or not outcome.workflow_id:
            continue
        wf = outcome.workflow_id
        url = kb.temporal_ui_url(wf)
        if url:
            links.append({"title": outcome.title, "url": url, "workflow_id": wf})
    return links


def _run_processing(session, targets, actor, allowed, destination=DEST_KB):
    """``(lines, metadata_extra, failed)`` for PROCESS / RESUME targets.

    RESUME (``allowed`` set) continues each file where it runs: knowledge-base
    files through their Document, chat-only files through their chat run.
    PROCESS sends every target to ``destination``.
    """
    if allowed is not None:
        kb_targets = [f for f in targets if f.destination != DEST_CHAT]
        chat_targets = [f for f in targets if f.destination == DEST_CHAT]
    elif destination == DEST_KB:
        kb_targets, chat_targets = list(targets), []
    else:
        kb_targets, chat_targets = [], list(targets)
    try:
        outcomes = []
        if kb_targets:
            outcomes += process_entries(session, _ids(kb_targets), actor, allowed=allowed)
        if chat_targets:
            outcomes += process_in_chat(session, chat_targets, actor, allowed=allowed)
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


_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "last": -1,
}
_ORD = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|\d+(?:st|nd|rd|th))"
_FILE_NOUN = r"(?:files?|documents?|docs?|attachments?|ones?)"
_JOIN = r"\s*(?:,|and|&)\s*(?:the\s+)?"
# "second file", "the 2nd and 4th files", "last one"
ORDINAL_RE = re.compile(rf"\b({_ORD}(?:{_JOIN}{_ORD})*)\s+{_FILE_NOUN}\b", _I)
# "file 2", "files 2 and 3", "file number 2", "file #2"
NUMBERED_RE = re.compile(
    rf"\b{_FILE_NOUN}\s*(?:number|no\.?|#)?\s*(\d+(?:{_JOIN}\d+)*)\b", _I
)


def _positions(message) -> list:
    """1-based file positions the message asks for (-1 = last), in order."""
    found = []
    for m in ORDINAL_RE.finditer(message):
        for token in re.findall(_ORD, m.group(1), _I):
            token = token.lower()
            found.append(_ORDINAL_WORDS.get(token) or int(re.sub(r"\D", "", token)))
    for m in NUMBERED_RE.finditer(message):
        found += [int(n) for n in re.findall(r"\d+", m.group(1))]
    return list(dict.fromkeys(found))


def _position_reply(session, kind, message, files, actor, destination=None):
    """Targets picked by position (“process the second file”), or ``None``.

    Positions follow the table order (oldest attach first). A number past the
    end gets a line saying how many files the chat has.
    """
    positions = _positions(message)
    if not positions:
        return None
    total = len(files)
    targets, outside = [], []
    for n in positions:
        if n == -1:
            targets.append(files[-1])
        elif 1 <= n <= total:
            targets.append(files[n - 1])
        else:
            outside.append(n)
    targets = list({f.id: f for f in targets}.values())
    lines = []
    if outside:
        noun = "file" if total == 1 else "files"
        asked = ", ".join(str(n) for n in outside)
        lines.append(
            f"There {'is' if total == 1 else 'are'} only {total} {noun} in this "
            f"chat, so there is no file {asked}. Pick a number from 1 to {total}."
        )
    if not targets:
        return CommandReply(
            "\n".join(lines), {"file_panel": _panel("picker", None, lines[0])}
        )
    return _act(
        session, kind, targets, actor, entry_ids=_ids(targets), lines=lines,
        destination=destination,
    )


def pending_ask(session) -> dict | None:
    """The "chat or knowledge base?" question the latest reply asked, if any."""
    turn = session.turns.order_by("-created_at").only("metadata").first()
    ask = ((turn.metadata if turn is not None else None) or {}).get("process_ask")
    return ask if isinstance(ask, dict) else None


def _recent_file_ids(session, files) -> list:
    """Files the pending question named, else the latest turn's attachment."""
    known = {f.id for f in files}
    ask = pending_ask(session) or {}
    ids = [i for i in (ask.get("entry_ids") or []) if i in known]
    if ids:
        return ids
    turn = session.turns.order_by("-created_at").only("media_url").first()
    if turn is None:
        return []
    chips = session_files.chips_for_media(session, turn.media_url)
    return [c["id"] for c in chips if c.get("id") in known]


def _ask_reply(targets) -> CommandReply:
    if targets:
        names = ", ".join(_q(f.title) for f in targets)
        it = "it" if len(targets) == 1 else "them"
    else:
        names, it = "the file", "it"
    ids = _ids(targets) if targets else None
    return CommandReply(
        PROCESS_ASK.format(names=names, it=it), {"process_ask": {"entry_ids": ids}}
    )


def handle_file_command(
    *, session, kind, message, attachment_ids=None, entry_ids=None, actor=None,
    destination=None,
) -> CommandReply:
    """Answer one file command deterministically (design F.3).

    ``destination`` (PROCESS only) is ``"chat"``, ``"kb"`` or ``"ask"``; when
    omitted it is read from ``message``.
    """
    message = message or ""
    if kind == Kind.PROCESS and not destination:
        destination = process_destination(message)
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
        return _act(
            session, kind, targets, actor, entry_ids=_ids(targets), destination=destination
        )

    # 2. Names in the message; positions when no file name matched.
    names = _name_targets(message, files)
    if not names.files and not names.ambiguous:
        by_position = _position_reply(
            session, kind, message, files, actor, destination=destination
        )
        if by_position is not None:
            return by_position
    if names.named:
        return _named_reply(session, kind, names, actor, destination=destination)

    # 3. PROCESS only: this turn's attachments, else the file the last reply
    #    asked about (or the last turn's attachment).
    if kind == Kind.PROCESS:
        attached = [by_id[i] for i in dict.fromkeys(attachment_ids or []) if i in by_id]
        if not attached:
            attached = [by_id[i] for i in _recent_file_ids(session, files)]
        if len(attached) == 1 or (attached and PLURAL_CUE.search(message)):
            return _act(
                session, kind, attached, actor, entry_ids=_ids(attached),
                destination=destination,
            )
        if destination == DEST_ASK:
            return _ask_reply(attached)
        picker_ids = _ids(attached) if attached else None
        return CommandReply(
            PICKER_NOTE,
            {"file_panel": _picker(picker_ids, PICKER_NOTE, destination)},
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
    elif kind == Kind.CANCEL:
        # A bare "cancel" means the stopped ones; fall back to running ones.
        targets = [f for f in files if f.state in _RESUMABLE] or [
            f for f in files if f.state in _IN_FLIGHT
        ]
        if not targets:
            return CommandReply(NOTHING_TO_CANCEL)
    else:
        return _status_reply(session, files, [], None)
    return _act(session, kind, targets, actor, entry_ids=_ids(targets))


def _act(
    session, kind, targets, actor, *, entry_ids, lines=(), destination=None
) -> CommandReply:
    """Run ``kind`` on ``targets``; ``lines`` are name-table lines to prepend."""
    lines = list(lines)
    if kind == Kind.STATUS:
        return _status_reply(session, targets, lines, entry_ids)
    if kind == Kind.PROCESS and destination == DEST_ASK:
        return _ask_reply(targets)
    extra = {}
    if kind == Kind.STOP:
        lines += _stop_lines(session, targets, actor)
    elif kind == Kind.CANCEL:
        lines += _cancel_lines(session, targets, actor)
    elif targets:
        allowed = _RESUMABLE if kind == Kind.RESUME else None
        result_lines, extra, failed = _run_processing(
            session, targets, actor, allowed, destination or DEST_CHAT
        )
        if failed:
            return CommandReply(START_FAILED)
        lines += result_lines
    if not lines:
        lines = [{
            Kind.STOP: NOTHING_TO_STOP,
            Kind.RESUME: NOTHING_TO_RESUME,
            Kind.CANCEL: NOTHING_TO_CANCEL,
        }.get(kind, "There is nothing to process.")]
    return CommandReply(
        "\n".join(lines),
        {**extra, "file_panel": _panel("tracker", entry_ids)},
    )


def _named_reply(
    session, kind, names: _NameTargets, actor, destination=None
) -> CommandReply:
    """Targets from names; ambiguous / not-found segments add their own line."""
    targets = list(names.files)
    candidates = [f for _tokens, group in names.ambiguous for f in group]
    notes = []
    if kind == Kind.PROCESS and destination == DEST_ASK:
        return _ask_reply(list({f.id: f for f in [*targets, *candidates]}.values()))
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
            return _act(
                session, kind, targets, actor, entry_ids=_ids(targets),
                destination=destination,
            )
        lines = []
        extra = {}
        if targets:
            lines, extra, failed = _run_processing(
                session, targets, actor, None, destination or DEST_CHAT
            )
            if failed:
                return CommandReply(START_FAILED)
        note = " ".join(notes)
        picker_ids = None if names.missing else _ids([*targets, *candidates])
        return CommandReply(
            "\n".join([*lines, *notes]),
            {**extra, "file_panel": _picker(picker_ids, note, destination)},
        )

    lines = []
    if kind == Kind.STATUS:
        # Report every match once (SessionFile holds a dict, so dedupe by id).
        targets = list({f.id: f for f in [*targets, *candidates]}.values())
    else:
        button = {Kind.STOP: "Stop", Kind.CANCEL: "Cancel"}.get(kind, "Resume")
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
    result: str  # queued|resumed|reprocessed|processing|removed|not_allowed|missing|blocked|in_kb|unknown
    document_id: str | None = None
    detail: str = ""
    destination: str = DEST_KB
    workflow_id: str = ""

    @property
    def line(self) -> str:
        name = _q(self.title)
        where = (
            " in this chat only" if self.destination == DEST_CHAT
            else " for the company knowledge base"
        )
        wf = _wf(self.workflow_id)
        return {
            "queued": f"Queued {name} for processing{where}{wf}.",
            "resumed": f"Resuming {name} from the last completed step{wf}.",
            "reprocessed": f"Queued {name} for processing{where} again from the start{wf}.",
            "processing": f"{name} is already processing{wf}.",
            "in_kb": (
                f"{name} is already in the company knowledge base{wf}, so it "
                "is not processed again here."
            ),
            "removed": f"{name} was removed from Documents.",
            "not_allowed": f"{name} isn't paused, stopped or failed.",
            "missing": f"{name}: the file is missing; please attach it again.",
            "blocked": f"Couldn't process {name}: {self.detail}",
        }.get(self.result, f"{name} is no longer in this chat.")


def _delete_precopies(names) -> None:
    storage = Document._meta.get_field("file").storage
    for name in names:
        try:
            storage.delete(name)
        except OSError:
            logger.warning("Could not delete unused document copy %s", name, exc_info=True)


def _precopy(session, ids) -> dict:
    """Phase 0, no lock: copy unstarted or recoverable entries under documents/."""
    file_field = Document._meta.get_field("file")
    by_id = {f.id: f for f in session_files.list_files(session)}
    precopies = {}
    try:
        for entry_id in ids:
            f = by_id.get(entry_id)
            if f is None or (f.document_id and f.state != "removed"):
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
    if not entry.get("document_id") and chat_processing.is_chat_run(entry):
        # Processed (or being processed) only in this chat: adding it to the
        # knowledge base starts a Document; a running chat run must end first.
        if chat_processing.entry_state(entry) in _IN_FLIGHT:
            return Outcome(
                entry_id, title, "blocked", None,
                "it is still processing in this chat; stop it or wait for it to finish.",
            )
        current = "not_started"
    else:
        current = session_files.derive_state(entry, document)
    if current in _IN_FLIGHT:
        return Outcome(entry_id, title, "processing", doc_id)
    if allowed is not None and current not in allowed:
        return Outcome(entry_id, title, "not_allowed", doc_id)
    try:
        if current in {"not_started", "removed"} and document is None:
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
            entry["destination"] = DEST_KB
            if current == "removed":
                state.move_to_unprocessed(entry_id)
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


def _with_kb_workflow(outcome: Outcome) -> Outcome:
    if not outcome.document_id:
        return outcome
    from dataclasses import replace

    return replace(outcome, workflow_id=kb.workflow_id_for(outcome.document_id))


def process_entries(session, entry_ids, actor, allowed=None) -> list[Outcome]:
    """Create / start / resume / reprocess the Documents behind ``entry_ids``.

    Phase 0 copies unstarted or recoverable files under ``documents/`` without a
    lock; phase 1 acts inside ``locked_files`` so a second request sees
    ``queued`` and skips. The Temporal call itself runs on commit
    (``enqueue_document_processing``).
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
    return [_with_kb_workflow(o) for o in outcomes]


# --------------------------------------------------------------------------
# Chat-only processing (no Document, no vectors)
# --------------------------------------------------------------------------


def process_in_chat(session, targets, actor, allowed=None) -> list[Outcome]:
    """Start, resume or reprocess ``targets`` only inside this chat.

    The work runs in ``ChatFileProcessingWorkflow``; its text stays in the
    session context. A file already in the knowledge base is left there.
    ``allowed`` (RESUME) limits the action to paused / stopped / failed files.
    """
    outcomes = []
    resume_ids = []
    with session_files.locked_files(session, actor=actor) as state:
        for f in targets:
            found = state.find(f.id)
            if found is None:
                outcomes.append(Outcome(f.id, f.title, "unknown", destination=DEST_CHAT))
                continue
            entry = found[1]
            wf = chat_processing.workflow_id_for(f.id)
            current = f.state
            if f.destination == DEST_KB and current != "removed":
                outcomes.append(Outcome(
                    f.id, f.title, "in_kb", f.document_id,
                    workflow_id=kb.workflow_id_for(f.document_id),
                ))
                continue
            if current in _IN_FLIGHT:
                outcomes.append(Outcome(f.id, f.title, "processing", destination=DEST_CHAT, workflow_id=wf))
                continue
            if allowed is not None and current not in allowed:
                outcomes.append(Outcome(f.id, f.title, "not_allowed", destination=DEST_CHAT))
                continue
            if current == "paused":
                resume_ids.append(f.id)  # needs the resume signal, after the lock
                continue
            fresh = current != "failed"
            token = chat_processing.begin(state, entry, fresh=fresh)
            if current == "processed":
                state.move_to_unprocessed(f.id)
            chat_processing.dispatch_on_commit(session.pk, f.id, token)
            result = {"failed": "resumed", "processed": "reprocessed"}.get(current, "queued")
            outcomes.append(Outcome(f.id, f.title, result, destination=DEST_CHAT, workflow_id=wf))
    for entry_id in resume_ids:
        title = next(f.title for f in targets if f.id == entry_id)
        wf = chat_processing.workflow_id_for(entry_id)
        try:
            chat_processing.resume(session, entry_id, actor)
            outcomes.append(Outcome(entry_id, title, "resumed", destination=DEST_CHAT, workflow_id=wf))
        except controls.ControlError as exc:
            outcomes.append(Outcome(entry_id, title, "blocked", None, exc.message, DEST_CHAT, wf))
    return outcomes
