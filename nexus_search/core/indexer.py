"""Indexing: turns raw documents into tokenized postings in storage."""
import logging
from collections import Counter
from typing import Callable, Optional

from .storage import Storage
from .tokenizer import tokenize

logger = logging.getLogger("nexus_search.indexer")

DocIdCallback = Callable[[str], None]


class Indexer:
    def __init__(self, storage: Storage):
        self.storage = storage
        self._listeners: dict[str, list[DocIdCallback]] = {"indexed": [], "deleted": []}

    def subscribe(
        self,
        on_indexed: Optional[DocIdCallback] = None,
        on_deleted: Optional[DocIdCallback] = None,
    ) -> None:
        """Register callbacks that receive a doc_id AFTER the change is committed
        (Phase 4 uses this to keep vector embeddings in sync). A callback that
        raises is logged and never breaks indexing."""
        if on_indexed:
            self._listeners["indexed"].append(on_indexed)
        if on_deleted:
            self._listeners["deleted"].append(on_deleted)

    def _emit(self, event: str, doc_id: str) -> None:
        for callback in list(self._listeners[event]):
            try:
                callback(doc_id)
            except Exception:
                logger.exception("%s listener failed for %s", event, doc_id)

    def add_document(
        self,
        doc_id: str,
        content: str,
        title: str = "",
        doc_type: str = "text",
        metadata: Optional[dict] = None,
    ):
        """Tokenize and (re)index a document. Calling this again with the
        same doc_id replaces its previous content and postings entirely."""
        tokens = tokenize(f"{title} {content}")
        term_freqs = Counter(tokens)
        with self.storage.lock:  # API threads / crawler workers share one connection
            self.storage.upsert_document(
                doc_id=doc_id, title=title, content=content, doc_type=doc_type,
                length=len(tokens), metadata=metadata or {},
            )
            self.storage.add_postings(doc_id, dict(term_freqs))
            self.storage.commit()
        self._emit("indexed", doc_id)

    def delete_document(self, doc_id: str) -> bool:
        """Delete a document AND its chunks (doc_id#chunkN).
        True if anything was deleted."""
        with self.storage.lock:
            targets = [doc_id] + self.storage.chunk_ids(doc_id)
            deleted = [d for d in targets if self.storage.delete_document(d)]
        for d in deleted:
            self._emit("deleted", d)
        return bool(deleted)