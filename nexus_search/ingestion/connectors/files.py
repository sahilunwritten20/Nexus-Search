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
DEFAULT_MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024  # 512 MiB payload per zip
DEFAULT_MAX_PDF_PAGES = 10_000

logger = logging.getLogger("nexus_search.ingestion.files")

DEFAULT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv", ".json", ".html", ".htm",
    ".pdf", ".docx", ".xlsx", ".pptx",
}
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}


def _detect_encoding(raw: bytes) -> str:
    """Best-effort encoding detection for text payloads.

    Order (P0-4): BOM -> strict UTF-8 -> detector. charset-normalizer's
    statistical guess on SHORT samples is unreliable even on the pinned
    3.5.1 (measured: cp1252 "Café" -> utf_16_be, "São Paulo" -> big5 ->
    mojibake), so an odd/non-Western guess on a short sample is only
    trusted when it actually EXPLAINS the bytes better than cp1252:
    - Western single-byte guesses (cp125x/iso8859/latin/mac-latin): the
      existing cp1252 preference applies (shares the high-byte range).
    - CJK-family guesses: kept only when decoding with them yields
      CJK-heavy text (measured margin: real CJK ~100% of chars, misfires
      ~12%).
    - BOM-less utf_16/utf_32 guesses: kept only when NUL interleave or
      (>=32 bytes of) CJK output says the sample really is UTF-16. The
      residual trade — a tiny genuine BOM-less UTF-16 CJK file may flip
      to cp1252 — is accepted and documented; BOM'd UTF-16 is caught
      earlier and never reaches here.

    Falls back to UTF-8 with replacement when detection finds nothing
    sane — detection failure must degrade, not crash."""
    if not raw:
        return "utf-8"
    # BOMs first — detection libraries underweight them, and they're certain.
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"),
                     (b"\xef\xbb\xbf", "utf-8")):
        if raw.startswith(bom):
            return enc
    # Strict UTF-8: a clean decode is stronger evidence than any
    # statistical guess (and covers ASCII).
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        sample = raw[:65536]
        matches = from_bytes(sample)  # sample-bound, not the whole file
        odd_short = len(sample) < 8192
        best_enc = None
        for rank, m in enumerate(matches):
            enc = m.encoding
            enc_l = enc.lower()
            if rank == 0:
                best_enc = enc
            # Single-byte legacy Western charsets: cp1252 is a latin-1
            # superset that shares the high-byte range with the encodings
            # the detector confuses (cp1250, mac_latin2, ...). Prefer it
            # when the sample is also strictly valid cp1252 — "São" must
            # not come back as "Săo".
            if enc_l.startswith(("cp125", "iso8859", "iso-8859", "latin",
                                 "mac_latin", "mac_roman", "macintosh")):
                if _cp1252_clean(sample):
                    return "cp1252"
                if rank == 0:
                    return enc  # Western top guess, but bytes aren't cp1252
                continue
            # Odd/non-Western guesses on SHORT samples are the misfire zone
            # (P0-4): trust a guess only when it explains the bytes. The
            # candidate scan rescues e.g. a big5 file whose TOP guess was
            # a utf_16 misfire, while never introducing exotic codepages
            # (cp037, koi8_r, ...) the top guess didn't already claim.
            if odd_short and enc_l.startswith(_ODD_GUESS_PREFIXES):
                if _odd_guess_explains(sample, enc_l):
                    return enc
                continue
            if rank == 0:
                return enc  # top guess outside the misfire families: as-is
        # No candidate explained a short odd sample -> the cp1252 fallback
        # is the best remaining bet when the bytes allow it.
        if _cp1252_clean(sample):
            return "cp1252"
        return best_enc or "utf-8"
    except Exception:
        logger.debug("encoding detection failed; defaulting to utf-8",
                     exc_info=True)
    return "utf-8"


# cp1252 has five undefined bytes; a strict decode fails on any of them.
_CP1252_UNDEFINED = {0x81, 0x8D, 0x8F, 0x90, 0x9D}


def _cp1252_clean(raw: bytes) -> bool:
    """True when raw decodes strictly as cp1252 (no undefined control bytes)."""
    return not any(b in _CP1252_UNDEFINED for b in raw)


# The guess families observed to misfire on short Western samples
# (charset-normalizer 3.4.x: mac_latin2/utf_16_be/big5; 3.5.1: utf_16_be/big5).
_ODD_GUESS_PREFIXES = (
    "utf_16", "utf16", "utf_32", "utf32",          # BOM-less multi-byte
    "big5", "cp932", "cp950", "shift_jis", "sjis",  # CJK double-byte
    "gb2312", "gbk", "gb18030", "gb_", "euc_jp", "euc_kr", "cp949",
    "johab", "iso2022_jp", "iso2022_kr", "hz",
)


def _cjk_share(text: str) -> float:
    """Fraction of chars in CJK ranges (Han, kana, Hangul, compat)."""
    if not text:
        return 0.0
    cjk = sum(1 for ch in text
              if "\u3040" <= ch <= "\u30ff" or "\u3400" <= ch <= "\u4dbf"
              or "\u4e00" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7a3"
              or "\uf900" <= ch <= "\ufaff")
    return cjk / len(text)


def _odd_guess_explains(sample: bytes, enc_l: str) -> bool:
    """Does a CJK/UTF-16-family guess actually explain this short sample
    better than cp1252 would? Measured margins: real CJK decodes to
    ~100% CJK chars; Western misfires land at ~12%."""
    try:
        decoded = sample.decode(enc_l)
    except (UnicodeDecodeError, LookupError):
        return False  # guess can't even read the bytes -> misfire
    if enc_l.startswith(("utf_16", "utf16", "utf_32", "utf32")):
        # Genuine BOM-less UTF-16 of Latin text is ~50% NUL bytes; CJK
        # content has none but needs enough bytes for the guess to mean
        # anything (tiny samples are exactly the "Café" misfire zone).
        nul_share = sum(1 for b in sample if b == 0) / max(len(sample), 1)
        if nul_share >= 1 / 16:
            return True
        return len(sample) >= 32 and _cjk_share(decoded) >= 0.3
    return _cjk_share(decoded) >= 0.3


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    return raw.decode(_detect_encoding(raw), errors="replace")


def read_markdown_file(path: Path) -> str:
    """Markdown reader (WP6): everything stays searchable, but the MARKUP
    doesn't leak into the index as noise:
    - YAML front matter (leading --- block) is dropped
    - fenced code blocks keep their text, drop the ``` fences + language tags
    - heading markers (#) are dropped, heading text kept
    - links [text](url) keep the text; images ![alt](url) keep the alt
    - emphasis/strong/strikethrough markers are unwrapped
    - blockquote markers and list bullets are stripped
    """
    text = read_text_file(path)

    # front matter: a leading --- ... --- block
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:].lstrip("\n")

    out_lines = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue  # fence markers and language tags are not content
        if in_fence:
            out_lines.append(line)  # code text verbatim
            continue
        # heading markers
        if stripped.startswith("#"):
            line = line.lstrip("# \t")
        # blockquote markers
        if stripped.startswith(">"):
            line = line.lstrip("> ")
        # images before links (they share the bracket syntax)
        import re as _re
        line = _re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
        # links: keep the anchor text, drop the URL
        line = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        # emphasis / strong / strikethrough
        line = _re.sub(r"(\*\*\*|___|\*\*|__|~~|_|\*)(?=\S)(.*?\S)\1", r"\2", line)
        # list bullets
        line = _re.sub(r"^(\s*)[-*+]\s+", r"\1", line)
        out_lines.append(line.rstrip())
    return "\n".join(out_lines).strip("\n")


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

    reader = PdfReader(str(path))
    # P1-12a: "huge pages" guard — a pathological page count means
    # pathological parse time; refuse before extracting anything.
    max_pages = _max_pdf_pages()
    if len(reader.pages) > max_pages:
        raise ValueError(f"{len(reader.pages)} pages exceeds "
                         f"NEXUS_MAX_PDF_PAGES ({max_pages})")
    pages = (page.extract_text() for page in reader.pages)
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


def _check_zip_payload(path: Path) -> None:
    """P1-12a: refuse zip containers whose DECLARED decompressed payload
    exceeds NEXUS_MAX_DECOMPRESSED_BYTES, BEFORE any reader materializes
    it. NEXUS_MAX_INGEST_BYTES caps the file on disk, not the payload —
    a ~200 KiB 'docx' whose XML entries declare gigabytes (the zip-bomb
    class) would otherwise be fully inflated by the OOXML reader. The
    check reads only the zip central directory (declared sizes), no
    decompression. A header that lies SMALL truncates harmlessly inside
    Python's zipfile; a header that lies LARGE just means the guard fires
    early — both safe directions."""
    import zipfile
    bound = _max_decompressed_bytes()
    total = 0
    with zipfile.ZipFile(str(path)) as zf:
        for info in zf.infolist():
            total += info.file_size
            if total > bound:
                raise ValueError(
                    f"declared decompressed payload {total} bytes exceeds "
                    f"NEXUS_MAX_DECOMPRESSED_BYTES ({bound})")


def read_docx_file(path: Path) -> str:
    from docx import Document

    _check_zip_payload(path)
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

    _check_zip_payload(path)
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

    _check_zip_payload(path)
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
    ".txt": read_text_file, ".md": read_markdown_file, ".markdown": read_markdown_file,
    ".rst": read_text_file, ".csv": read_csv_file, ".json": read_json_file,
    ".html": read_html_file, ".htm": read_html_file,
    ".pdf": read_pdf_file, ".docx": read_docx_file,
    ".xlsx": read_xlsx_file, ".pptx": read_pptx_file,
}

# content-type -> reader: magic bytes beat lying extensions (WP6)
_CONTENT_READERS = {
    "application/pdf": read_pdf_file,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": read_docx_file,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": read_xlsx_file,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": read_pptx_file,
}


def read_file(path: Path) -> str:
    """Read a file; '' (and a logged warning) on failure or over the size
    limit. Routing: magic bytes first (a PDF named .txt parses as PDF),
    extension second — `mime.py` holds the detection policy."""
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
        from ..mime import sniff_content_type
        sniffed = sniff_content_type(path)
        if sniffed == "application/octet-stream":
            logger.warning("Refusing to read %s: binary content", path)
            return ""
        reader = _CONTENT_READERS.get(sniffed)
        if reader is not None:
            return reader(path)
        reader = _READERS.get(path.suffix.lower())
        if reader is None:
            return ""
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


def _max_decompressed_bytes() -> int:
    raw = os.environ.get("NEXUS_MAX_DECOMPRESSED_BYTES")
    if not raw:
        return DEFAULT_MAX_DECOMPRESSED_BYTES
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid NEXUS_MAX_DECOMPRESSED_BYTES %r; using default %d",
                       raw, DEFAULT_MAX_DECOMPRESSED_BYTES)
        return DEFAULT_MAX_DECOMPRESSED_BYTES


def _max_pdf_pages() -> int:
    raw = os.environ.get("NEXUS_MAX_PDF_PAGES")
    if not raw:
        return DEFAULT_MAX_PDF_PAGES
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid NEXUS_MAX_PDF_PAGES %r; using default %d",
                       raw, DEFAULT_MAX_PDF_PAGES)
        return DEFAULT_MAX_PDF_PAGES


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