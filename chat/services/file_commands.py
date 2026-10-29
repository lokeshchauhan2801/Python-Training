"""File commands typed in the chat (design F.2).

For now only the pure classifier: ``classify(text) -> Command``. It never reads
the session; the session-dependent gate (``effective_kind``) and the handler
come later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from chat.services import kb
from chat.services.chat_service import ChatService
from chat.services.file_matching import (
    COMMAND_STOPWORDS,
    explicit_name_tokens,
    name_tokens,
)


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
