"""MIME type detection for Nexus Search (WP6).

Two layers:
- sniff_content_type(path): magic bytes for the formats the ingestion
  pipeline actually parses — PDF (%PDF-), ZIP-container Office formats
  (docx/xlsx/pptx disambiguated by their internal [Content_Types]/part
  paths), text-vs-binary heuristic. Returns '' when undetermined.
- detect_mime_type(path): CONTENT first (a file with a lying extension
  routes by its real bytes — files.py depends on exactly that), extension
  (mimetypes) as the fallback for undeterminable content.
"""
import zipfile
from pathlib import Path

import mimetypes

_ZIP_OFFICE_MARKERS = (
    ("word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
)


def sniff_content_type(path: str) -> str:
    """Best-effort magic-byte sniffing of a real file. '' = undetermined
    (caller falls back to the extension). Never raises."""
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        with p.open("rb") as f:
            head = f.read(8)
        if not head:
            return ""
        if head.startswith(b"%PDF-"):
            return "application/pdf"
        if head.startswith(b"PK\x03\x04"):
            # ZIP container: only claim an Office type when the internal
            # part paths prove it — a plain .zip must stay generic.
            try:
                with zipfile.ZipFile(p) as zf:
                    names = zf.namelist()
                    for marker, mime in _ZIP_OFFICE_MARKERS:
                        if any(n.startswith(marker) for n in names):
                            return mime
                    if any(n == "[Content_Types].xml" for n in names):
                        return "application/octet-stream"  # Office-ish but unclaimed
            except (zipfile.BadZipFile, OSError):
                return "application/zip"
            return "application/zip"
        # text vs binary: NUL bytes or a majority of non-text bytes in the
        # first 4 KiB mean binary garbage that no text reader should parse
        with p.open("rb") as f:
            sample = f.read(4096)
        if b"\x00" in sample:
            return "application/octet-stream"
        text_chars = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127)
        if sample and text_chars / len(sample) < 0.7:
            return "application/octet-stream"
        return "text/plain"
    except OSError:
        return ""


def detect_mime_type(path: str) -> str:
    """MIME type for a file path: CONTENT first (magic bytes), extension
    (mimetypes) as fallback. A PDF named .txt reports as PDF — files.py
    routes readers by this, so a lying extension cannot misroute a file
    into the plain-text reader."""

    sniffed = sniff_content_type(path)
    if sniffed and sniffed != "application/octet-stream":
        return sniffed
    mime_type, _ = mimetypes.guess_type(path)
    if sniffed == "application/octet-stream" and not mime_type:
        return "application/octet-stream"
    return mime_type or "application/octet-stream"