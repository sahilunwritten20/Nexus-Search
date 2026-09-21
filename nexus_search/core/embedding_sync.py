"""EmbeddingSync: single sync layer for all entry points (T3).

Subscribes to Indexer events, batches embedding generation, and handles
failures without breaking indexing. Used by API, ingestion CLI, crawler CLI,
and benchmark.
"""
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, List

from ..core.indexer import Indexer
from ..core.storage import Storage
from ..core.vector_store import VectorStoreManager
from ..core.embedders import Embedder, EmbedderUnavailable, get_embedder

logger = logging.getLogger("nexus_search.embedding_sync")


@dataclass
class SyncStats:
    indexed: int = 0
    updated: int = 0
    deleted: int = 0
    errors: int = 0
    last_flush: float = 0.0


class EmbeddingSync:
    """Single sync layer for all entry points.
    
    Subscribes to Indexer.indexed/deleted events, queues work, and flushes
    in batches. Failures are counted and logged, never raised into indexing.
    """
    
    def __init__(
        self,
        vector_store: VectorStoreManager,
        batch_size: int = 32,
    ):
        self.vector_store = vector_store
        self.batch_size = batch_size
        self._indexer: Optional[Indexer] = None
        self._queue: List[tuple[str, str, str, str, str]] = []  # (op, doc_id, text, doc_type, language)
        self._queue_lock = threading.Lock()
        self._stats = SyncStats()
        self._stats_lock = threading.Lock()
        self._closed = False
    
    def attach(self, indexer: Indexer) -> None:
        """Attach to an Indexer to receive indexed/deleted events."""
        if self._indexer is not None:
            raise RuntimeError("Already attached to an indexer")
        self._indexer = indexer
        indexer.subscribe(on_indexed=self._on_indexed, on_deleted=self._on_deleted)
        logger.info("EmbeddingSync attached to indexer")
    
    def _on_indexed(self, doc_id: str) -> None:
        """Called when a document is indexed (or re-indexed)."""
        if self._closed:
            return
        # Get the document to extract text for embedding
        doc = self._indexer.storage.get_document(doc_id)
        if doc is None:
            return
        text = f"{doc.title} {doc.content}"
        doc_type = doc.doc_type or ""
        language = doc.metadata.get("language", "") or ""
        self._enqueue("upsert", doc_id, text, doc_type, language)
    
    def _on_deleted(self, doc_id: str) -> None:
        """Called when a document is deleted."""
        if self._closed:
            return
        self._enqueue("delete", doc_id, "", "", "")
    
    def _enqueue(self, op: str, doc_id: str, text: str = "", doc_type: str = "", language: str = "") -> None:
        """Add work to the queue."""
        with self._queue_lock:
            self._queue.append((op, doc_id, text, doc_type, language))
            # Auto-flush if batch size reached
            if len(self._queue) >= self.batch_size:
                self._flush_locked()
    
    def _flush_locked(self) -> None:
        """Flush the queue (must hold _queue_lock)."""
        if not self._queue:
            return
        
        items = self._queue
        self._queue = []
        
        # Process outside the lock to avoid blocking indexing
        # (but we need to process errors without raising)
        for op, doc_id, text, doc_type, language in items:
            try:
                if op == "upsert":
                    self.vector_store.upsert(doc_id, text, doc_type, language)
                    with self._stats_lock:
                        self._stats.indexed += 1
                elif op == "delete":
                    self.vector_store.delete(doc_id)
                    with self._stats_lock:
                        self._stats.deleted += 1
            except EmbedderUnavailable as exc:
                logger.warning("Embedder unavailable, skipping embedding for %s: %s", doc_id, exc)
                with self._stats_lock:
                    self._stats.errors += 1
            except Exception as exc:
                logger.exception("Failed to process embedding for %s: %s", doc_id, exc)
                with self._stats_lock:
                    self._stats.errors += 1
        
        self._stats.last_flush = time.time()
    
    def flush(self) -> None:
        """Flush any pending work. Call at end of CLI runs and crawls."""
        with self._queue_lock:
            self._flush_locked()
    
    def get_stats(self) -> dict:
        with self._stats_lock:
            return {
                "indexed": self._stats.indexed,
                "updated": self._stats.updated,
                "deleted": self._stats.deleted,
                "errors": self._stats.errors,
                "last_flush": self._stats.last_flush,
                "queue_size": len(self._queue),
            }
    
    def close(self) -> None:
        """Close the sync layer, flushing any pending work."""
        self._closed = True
        self.flush()


def create_embedding_sync(
    vector_store: VectorStoreManager,
    batch_size: int = 32,
) -> EmbeddingSync:
    return EmbeddingSync(vector_store, batch_size)