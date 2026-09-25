"""Files connector for Nexus Search.

Supports: TXT, Markdown, RST, CSV, JSON, HTML, PDF, DOCX, XLSX, PPTX.
Unreadable files are logged (never silently dropped) and skipped.
Oversized files are refused BEFORE read (NEXUS_MAX_INGEST_BYTES, default 64MB)
so a monster CSV can't OOM the process mid-parse.
"""
import csv
import json
import logging
import os
from pathlib import Path
from typing import Callable, Iterator, Optional

from ..types import IngestDoc

DEFAULT_MAX_INGEST_BYTES = 64 * 1024 * 1024  # 64 MiB

logger = logging.getLogger("nexus_search.ingestion.files")

DEFAULT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv", ".json", ".html", ".htm",
    ".pdf", ".docx", ".xlsx", ".pptx",
}
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}


def _detect_encoding(raw: bytes) -> str:
    """Best-effort encoding detection for text payloads.

    Uses charset-normalizer (already a transitive dep via requests, declared
    in requirements.txt). Falls back to UTF-8 with replacement when detection
    finds nothing sane — detection failure must degrade, not crash."""
    if not raw:
        return "utf-8"
    # BOMs first — detection libraries underweight them, and they're certain.
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"),
                     (b"\xef\xbb\xbf", "utf-8")):
        if raw.startswith(bom):
            return enc
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw[:65536]).best()  # sample-bound, not the whole file
        if best and best.encoding:
            enc = best.encoding
            # Single-byte legacy charsets: cp1252 is a latin-1 superset that
            # shares the high-byte range with the Western encodings the
            # detector can confuse (cp1250 etc.). Prefer it when the sample
            # is also valid cp1252 (i.e. contains none of its undefined
            # control bytes) — "São" must not come back as "Săo".
            if enc.lower().startswith(("cp125", "iso8859", "latin")):
                sample = raw[:65536]
                cp1252_undefined = {0x81, 0x8D, 0x8F, 0x90, 0x9D}
                if not any(b in cp1252_undefined for b in sample):
                    return "cp1252"
            return enc
    except Exception:
        pass
    return "utf-8"


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    return raw.decode(_detect_encoding(raw), errors="replace")


def read_csv_file(path: Path) -> str:
    """Stream a CSV row-by-row — no full-file materialization. Detection
    samples the first 64KB only; the body is decoded incrementally, so a
    40MB CSV costs a bounded window instead of three full-size copies."""
    import io
    with path.open("rb") as probe:
        sample = probe.read(65536)
    encoding = _detect_encoding(sample)
    with path.open("r", encoding=encoding, errors="replace") as f:
        reader = csv.DictReader(f)
        out_rows: list[str] = []
        for row in reader:
            line = " | ".join(f"{k}: {v}" for k, v in row.items() if k and v)
            if line:
                out_rows.append(line)
    return "\n".join(out_rows)


def read_json_file(path: Path) -> str:
    out: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                out.append(str(k))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            out.append(str(x))

    walk(json.loads(read_text_file(path)))
    return "\n".join(out)


def read_html_file(path: Path) -> str:
    from .web import parse_html

    return parse_html(read_text_file(path), path.as_uri()).content


def read_pdf_file(path: Path) -> str:
    from pypdf import PdfReader

    pages = (page.extract_text() for page in PdfReader(str(path)).pages)
    text = "\n".join(t for t in pages if t)
    if not text.strip():
        ocr = _ocr_pdf(path)
        if ocr:
            return ocr
    return text


def _ocr_pdf(path: Path) -> str:
    """OCR fallback for image-only (scanned) PDFs, opt-in via NEXUS_OCR=1.

    Needs pytesseract + a `tesseract` binary on PATH (and pdf2image+poppler).
    If unavailable we return "" and LOG it, not silently skip: a doc that
    yielded no text was already logged by read_file as unreadable/disabled."""
    import shutil as _shutil
    if os.environ.get("NEXUS_OCR") != "1":
        return ""
    if _shutil.which("tesseract") is None:
        logger.warning("NEXUS_OCR=1 but no tesseract binary on PATH; skipping OCR for %s", path)
        return ""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        logger.warning("NEXUS_OCR=1 but pytesseract/pdf2image not installed; skipping OCR for %s", path)
        return ""
    try:
        images = convert_from_path(str(path))
        return "\n".join(pytesseract.image_to_string(img) for img in images)
    except Exception as exc:
        logger.warning("OCR failed for %s: %s", path, exc)
        return ""


def read_docx_file(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    text = [p.text for p in document.paragraphs if p.text]
    for table in document.tables:  # tables were previously dropped
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                text.append(" | ".join(cells))
    return "\n".join(text)


def read_xlsx_file(path: Path) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        text = []
        for sheet in workbook.worksheets:
            text.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(v) for v in row if v is not None]
                if values:
                    text.append(" | ".join(values))
        return "\n".join(text)
    finally:
        workbook.close()


def read_pptx_file(path: Path) -> str:
    from pptx import Presentation

    text = []
    for n, slide in enumerate(Presentation(str(path)).slides, start=1):
        text.append(f"Slide: {n}")
        for shape in slide.shapes:
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    text.append(" | ".join(c.text for c in row.cells if c.text))
            elif getattr(shape, "text", ""):
                text.append(shape.text)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text
            if notes:
                text.append(notes)
    return "\n".join(text)


_READERS: dict[str, Callable[[Path], str]] = {
    ".txt": read_text_file, ".md": read_text_file, ".rst": read_text_file,
    ".csv": read_csv_file, ".json": read_json_file,
    ".html": read_html_file, ".htm": read_html_file,
    ".pdf": read_pdf_file, ".docx": read_docx_file,
    ".xlsx": read_xlsx_file, ".pptx": read_pptx_file,
}


def read_file(path: Path) -> str:
    """Read a file according to its extension; '' (and a logged warning) on
    failure or when the file exceeds the size limit."""
    reader = _READERS.get(path.suffix.lower())
    if reader is None:
        return ""
    limit = _max_ingest_bytes()
    try:
        size = path.stat().st_size  # stat BEFORE opening: size guard costs nothing
    except OSError:
        return ""
    if size > limit:
        logger.warning("Refusing to read %s: %d bytes exceeds NEXUS_MAX_INGEST_BYTES (%d)",
                       path, size, limit)
        return ""
    try:
        return reader(path)
    except Exception as exc:  # corrupt file, missing optional dependency, ...
        logger.warning("Could not read %s: %s: %s", path, type(exc).__name__, exc)
        return ""


def _max_ingest_bytes() -> int:
    raw = os.environ.get("NEXUS_MAX_INGEST_BYTES")
    if not raw:
        return DEFAULT_MAX_INGEST_BYTES
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid NEXUS_MAX_INGEST_BYTES %r; using default %d",
                       raw, DEFAULT_MAX_INGEST_BYTES)
        return DEFAULT_MAX_INGEST_BYTES


def iter_files(root: str, extensions: Optional[set[str]] = None) -> Iterator[IngestDoc]:
    extensions = {e.lower() for e in (extensions or DEFAULT_EXTENSIONS)}
    root_path = Path(root)

    for path in sorted(root_path.rglob("*")):
        rel = path.relative_to(root_path)
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        content = read_file(path)
        if not content.strip():
            logger.warning("No text extracted from %s (empty, scanned, or unreadable)", rel)
            continue
        yield IngestDoc(
            doc_id=f"file:{rel}",
            title=path.stem,
            content=content,
            doc_type="file",
            metadata={"path": str(rel), "extension": path.suffix.lower(), "filename": path.name},
        )