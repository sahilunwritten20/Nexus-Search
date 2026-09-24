"""Ties connectors, dedup, quality gate, chunking and the Phase 1 indexer together.

ONE code path (`ingest_one`) serves both the batch CLI and the crawler, so
chunking, quality scoring and dedup behave identically everywhere.
"""
import hashlib
import logging
import threading
from typing import Iterable, Optional, Union

from ..core.hybrid_search import HybridSearch
from ..core.embedding_sync import EmbeddingSync
from ..core.indexer import Indexer
from .chunker import chunk_text
from .dedup import Deduplicator
from .mime import detect_mime_type
from .quality import content_quality_score
from .types import IngestDoc

logger = logging.getLogger("nexus_search.ingestion")


def _embed_indexed_docs(indexed_ids: list[str], indexer: Indexer,
                        target: Union["EmbeddingSync", "HybridSearch"]) -> None:
    """Generate and store embeddings for newly indexed documents.

    Works with either ingestion-time target:
    - EmbeddingSync -> write through its VectorStoreManager (content-hash
      gated, so this is a no-op if the same content was already embedded —
      e.g. because the sync is ALSO attached to the indexer and already
      handled the 'indexed' event).
    - HybridSearch  -> legacy path used by tests (embedder + vector index).
    """
    if isinstance(target, EmbeddingSync):
        for idx_id in indexed_ids:
            stored_doc = indexer.storage.get_document(idx_id)
            if stored_doc:
                text = f"{stored_doc.title} {stored_doc.content}"
                target.vector_store.upsert(
                    idx_id, text,
                    stored_doc.doc_type or "",
                    stored_doc.metadata.get("language", "") or "",
                )
        return
    for idx_id in indexed_ids:
        stored_doc = indexer.storage.get_document(idx_id)
        if stored_doc:
            text = f"{stored_doc.title} {stored_doc.content}"
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
            embedding = target.embedding_manager.get_or_compute(idx_id, text)
            target.vector_index.upsert(idx_id, embedding, content_hash)


def _index(indexer: Indexer, doc: IngestDoc, metadata: dict, chunk_size: Optional[int]) -> list[str]:
    """Index one doc, or (if chunk_size is set and the doc is longer) its chunks.
    Old chunks of the same parent are removed first so updates never leave stale ones.
    Returns list of indexed doc_ids (parent + chunks)."""
    indexed_ids = []
    for old in indexer.storage.chunk_ids(doc.doc_id):
        indexer.delete_document(old)
    if chunk_size and len(doc.content) > chunk_size:
        chunks = chunk_text(doc.content, chunk_size=chunk_size, overlap=min(50, chunk_size // 10), snap_to_space=True)
        indexer.delete_document(doc.doc_id)  # a previously un-chunked version
        for i, chunk in enumerate(chunks):
            chunk_id = f"{doc.doc_id}#chunk{i}"
            indexer.add_document(
                doc_id=chunk_id, content=chunk, title=doc.title, doc_type=doc.doc_type,
                metadata={**metadata, "parent_id": doc.doc_id, "chunk_index": i, "chunk_count": len(chunks)},
            )
            indexed_ids.append(chunk_id)
    else:
        indexer.add_document(
            doc_id=doc.doc_id, content=doc.content, title=doc.title, doc_type=doc.doc_type, metadata=metadata
        )
        indexed_ids.append(doc.doc_id)
    return indexed_ids


def _canonical_target_exists(indexer: Indexer, doc: IngestDoc) -> bool:
    """True when this page declares a canonical URL that DIFFERS from the
    crawled URL and the canonical target is already in the index.

    The crawler stores web pages as 'web:<url>'; a page whose canonical URL
    points elsewhere is the same document wearing a second address — indexing
    it again would create a near-duplicate result. This dedup signal is
    ADDITIVE to content-hash dedup (different content can still be a
    canonical duplicate), and only applies to crawled/web metadata."""
    canonical = str(doc.metadata.get("canonical_url") or "")
    url = str(doc.metadata.get("url") or "")
    if not canonical or not url or canonical == url:
        return False
    target_id = f"web:{canonical}"
    if indexer.storage.get_document(target_id) is not None:
        return True
    # Chunked parents have no parent row, only chunk rows.
    return bool(indexer.storage.chunk_ids(target_id))


def ingest_one(
    doc: IngestDoc,
    indexer: Indexer,
    dedup: Deduplicator,
    min_quality: Optional[float] = None,
    chunk_size: Optional[int] = None,
    sync: Optional[Union[EmbeddingSync, HybridSearch]] = None,
    hybrid: Optional[Union[EmbeddingSync, HybridSearch]] = None,
) -> str:
    """Dedup (content-hash + canonical URL) -> quality gate -> (chunk) ->
    index -> register -> embed. Returns 'indexed', 'duplicate' or 'low_quality'."""
    if dedup.is_duplicate(doc.content):
        return "duplicate"
    if _canonical_target_exists(indexer, doc):
        logger.info("DUPLICATE canonical %s (already indexed as %s)",
                    doc.metadata.get("url"), doc.metadata.get("canonical_url"))
        return "duplicate"
    quality = content_quality_score(doc.content)
    if min_quality is not None and quality < min_quality:
        return "low_quality"
    metadata = {**doc.metadata, "quality": round(quality, 3)}
    if "path" in metadata:
        metadata.setdefault("mime_type", detect_mime_type(str(metadata["path"])))
    indexed_ids = _index(indexer, doc, metadata, chunk_size)
    dedup.register(doc.content, doc.doc_id)

    # Support both `sync` and `hybrid` parameter names for backward compatibility
    target = sync if sync is not None else hybrid

    if target is not None:
        _embed_indexed_docs(indexed_ids, indexer, target)

    return "indexed"


def ingest_documents(
    docs: Iterable[IngestDoc],
    indexer: Indexer,
    dedup: Deduplicator,
    min_quality: Optional[float] = None,
    chunk_size: Optional[int] = None,
    sync: Optional[Union[EmbeddingSync, HybridSearch]] = None,
    hybrid: Optional[Union[EmbeddingSync, HybridSearch]] = None,
) -> dict:
    """Batch path - used by the CLI for files/code/product sources."""
    stats = {"indexed": 0, "duplicates": 0}
    if min_quality is not None:
        stats["low_quality"] = 0
    keys = {"indexed": "indexed", "duplicate": "duplicates", "low_quality": "low_quality"}
    for doc in docs:
        status = ingest_one(doc, indexer, dedup, min_quality=min_quality, chunk_size=chunk_size, sync=sync, hybrid=hybrid)
        stats[keys[status]] += 1
        logger.info("%s %s", status.upper(), doc.doc_id)
    return stats


def make_crawler_ingest_fn(
    indexer: Indexer,
    dedup: Deduplicator,
    min_quality: Optional[float] = None,
    chunk_size: Optional[int] = None,
    sync: Optional[Union[EmbeddingSync, HybridSearch]] = None,
    hybrid: Optional[Union[EmbeddingSync, HybridSearch]] = None,
):
    """Returns the (url, title, text, metadata) -> None callable CrawlPipeline
    expects as `ingest_fn`. Thread-safe: crawler workers call it concurrently,
    and dedup + index + register must happen as one step."""
    lock = threading.Lock()

    def ingest_fn(url: str, title: str, text: str, metadata: dict) -> None:
        doc = IngestDoc(doc_id=f"web:{url}", title=title, content=text, doc_type="web", metadata=metadata)
        with lock:
            # Support both `sync` and `hybrid` parameter names
            status = ingest_one(doc, indexer, dedup, min_quality=min_quality, chunk_size=chunk_size, sync=sync, hybrid=hybrid)
        if status != "indexed":
            logger.info("%s skip %s", status.upper(), url)

    return ingest_fn