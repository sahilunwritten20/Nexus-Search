"""Code connector: walks a directory and yields indexable documents from
source files, so code becomes searchable the same way docs and products are.
"""
from pathlib import Path
from typing import Iterator, Optional

from ..types import IngestDoc

IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}
DEFAULT_CODE_EXTENSIONS = {".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".rb", ".php"}


def iter_code(root: str, extensions: Optional[set[str]] = None) -> Iterator[IngestDoc]:
    extensions = extensions or DEFAULT_CODE_EXTENSIONS
    root_path = Path(root)
    for path in sorted(root_path.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(root_path).parts):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(root_path)
        yield IngestDoc(
            doc_id=f"code:{rel}",
            title=path.name,
            content=content,
            doc_type="code",
            metadata={"path": str(rel), "language": path.suffix.lstrip(".")},
        )