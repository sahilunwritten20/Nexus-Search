"""Indexing: turns raw documents into tokenized postings in storage."""
from collections import Counter
from typing import Optional

from .storage import Storage
from .tokenizer import tokenize


class Indexer:
    def __init__(self, storage: Storage):
        self.storage = storage

    def add_document(
        self,
        doc_id: str,
        content: str,
        title: str = "",
        doc_type: str = "text",
        metadata: Optional[dict] = None,
    ):
        """Tokenize and (re)index a document. Calling this again with the
        same doc_id replaces its previous content and postings entirely —
        this is how the ingestion connectors in Phase 2 handle updates.
        """
        tokens = tokenize(f"{title} {content}")
        term_freqs = Counter(tokens)
        self.storage.upsert_document(
            doc_id=doc_id,
            title=title,
            content=content,
            doc_type=doc_type,
            length=len(tokens),
            metadata=metadata or {},
        )
        self.storage.add_postings(doc_id, dict(term_freqs))
        self.storage.commit()

    def delete_document(self, doc_id: str) -> bool:
        return self.storage.delete_document(doc_id)
