"""Plain-text normalization for LLM-generated chat content."""

from __future__ import annotations

import json
import re

_FENCED_BLOCK_RE = re.compile(r"```[\s\S]*?(?:```|$)")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://[^\s<>()]+")
_BULLET_RE = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_BOLD_RE = re.compile(r"(?<![\w*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?![\w*])")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")


def normalize_llm_response(text: str) -> str:
    """Convert Markdown prose emphasis and list markers to plain text.

    Code, URLs, valid JSON payloads, ordinary hyphens, and mathematical
    multiplication are retained verbatim. Only ordinary prose emphasis and
    line-leading Markdown bullets are changed.
    """
    if not text:
        return text

    protected: list[str] = []

    def protect(value: str) -> str:
        protected.append(value)
        return f"\x00{len(protected) - 1}\x00"

    def protect_match(match: re.Match) -> str:
        return protect(match.group(0))

    cleaned = _FENCED_BLOCK_RE.sub(protect_match, text)
    cleaned = _INLINE_CODE_RE.sub(protect_match, cleaned)

    decoder = json.JSONDecoder()
    index = 0
    while index < len(cleaned):
        if cleaned[index] not in "[{":
            index += 1
            continue
        try:
            _, end = decoder.raw_decode(cleaned[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        value = cleaned[index : index + end]
        placeholder = protect(value)
        cleaned = cleaned[:index] + placeholder + cleaned[index + end :]
        index += len(placeholder)

    cleaned = _URL_RE.sub(protect_match, cleaned)
    cleaned = _BULLET_RE.sub(r"\1• ", cleaned)
    cleaned = _BOLD_RE.sub(r"\1", cleaned)
    cleaned = _ITALIC_RE.sub(r"\1", cleaned)

    for index, value in enumerate(protected):
        cleaned = cleaned.replace(f"\x00{index}\x00", value)
    return cleaned
