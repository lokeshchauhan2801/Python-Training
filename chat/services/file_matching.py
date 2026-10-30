"""Match a chat message to the files attached to the chat (design H, D7).

Pure functions: no DB access. A "file" is any object with ``id``, ``title``,
``file_type``, ``attached_at`` and ``readable`` (``session_files.SessionFile``
in production, a small dataclass in tests).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from documents.services import SUPPORTED_FILE_TYPES

# --------------------------------------------------------------------------- #
# Word sets
# --------------------------------------------------------------------------- #
D7_WORDS = frozenset({
    "create", "pie", "chart", "graph", "from", "file", "the", "this", "of",
    "for", "show", "a", "an", "my",
})
CHART_EXTRA = frozenset({
    "bar", "line", "plot", "make", "draw", "generate", "give", "me", "using",
    "with", "please", "and", "to", "in", "on", "by", "named", "called",
})
NAME_CUE = frozenset({
    "file", "files", "document", "documents", "doc", "docs", "sheet",
    "spreadsheet",
})
COMMAND_WORDS = frozenset({
    "process", "reprocess", "add", "save", "store", "upload", "put", "index",
    "ingest", "knowledge", "base", "kb", "company", "library", "documents",
    "document", "docs", "doc", "file", "files", "to", "into", "the", "this",
    "that", "these", "those", "it", "them", "a", "an", "my", "all", "please",
    "and", "stop", "cancel", "halt", "abort", "pause", "resume", "continue",
    "restart", "retry", "try", "again", "status", "progress", "show", "list",
    "of", "for", "is", "what", "which", "attached", "uploaded", "selected",
    "now",
    # R8
    "processing", "everything", "every", "each", "running", "current",
    "queued", "task", "tasks", "job", "jobs", "workflow", "workflows",
    "indexing", "embedding", "uploading", "uploads",
})
# V1 filler and state words.
FILLER = frozenset({
    "can", "could", "would", "will", "you", "your", "want", "need", "like",
    "just", "both", "have", "has", "are", "how", "doing", "right", "away",
    "don", "not", "here", "there", "chat", "from", "with", "using", "one",
    "ones", "new", "old", "latest", "recent", "other", "stopped", "failed",
    "processed", "pending", "done", "ready", "finished", "completed", "ok",
    "okay", "yes", "so", "pls", "kindly", "go", "ahead", "start", "begin",
    "on", "in", "me",
})
# Words that point at a file instead of naming it (review-3 F1).
REFERENCE_WORDS = frozenset({
    "that", "these", "those", "it", "its", "them", "they", "their", "we", "our",
    "attached", "uploaded", "added", "sent", "shared", "selected", "given",
    "provided", "previous", "prior", "last", "latest", "recent", "newest",
    "earlier", "above", "current", "same", "first", "second", "third",
    "another",
})
TYPE_ALIASES = {"excel": "xlsx"}  # feeds ext_filter; never a name token

CHART_STOPWORDS = D7_WORDS | CHART_EXTRA | NAME_CUE | FILLER | REFERENCE_WORDS
COMMAND_STOPWORDS = COMMAND_WORDS | FILLER | REFERENCE_WORDS

EXTS = frozenset(ext.lstrip(".") for ext in SUPPORTED_FILE_TYPES)

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
_DOTTED_NAME = re.compile(
    r"\b[\w\-]+(?:\.[\w\-]+)*\.(?:pdf|docx|txt|md|csv|xlsx|json|png|jpe?g|tiff?)\b"
)
_NAME_AFTER = frozenset({"named", "called"})
_SEGMENT_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bplus\b)\s*", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def _words(text: str | None) -> list[str]:
    return [w for w in _WORD_SPLIT.split((text or "").lower()) if w]


def _is_type_word(word: str) -> bool:
    return word in EXTS or word in TYPE_ALIASES


def _is_loose(word: str, stopwords) -> bool:
    return len(word) >= 3 and word not in stopwords and not _is_type_word(word)


def _dedupe(tokens) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _ext_filter(words: list[str]) -> frozenset[str]:
    exts = {w for w in words if w in EXTS}
    exts |= {TYPE_ALIASES[w] for w in words if w in TYPE_ALIASES}
    return frozenset(exts)


def name_tokens(text: str | None, stopwords) -> list[str]:
    """Loose tokens: words that could be part of a file name (H.1 step 3)."""
    return _dedupe(w for w in _words(text) if _is_loose(w, stopwords))


def explicit_name_tokens(text: str | None, stopwords) -> list[str]:
    """Tokens the user clearly meant as a file name (H.1 step 4)."""
    lowered = (text or "").lower()
    found: list[str] = []
    # (a) the base tokens of a dotted filename ("budget.csv" -> budget).
    for match in _DOTTED_NAME.finditer(lowered):
        found.extend(
            w for w in _words(match.group(0))
            if len(w) >= 3 and not _is_type_word(w)
        )
    words = _words(lowered)
    for index, word in enumerate(words):
        # (b) the run of loose tokens directly before an extension / name cue.
        if _is_type_word(word) or word in NAME_CUE:
            run: list[str] = []
            j = index - 1
            while j >= 0 and _is_loose(words[j], stopwords):
                run.insert(0, words[j])
                j -= 1
            found.extend(run)
        # (c) the run of loose tokens directly after "named" / "called".
        if word in _NAME_AFTER:
            j = index + 1
            while j < len(words) and _is_loose(words[j], stopwords):
                found.append(words[j])
                j += 1
    return _dedupe(found)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Match:
    kind: str  # "exact" | "prefix" | "none"
    files: tuple = ()
    name_tokens: tuple = ()
    explicit_tokens: tuple = ()
    ext_filter: frozenset = frozenset()

    @property
    def explicit_name(self) -> bool:
        return bool(self.explicit_tokens)


def _readable(files) -> list:
    return [f for f in files if getattr(f, "readable", True)]


def _file_ext(file) -> str:
    ext = (getattr(file, "file_type", "") or "").lower().lstrip(".")
    if ext:
        return ext
    title = (file.title or "").lower()
    return title.rsplit(".", 1)[-1] if "." in title else ""


def _base(title: str) -> str:
    lowered = (title or "").lower()
    return lowered.rsplit(".", 1)[0] if "." in lowered else lowered


def _newest(files):
    return max(files, key=lambda f: f.attached_at)


def _collapse_same_title(files) -> list:
    """Keep only the newest file per (case-insensitive) title, in input order."""
    newest: dict[str, object] = {}
    for f in files:
        key = (f.title or "").lower()
        if key not in newest or f.attached_at > newest[key].attached_at:
            newest[key] = f
    kept = {id(f) for f in newest.values()}
    return [f for f in files if id(f) in kept]


def _start_hit(token: str, file) -> bool:
    return _base(file.title).startswith(token)


def _inner_hit(token: str, file) -> bool:
    return any(
        ft.startswith(token) for ft in _WORD_SPLIT.split(_base(file.title)) if ft
    )


def prefix_hits(token: str, files, start_only: bool = True) -> list:
    """Readable files whose name starts with ``token`` (or a part of it does)."""
    token = (token or "").lower()
    if not token:
        return []
    return [
        f for f in _readable(files)
        if _start_hit(token, f) or (not start_only and _inner_hit(token, f))
    ]


def _exact_hits(text: str, files) -> list:
    hits = []
    for f in files:
        title = (f.title or "").lower()
        if not title:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(title) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            hits.append(f)
    return _collapse_same_title(hits)


def match_files(message: str | None, files, *, stopwords) -> Match:
    """Find the file(s) a message names (H.1)."""
    text = (message or "").lower()
    words = _words(text)
    candidates = _readable(files)
    loose = tuple(name_tokens(text, stopwords))
    explicit = tuple(explicit_name_tokens(text, stopwords))
    ext_filter = _ext_filter(words)

    def result(kind, matched=()):
        return Match(kind, tuple(matched), loose, explicit, ext_filter)

    exact = _exact_hits(text, candidates)
    if exact:
        return result("exact", exact)

    scored = []
    for f in candidates:
        if ext_filter and _file_ext(f) not in ext_filter:
            continue
        score = sum(
            1 for t in loose if _start_hit(t, f) or _inner_hit(t, f)
        )
        if score <= 0:
            continue
        if not explicit and not any(_start_hit(t, f) for t in loose):
            continue
        scored.append((score, f))
    if scored:
        top = max(score for score, _f in scored)
        best = _collapse_same_title([f for score, f in scored if score == top])
        return result("prefix", best)
    return result("none")


def split_segments(message: str | None) -> list[str]:
    """Split a file command on ",", "&", "and", "plus" (review-3 F6)."""
    parts = _SEGMENT_SPLIT.split(message or "")
    return [p.strip() for p in parts if p and p.strip()]


def match_command_segments(message: str | None, files, *, stopwords) -> list[Match]:
    """One Match per segment, or a single whole-message match for one segment."""
    segments = split_segments(message)
    if len(segments) <= 1:
        return [match_files(message, files, stopwords=stopwords)]
    return [match_files(s, files, stopwords=stopwords) for s in segments]


# --------------------------------------------------------------------------- #
# Chart target (H.2)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChartTarget:
    kind: str  # "no_files" | "file" | "ambiguous" | "not_found"
    file: object = None
    candidates: tuple = ()
    tokens: tuple = ()


def references_file(message: str | None, files=()) -> bool:
    """Whether the message expressly identifies a chat file or document."""
    text = (message or "").lower()
    if _DOTTED_NAME.search(text):
        return True
    if re.search(
        r"\b(?:this|that|my|the|attached|uploaded|from|in)\s+"
        r"(?:pdf|docx|txt|md|csv|xlsx|json|png|jpe?g|tiff?|image|picture|photo"
        r"|screenshot|attachment)\b",
        text,
    ):
        return True
    if re.search(r"\b(?:file|files|document|documents|doc|docs|sheet|spreadsheet)\b", text):
        return True
    match = match_files(text, files, stopwords=CHART_STOPWORDS)
    return match.kind in ("exact", "prefix") and bool(match.files)


def select_chart_file(files, message: str | None) -> ChartTarget:
    """Pick the file a chart request should use (D7, H.2)."""
    readable = _readable(files)
    if not readable:
        return ChartTarget("no_files")
    m = match_files(message, readable, stopwords=CHART_STOPWORDS)
    if m.kind in ("exact", "prefix"):
        if len(m.files) == 1:
            return ChartTarget("file", m.files[0], m.files, m.name_tokens)
        hit_tokens = tuple(
            t for t in m.name_tokens
            if any(_start_hit(t, f) or _inner_hit(t, f) for f in m.files)
        )
        return ChartTarget("ambiguous", None, m.files, hit_tokens or m.name_tokens)
    if m.explicit_name:
        return ChartTarget("not_found", None, (), m.explicit_tokens)
    if m.ext_filter:
        typed = [f for f in readable if _file_ext(f) in m.ext_filter]
        if typed:
            newest = _newest(typed)
            return ChartTarget("file", newest, (newest,), ())
        return ChartTarget("not_found", None, (), tuple(sorted(m.ext_filter)))
    newest = _newest(readable)
    return ChartTarget("file", newest, (newest,), ())


def _titles(files) -> str:
    return ", ".join(f.title for f in files)


def chart_target_reply(target: ChartTarget, files) -> str:
    """The deterministic reply for an ``ambiguous`` / ``not_found`` target."""
    tokens = " ".join(target.tokens)
    if target.kind == "ambiguous":
        first = target.candidates[0].title if target.candidates else ""
        return (
            f"I found several files matching “{tokens}”: "
            f"{_titles(target.candidates)}. Name the one you want, for example "
            f"“pie chart of {first}”."
        )
    if target.kind == "not_found":
        here = _titles(_readable(files))
        if target.tokens and all(t in EXTS for t in target.tokens):
            kinds = "/".join(t.upper() for t in target.tokens)
            return f"There is no {kinds} file in this chat. Files here: {here}."
        return (
            f"I couldn't find a file called “{tokens}” in this chat. "
            f"Files here: {here}."
        )
    return ""
