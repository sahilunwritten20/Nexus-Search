"""Files connector: walks a directory and yields indexable documents from
plain-text files (.txt, .md, .rst by default).
"""
from pathlib import Path
from typing import Iterator, Optional

from ..types import IngestDoc

DEFAULT_EXTENSIONS = {".txt", ".md", ".rst"}


def iter_files(root: str, extensions: Optional[set[str]] = None) -> Iterator[IngestDoc]:
    extensions = extensions or DEFAULT_EXTENSIONS
    root_path = Path(root)
    for path in sorted(root_path.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(root_path)
        yield IngestDoc(
            doc_id=f"file:{rel}",
            title=path.stem,
            content=content,
            doc_type="file",
            metadata={"path": str(rel), "extension": path.suffix},
        )
