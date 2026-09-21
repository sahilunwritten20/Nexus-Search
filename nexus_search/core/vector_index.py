"""Vector index for Nexus Search Phase 4 (flat/brute-force implementation)."""
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from .embeddings import _deserialize_embedding, _serialize_embedding

logger = logging.getLogger("nexus_search.vector_index")

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@dataclass
class VectorSearchResult:
    doc_id: str
    score: float


class VectorIndex:
    def __init__(self, db_path: str = "nexus_search.db", model: str = DEFAULT_MODEL):
        self.db_path = db_path
        self.model = model
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        self._init_schema()
        self._embedding_cache: dict[str, np.ndarray] = {}
        self._cache_lock = threading.RLock()

    def _init_schema(self):
        with self.lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS vectors (
                    doc_id TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_vectors_model ON vectors (model)")
            self.conn.commit()

    def _load_all_embeddings(self) -> dict[str, np.ndarray]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT doc_id, embedding, dim FROM vectors WHERE model = ?",
                (self.model,)
            ).fetchall()
        return {doc_id: _deserialize_embedding(emb, dim) for doc_id, emb, dim in rows}

    def _refresh_cache(self):
        with self._cache_lock:
            self._embedding_cache = self._load_all_embeddings()

    def add(self, doc_id: str, embedding: np.ndarray, content_hash: str):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO vectors (doc_id, embedding, dim, model, content_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, _serialize_embedding(embedding), embedding.shape[0], self.model, content_hash, time.time())
            )
            self.conn.commit()
        with self._cache_lock:
            self._embedding_cache[doc_id] = embedding

    def remove(self, doc_id: str):
        with self.lock:
            self.conn.execute("DELETE FROM vectors WHERE doc_id = ? AND model = ?", (doc_id, self.model))
            self.conn.commit()
        with self._cache_lock:
            self._embedding_cache.pop(doc_id, None)

    def search(self, query_embedding: np.ndarray, top_k: int = 10, min_score: float = -1.0) -> list[VectorSearchResult]:
        with self._cache_lock:
            if not self._embedding_cache:
                self._refresh_cache()
            cache = dict(self._embedding_cache)

        if not cache:
            return []

        query_norm = np.linalg.norm(query_embedding)
        if query_norm == 0:
            return []
        query_normalized = query_embedding / query_norm

        results = []
        for doc_id, emb in cache.items():
            emb_norm = np.linalg.norm(emb)
            if emb_norm == 0:
                continue
            score = float(np.dot(query_normalized, emb / emb_norm))
            if score >= min_score:
                results.append(VectorSearchResult(doc_id=doc_id, score=score))

        results.sort(key=lambda r: -r.score)
        return results[:top_k]

    def get_embedding(self, doc_id: str) -> Optional[np.ndarray]:
        with self.lock:
            row = self.conn.execute(
                "SELECT embedding, dim FROM vectors WHERE doc_id = ? AND model = ?",
                (doc_id, self.model)
            ).fetchone()
        if row is None:
            return None
        return _deserialize_embedding(row[0], row[1])

    def count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM vectors WHERE model = ?", (self.model,)).fetchone()[0]

    def clear(self):
        with self.lock:
            self.conn.execute("DELETE FROM vectors WHERE model = ?", (self.model,))
            self.conn.commit()
        with self._cache_lock:
            self._embedding_cache.clear()

    def close(self):
        with self.lock:
            self.conn.close()


class VectorIndexManager:
    def __init__(self, db_path: str = "nexus_search.db", model: str = DEFAULT_MODEL):
        self.index = VectorIndex(db_path, model)
        self.model = model

    def upsert(self, doc_id: str, embedding: np.ndarray, content_hash: str):
        self.index.add(doc_id, embedding, content_hash)

    def delete(self, doc_id: str):
        self.index.remove(doc_id)

    def search(self, query_embedding: np.ndarray, top_k: int = 10, min_score: float = -1.0) -> list[VectorSearchResult]:
        return self.index.search(query_embedding, top_k, min_score)

    def search_by_doc_id(self, doc_id: str, top_k: int = 10) -> list[VectorSearchResult]:
        emb = self.index.get_embedding(doc_id)
        if emb is None:
            return []
        results = self.index.search(emb, top_k + 1)
        return [r for r in results if r.doc_id != doc_id][:top_k]

    def get_stats(self) -> dict:
        return {"count": self.index.count(), "model": self.model}

    def close(self):
        self.index.close()


def create_vector_index(db_path: str = "nexus_search.db", model: str = DEFAULT_MODEL) -> VectorIndexManager:
    return VectorIndexManager(db_path, model)