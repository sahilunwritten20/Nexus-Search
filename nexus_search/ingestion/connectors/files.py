"""Files connector for Nexus Search.

Supports:
- TXT
- Markdown
- RST
- PDF
- DOCX
- XLSX
- PPTX
"""

from pathlib import Path
from typing import Iterator, Optional

from ..types import IngestDoc


DEFAULT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
}


def read_text_file(path: Path) -> str:
    """Read a normal text-based file."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def read_pdf_file(path: Path) -> str:
    """Extract text from a PDF file."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))

        text = []

        for page in reader.pages:
            page_text = page.extract_text()

            if page_text:
                text.append(page_text)

        return "\n".join(text)

    except Exception:
        return ""


def read_docx_file(path: Path) -> str:
    """Extract text from a DOCX file."""
    try:
        from docx import Document

        document = Document(str(path))

        text = []

        for paragraph in document.paragraphs:
            if paragraph.text:
                text.append(paragraph.text)

        return "\n".join(text)

    except Exception:
        return ""


def read_xlsx_file(path: Path) -> str:
    """Extract cell values from an XLSX file."""
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(
            filename=str(path),
            read_only=True,
            data_only=True,
        )

        text = []

        for sheet in workbook.worksheets:
            text.append(f"Sheet: {sheet.title}")

            for row in sheet.iter_rows(values_only=True):
                values = [
                    str(value)
                    for value in row
                    if value is not None
                ]

                if values:
                    text.append(" | ".join(values))

        workbook.close()

        return "\n".join(text)

    except Exception:
        return ""


def read_pptx_file(path: Path) -> str:
    """Extract text from a PPTX file."""
    try:
        from pptx import Presentation

        presentation = Presentation(str(path))

        text = []

        for slide_number, slide in enumerate(
            presentation.slides,
            start=1,
        ):
            text.append(f"Slide: {slide_number}")

            for shape in slide.shapes:
                if hasattr(shape, "text") and shape.text:
                    text.append(shape.text)

        return "\n".join(text)

    except Exception:
        return ""


def read_file(path: Path) -> str:
    """Read a file according to its extension."""

    extension = path.suffix.lower()

    if extension in {".txt", ".md", ".rst"}:
        return read_text_file(path)

    if extension == ".pdf":
        return read_pdf_file(path)

    if extension == ".docx":
        return read_docx_file(path)

    if extension == ".xlsx":
        return read_xlsx_file(path)

    if extension == ".pptx":
        return read_pptx_file(path)

    return ""


def iter_files(
    root: str,
    extensions: Optional[set[str]] = None,
) -> Iterator[IngestDoc]:

    extensions = extensions or DEFAULT_EXTENSIONS

    # Make sure extensions are lowercase.
    extensions = {
        extension.lower()
        for extension in extensions
    }

    root_path = Path(root)

    for path in sorted(root_path.rglob("*")):

        if not path.is_file():
            continue

        if path.suffix.lower() not in extensions:
            continue

        content = read_file(path)

        # Skip files where no text could be extracted.
        if not content.strip():
            continue

        rel = path.relative_to(root_path)

        yield IngestDoc(
            doc_id=f"file:{rel}",
            title=path.stem,
            content=content,
            doc_type="file",
            metadata={
                "path": str(rel),
                "extension": path.suffix.lower(),
                "filename": path.name,
            },
        )