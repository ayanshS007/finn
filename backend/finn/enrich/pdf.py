"""Text of the first pages of a filing's attachment, for the LLM labeller and the quote check.

The PDF is downloaded through the shared HTTP client (on-disk cache, per-host spacing) and the text
is extracted with pdfplumber, falling back to pypdf. The extracted text is cached too, so a filing
is parsed once. A scanned PDF yields empty text; that is returned as empty, never guessed at.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from finn.config import cache_dir
from finn.sources.http import HttpClient, SourceError, shared_client

log = logging.getLogger(__name__)

PAGES = 3
TTL_PDF = 30 * 24 * 3600  # filings do not change
REFERER = {"Referer": "https://www.nseindia.com/"}
MIN_USEFUL_CHARS = 40  # fewer than this and the page is a scan or empty


@dataclass(frozen=True)
class PdfText:
    url: str
    text: str
    pages: int  # pages read (at most PAGES)
    engine: str  # pdfplumber | pypdf | none
    cached: bool = False


def _extract_plumber(data: bytes, pages: int) -> tuple[str, int]:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        chunks = [(page.extract_text() or "") for page in pdf.pages[:pages]]
    return "\n".join(chunks), len(chunks)


def _extract_pypdf(data: bytes, pages: int) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    chunks = [(page.extract_text() or "") for page in reader.pages[:pages]]
    return "\n".join(chunks), len(chunks)


def extract_text(data: bytes, pages: int = PAGES) -> tuple[str, int, str]:
    """(text, pages read, engine). pdfplumber first; pypdf when it fails or finds no text."""
    best: tuple[str, int, str] = ("", 0, "none")
    for engine, fn in (("pdfplumber", _extract_plumber), ("pypdf", _extract_pypdf)):
        try:
            text, n = fn(data, pages)
        except Exception as exc:  # a broken PDF must not stop the job
            log.warning("%s could not read the PDF: %s", engine, type(exc).__name__)
            continue
        if len(text.strip()) >= MIN_USEFUL_CHARS:
            return text.strip(), n, engine
        if n and best[1] == 0:
            best = (text.strip(), n, engine)
    return best


def _text_path(root: Path, url: str, pages: int) -> Path:
    digest = hashlib.sha256(f"{url}|{pages}".encode()).hexdigest()
    return root / "pdf_text" / f"{digest}.json"


def pdf_excerpt(
    url: str | None,
    pages: int = PAGES,
    *,
    http: HttpClient | None = None,
    root: Path | None = None,
) -> PdfText | None:
    """Text of the first `pages` pages, or None when there is no attachment or it cannot be had.

    A download error returns None (the caller labels from the subject alone); it is logged, not
    swallowed silently, and never replaced by invented text.
    """
    if not url:
        return None
    root = root or cache_dir()
    path = _text_path(root, url, pages)
    if path.exists():
        try:
            row = json.loads(path.read_text())
            return PdfText(url, row["text"], row["pages"], row["engine"], True)
        except (ValueError, KeyError, OSError):
            pass
    try:
        resp = (http or shared_client()).get(url, ttl=TTL_PDF, headers=REFERER)
    except SourceError as exc:
        log.warning("attachment not fetched (%s): %s", url, exc)
        return None
    if not resp.content.lstrip().startswith(b"%PDF"):
        log.warning("attachment is not a PDF (%s): %r", url, resp.content[:40])
        return None
    text, n, engine = extract_text(resp.content, pages)
    result = PdfText(url, text, n, engine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"text": text, "pages": n, "engine": engine}))
    return result
