"""Code connector: walks a directory and yields indexable documents from
source files, so code becomes searchable the same way docs and products are.
"""
import logging
from pathlib import Path
from typing import Iterator, Optional

from ..types import IngestDoc
from .files import _max_ingest_bytes  # same size policy as the files connector

logger = logging.getLogger("nexus_search.ingestion.code")

IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
DEFAULT_CODE_EXTENSIONS = {".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".rb", ".php"}


def iter_code(root: str, extensions: Optional[set[str]] = None) -> Iterator[IngestDoc]:
    extensions = extensions or DEFAULT_CODE_EXTENSIONS
    root_path = Path(root)
    limit = _max_ingest_bytes()
    for path in sorted(root_path.rglob("*")):
        rel = path.relative_to(root_path)
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        try:
            # stat BEFORE reading: a generated bundle must not OOM the run
            if path.stat().st_size > limit:
                logger.warning("Skipping oversized source file %s (> %d bytes)", rel, limit)
                continue
            # errors="replace", not "ignore": mojibake markers keep the byte
            # count (and the searchable token positions) honest
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read %s: %s", rel, exc)
            continue
        if not content.strip():
            continue
        yield IngestDoc(
            doc_id=f"code:{rel}",
            title=path.name,
            content=content,
            doc_type="code",
            metadata={"path": str(rel), "language": path.suffix.lstrip(".")},
        )