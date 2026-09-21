"""Embedding generation and caching for Nexus Search Phase 4."""
import hashlib
import json
import logging
import sqlite3
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger("nexus_search.embeddings")

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_model = None
_model_lock = threading.Lock()
_model_name = DEFAULT_MODEL


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                    _model = SentenceTransformer(_model_name)
                    logger.info("Loaded embedding model: %s", _model_name)
                except Exception as exc:
                    logger.warning("Failed to load sentence-transformers: %s. Using hash fallback.", exc)
                    _model = False
    return _model


def set_embedding_model(model_name: str):
    global _model, _model_name
    with _model_lock:
        _model_name = model_name
        _model = None


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _serialize_embedding(embedding: np.ndarray) -> bytes:
    return embedding.astype(np.float32).tobytes()


def _deserialize_embedding(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).reshape(-1)


def embed_texts(texts: list[str]) -> list[np.ndarray]:
    model = _get_model()
    if model is False:
        return [_hash_fallback_embedding(t) for t in texts]
    embeddings = model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    return [e.astype(np.float32) for e in embeddings]


def embed_text(text: str) -> np.ndarray:
    return embed_texts([text])[0]


def _hash_fallback_embedding(text: str) -> np.ndarray:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    arr = np.frombuffer(h, dtype=np.uint8).astype(np.float32)
    if len(arr) < EMBEDDING_DIM:
        arr = np.tile(arr, (EMBEDDING_DIM // len(arr) + 1))[:EMBEDDING_DIM]
    arr = (arr / 127.5) - 1.0
    return arr


class EmbeddingCache:
    def __init__(self, db_path: str = "nexus_search.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        with self.lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    doc_id TEXT PRIMARY KEY,
                    embedding BLOB NOT NULL,
                    dim INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings (model)")
            self.conn.commit()

    def get(self, doc_id: str, content_hash: str, model: str = DEFAULT_MODEL) -> Optional[np.ndarray]:
        with self.lock:
            row = self.conn.execute(
                "SELECT embedding, dim, content_hash FROM embeddings WHERE doc_id = ? AND model = ?",
                (doc_id, model)
            ).fetchone()
        if row is None:
            return None
        emb_blob, dim, stored_hash = row
        if stored_hash != content_hash:
            return None
        return _deserialize_embedding(emb_blob, dim)

    def set(self, doc_id: str, embedding: np.ndarray, content_hash: str, model: str = DEFAULT_MODEL):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO embeddings (doc_id, embedding, dim, model, content_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (doc_id, _serialize_embedding(embedding), embedding.shape[0], model, content_hash, time.time())
            )
            self.conn.commit()

    def delete(self, doc_id: str, model: str = DEFAULT_MODEL):
        with self.lock:
            self.conn.execute("DELETE FROM embeddings WHERE doc_id = ? AND model = ?", (doc_id, model))
            self.conn.commit()

    def batch_set(self, items: list[tuple[str, np.ndarray, str]], model: str = DEFAULT_MODEL):
        if not items:
            return
        now = time.time()
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO embeddings (doc_id, embedding, dim, model, content_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(doc_id, _serialize_embedding(emb), emb.shape[0], model, content_hash, now)
                 for doc_id, emb, content_hash in items]
            )
            self.conn.commit()

    def get_stats(self) -> dict:
        with self.lock:
            row = self.conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
            model_row = self.conn.execute("SELECT model, COUNT(*) FROM embeddings GROUP BY model").fetchall()
        return {"total": row[0], "by_model": dict(model_row)}

    def close(self):
        with self.lock:
            self.conn.close()


class EmbeddingManager:
    def __init__(self, db_path: str = "nexus_search.db", model: str = DEFAULT_MODEL):
        self.cache = EmbeddingCache(db_path)
        self.model = model
        self._batch_buffer: list[tuple[str, str]] = []
        self._batch_lock = threading.Lock()
        self._batch_size = 32

    def get_or_compute(self, doc_id: str, text: str) -> np.ndarray:
        content_hash = _hash_content(text)
        cached = self.cache.get(doc_id, content_hash, self.model)
        if cached is not None:
            return cached
        embedding = embed_text(text)
        self.cache.set(doc_id, embedding, content_hash, self.model)
        return embedding

    def batch_get_or_compute(self, items: list[tuple[str, str]]) -> list[np.ndarray]:
        to_compute = []
        results = [None] * len(items)
        for i, (doc_id, text) in enumerate(items):
            content_hash = _hash_content(text)
            cached = self.cache.get(doc_id, content_hash, self.model)
            if cached is not None:
                results[i] = cached
            else:
                to_compute.append((i, doc_id, text, content_hash))
        if to_compute:
            texts = [t for _, _, t, _ in to_compute]
            embeddings = embed_texts(texts)
            for (idx, doc_id, _, content_hash), emb in zip(to_compute, embeddings):
                self.cache.set(doc_id, emb, content_hash, self.model)
                results[idx] = emb
        return results

    def invalidate(self, doc_id: str):
        self.cache.delete(doc_id, self.model)

    def invalidate_batch(self, doc_ids: list[str]):
        for doc_id in doc_ids:
            self.invalidate(doc_id)

    def get_stats(self) -> dict:
        return self.cache.get_stats()

    def close(self):
        self.cache.close()


def create_embedding_manager(db_path: str = "nexus_search.db", model: str = DEFAULT_MODEL) -> EmbeddingManager:
    return EmbeddingManager(db_path, model)