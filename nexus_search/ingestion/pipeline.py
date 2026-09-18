"""Ties connectors, dedup, and the Phase 1 indexer together."""
import logging
from typing import Iterable

from ..core.indexer import Indexer
from .dedup import Deduplicator
from .types import IngestDoc

logger = logging.getLogger("nexus_search.ingestion")


def ingest_documents(docs: Iterable[IngestDoc], indexer: Indexer, dedup: Deduplicator) -> dict:
    """Batch path — used by the CLI for files/code/product sources."""
    stats = {"indexed": 0, "duplicates": 0}
    for doc in docs:
        if dedup.is_duplicate(doc.content):
            stats["duplicates"] += 1
            logger.info("DUPLICATE skip %s", doc.doc_id)
            continue
        indexer.add_document(
            doc_id=doc.doc_id, content=doc.content, title=doc.title,
            doc_type=doc.doc_type, metadata=doc.metadata,
        )
        dedup.register(doc.content, doc.doc_id)
        stats["indexed"] += 1
        logger.info("INDEXED %s", doc.doc_id)
    return stats


def make_crawler_ingest_fn(indexer: Indexer, dedup: Deduplicator):
    """Returns a (url, title, text, metadata) -> None callable — the exact
    shape Phase 3's CrawlPipeline expects as `ingest_fn`. This is the real
    integration point: pass this into CrawlPipeline instead of its
    placeholder `default_ingest`, and crawled pages flow straight into the
    real index with the same dedup used everywhere else.
    """
    def ingest_fn(url: str, title: str, text: str, metadata: dict) -> None:
        if dedup.is_duplicate(text):
            logger.info("DUPLICATE skip %s", url)
            return
        doc_id = f"web:{url}"
        indexer.add_document(doc_id=doc_id, content=text, title=title, doc_type="web", metadata=metadata)
        dedup.register(text, doc_id)

    return ingest_fn
