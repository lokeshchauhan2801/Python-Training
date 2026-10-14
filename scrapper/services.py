"""Scrapper orchestration service (SSRF-safe single fetch).

``ScrapeService.run`` performs one guarded HTTP(S) fetch for a ``ScrapeJob``. All
network and validation logic is contained here (not in the model/admin/API). The
service never raises for *expected* failures: it records a user-safe message on
the job, flips the status, and returns the job so callers (admin confirmation
flow, API run action) always get a clean result. Only programmer errors
propagate.

SSRF defenses, applied before any connection:

1. Scheme allow-list: only ``http`` / ``https``.
2. Reject embedded credentials (``user:pass@``) and ports outside {80, 443}.
3. Resolve ALL addresses via ``getaddrinfo`` and reject if ANY is
   private/loopback/link-local/reserved/multicast/unspecified (and the
   IPv4-mapped IPv6 equivalents). Defends DNS round-robin / rebinding.
4. Hard timeout (~10s) and a streamed response-size limit (~2MB).
5. Automatic redirects DISABLED (3xx is terminal).

Logging records the URL only, never secrets.
"""

import ipaddress
import logging
import socket
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
REQUEST_TIMEOUT = 10  # seconds (connect + read)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_CONTENT_CHARS = 200_000
USER_AGENT = "agentic-flow-scraper/1.0"


class ScrapeValidationError(Exception):
    """Raised internally when a URL fails the SSRF policy (expected failure)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable automatic redirect following; treat 3xx as terminal."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TextExtractor(HTMLParser):
    """Collect <title> and visible plain text, skipping script/style."""

    _SKIP = {"script", "style", "noscript", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._title_parts = []
        self._text_parts = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)
        elif self._skip_depth == 0:
            stripped = data.strip()
            if stripped:
                self._text_parts.append(stripped)

    @property
    def title(self):
        return " ".join("".join(self._title_parts).split())[:512]

    @property
    def text(self):
        return "\n".join(self._text_parts)[:MAX_CONTENT_CHARS]


def _ip_is_blocked(ip_str) -> bool:
    """Return True if an IP falls in any range that must never be fetched."""
    ip = ipaddress.ip_address(ip_str)
    # Collapse IPv4-mapped / IPv4-compatible IPv6 to the embedded IPv4 so the
    # same checks apply (e.g. ::ffff:127.0.0.1).
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_url(url: str) -> tuple[str, int]:
    """Validate scheme/host/port and resolve all addresses. Returns (host, port).

    Raises ``ScrapeValidationError`` (expected failure) on any policy breach.
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ScrapeValidationError("URL not allowed")
    if parts.username or parts.password:
        raise ScrapeValidationError("URL not allowed")

    host = parts.hostname
    if not host:
        raise ScrapeValidationError("URL not allowed")

    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    if port not in ALLOWED_PORTS:
        raise ScrapeValidationError("URL not allowed")

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ScrapeValidationError("Could not reach URL") from exc

    if not infos:
        raise ScrapeValidationError("Could not reach URL")

    # Reject if ANY resolved address is in a blocked range.
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if _ip_is_blocked(ip_str):
            raise ScrapeValidationError("URL not allowed")

    return host, port


class ScrapeService:
    def run(self, job, *, actor=None):
        """Execute a single safe fetch and record the outcome on the job."""
        from .models import ScrapeJob

        job.status = ScrapeJob.Status.RUNNING
        job.error = ""
        job.save(actor=actor, update_fields=["status", "error", "updated_at", "updated_by"])

        try:
            _validate_url(job.url)
            title, content = self._fetch(job.url)
        except ScrapeValidationError as exc:
            # Expected failure: record a user-safe message, do not log as error.
            logger.warning("Scrape rejected for url=%s: %s", job.url, exc)
            return self._fail(job, str(exc), actor)
        except _ScrapeError as exc:
            logger.warning("Scrape failed for url=%s: %s", job.url, exc)
            return self._fail(job, str(exc), actor)

        job.status = ScrapeJob.Status.SUCCESS
        job.title = title or job.title
        job.content = content
        job.error = ""
        job.save(
            actor=actor,
            update_fields=["status", "title", "content", "error", "updated_at", "updated_by"],
        )
        return job

    def _fail(self, job, message, actor):
        from .models import ScrapeJob

        job.status = ScrapeJob.Status.ERROR
        job.error = message
        job.save(
            actor=actor,
            update_fields=["status", "error", "updated_at", "updated_by"],
        )
        return job

    def _fetch(self, url: str) -> tuple[str, str]:
        """Fetch the URL with redirects disabled, timeout, and a size cap."""
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            response = opener.open(request, timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise _ScrapeError("Redirects are not allowed") from exc
            raise _ScrapeError(f"HTTP error {exc.code}") from exc
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise _ScrapeError("Could not reach URL") from exc

        with response:
            status = getattr(response, "status", None) or response.getcode()
            if status is not None and not (200 <= status < 300):
                raise _ScrapeError(f"HTTP error {status}")

            chunks = []
            total = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise _ScrapeError("Response too large")
                chunks.append(chunk)

            charset = response.headers.get_content_charset() or "utf-8"

        body = b"".join(chunks).decode(charset, errors="replace")
        parser = _TextExtractor()
        parser.feed(body)
        return parser.title, parser.text


class _ScrapeError(Exception):
    """Internal expected fetch failure, carrying a user-safe message."""
