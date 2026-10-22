"""SSRF-safe background download for the file-OR-URL document source.

When a ``Document`` is created with a ``source_url`` instead of an uploaded
file, the media file is downloaded here on the background processing path, saved
onto ``document.file``, and then handed to the normal extraction pipeline.

The SSRF defense is NOT reimplemented: it reuses the tested scrapper policy
(``scrapper.services._validate_url`` → scheme allow-list, no credentials, port
allow-list, and rejection of ANY resolved private/loopback/link-local/reserved/
multicast/unspecified IP) plus the scrapper redirect-disabled opener, request
timeout, and streamed response-size cap. All network imports stay local to the
function so importing this module opens no socket.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

_CONTENT_TYPE_SUFFIX = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
}


class UrlSourceError(Exception):
    """A user-safe failure while downloading a document from its source URL."""


def _infer_filename(url: str, content_type: str) -> str:
    """Derive a safe ``name.ext`` for the downloaded file.

    The base name comes from the URL path; the suffix comes from the path or,
    failing that, from the Content-Type. Falls back to ``download`` + ``.bin``.
    """
    path = urlsplit(url).path
    base = os.path.basename(unquote(path)).strip() or "download"

    base = base.replace("/", "_").replace("\\", "_").replace("\x00", "")
    root, ext = os.path.splitext(base)
    if not ext:
        ctype = (content_type or "").split(";", 1)[0].strip().lower()
        ext = _CONTENT_TYPE_SUFFIX.get(ctype, ".bin")
        base = (root or "download") + ext
    return base


def download_source_to_document(document) -> None:
    """Download ``document.source_url`` and store it as ``document.file``.

    Raises :class:`UrlSourceError` (user-safe message) on any policy breach or
    fetch failure. On success the file bytes are saved onto the document (NOT
    committed — ``save=False``) and ``title``/``file_type``/``content_type``/
    ``file_size`` are updated to reflect the downloaded file; the caller is
    responsible for persisting and then running extraction.
    """
    import socket
    import urllib.error
    import urllib.request
    from pathlib import Path

    from django.core.files.base import ContentFile

    from scrapper.services import (
        MAX_RESPONSE_BYTES,
        REQUEST_TIMEOUT,
        USER_AGENT,
        ScrapeValidationError,
        _NoRedirect,
        _validate_url,
    )

    url = document.source_url
    if not url:
        raise UrlSourceError("No source URL to download.")


    try:
        _validate_url(url)
    except ScrapeValidationError as exc:
        raise UrlSourceError(str(exc)) from exc

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        response = opener.open(request, timeout=REQUEST_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            raise UrlSourceError("Redirects are not allowed") from exc
        raise UrlSourceError(f"HTTP error {exc.code}") from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise UrlSourceError("Could not reach URL") from exc

    with response:
        status = getattr(response, "status", None) or response.getcode()
        if status is not None and not (200 <= status < 300):
            raise UrlSourceError(f"HTTP error {status}")

        content_type = response.headers.get_content_type()
        chunks = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise UrlSourceError("Response too large")
            chunks.append(chunk)

    data = b"".join(chunks)
    if not data:
        raise UrlSourceError("The URL returned an empty response.")

    name = _infer_filename(url, content_type)
    document.file.save(name, ContentFile(data), save=False)
    document.title = name
    document.file_type = Path(name).suffix.lstrip(".").lower() or "bin"
    document.content_type = content_type or ""
    document.file_size = len(data)
