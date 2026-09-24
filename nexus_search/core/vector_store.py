"""Unified vector store for Nexus Search Phase 4 (T2).

Replaces both `embeddings` and `vectors` tables with a single `doc_vectors` table.
Provides in-memory L2-normalized matrix with efficient search via matrix @ query.
"""
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable
import numpy as np

from .embedders import get_embedder, EmbedderUnavailable, Embedder

logger = logging.getLogger("nexus_search.vector_store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_vectors (
    doc_id TEXT NOT NULL,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    vector BLOB NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (doc_id, model)
);
CREATE INDEX IF NOT EXISTS idx_doc_vectors_model_hash ON doc_vectors (model, content_hash);
CREATE INDEX IF NOT EXISTS idx_doc_vectors_updated ON doc_vectors (updated_at);
"""

# Legacy tables to be dropped
LEGACY_TABLES = ["embeddings", "vectors"]


@dataclass
class VectorSearchResult:
    doc_id: str
    score: float


class VectorStore:
    """Unified vector store with in-memory matrix for fast search."""
    
    def __init__(
        self,
        db_path: str = "nexus_search.db",
        embedder: Optional[Embedder] = None,
    ):
        self.db_path = db_path
        self.embedder = embedder or get_embedder()
        self.model = self.embedder.name
        self.dim = self.embedder.dim
        
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        
        self._init_schema()
        self._drop_legacy_tables()
        
        # In-memory matrix: (n_vectors, dim) float32, L2-normalized
        self._matrix: np.ndarray = np.empty((0, self.dim), dtype=np.float32)
        self._doc_ids: np.ndarray = np.empty(0, dtype=object)
        self._doc_types: np.ndarray = np.empty(0, dtype=object)
        self._languages: np.ndarray = np.empty(0, dtype=object)
        self._content_hashes: dict[str, str] = {}  # doc_id -> content_hash
        self._matrix_lock = threading.RLock()
        
        # Freshness tracking
        self._data_version = 0
        self._last_reload_time = 0.0
        
        self._load_matrix()
    
    def _init_schema(self):
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
    
    def _drop_legacy_tables(self):
        """Drop legacy embeddings/vectors tables on first open."""
        with self.lock:
            for table in LEGACY_TABLES:
                exists = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if exists:
                    logger.info("Dropping legacy table: %s", table)
                    self.conn.execute(f"DROP TABLE {table}")
            self.conn.commit()
    
    def _load_matrix(self):
        """Load all vectors for this model into memory."""
        with self.lock:
            # Try to join with documents for doc_type/language, but handle missing table
            try:
                rows = self.conn.execute(
                    """
                    SELECT dv.doc_id, dv.vector, dv.content_hash, d.doc_type, 
                           COALESCE(d.metadata, '{}') as metadata
                    FROM doc_vectors dv
                    LEFT JOIN documents d ON d.doc_id = dv.doc_id
                    WHERE dv.model = ? AND dv.dim = ?
                    """,
                    (self.model, self.dim)
                ).fetchall()
            except sqlite3.OperationalError:
                # Documents table doesn't exist yet - load without metadata
                rows = self.conn.execute(
                    """
                    SELECT dv.doc_id, dv.vector, dv.content_hash, '', '{}'
                    FROM doc_vectors dv
                    WHERE dv.model = ? AND dv.dim = ?
                    """,
                    (self.model, self.dim)
                ).fetchall()
        
        if not rows:
            with self._matrix_lock:
                self._matrix = np.empty((0, self.dim), dtype=np.float32)
                self._doc_ids = np.empty(0, dtype=object)
                self._doc_types = np.empty(0, dtype=object)
                self._languages = np.empty(0, dtype=object)
                self._content_hashes = {}
            return
        
        import json
        n = len(rows)
        matrix = np.empty((n, self.dim), dtype=np.float32)
        doc_ids = np.empty(n, dtype=object)
        doc_types = np.empty(n, dtype=object)
        languages = np.empty(n, dtype=object)
        content_hashes = {}
        
        for i, (doc_id, vec_blob, content_hash, doc_type, metadata) in enumerate(rows):
            vec = np.frombuffer(vec_blob, dtype=np.float32)
            matrix[i] = vec
            doc_ids[i] = doc_id
            doc_types[i] = doc_type or ""
            meta = json.loads(metadata) if metadata else {}
            languages[i] = meta.get("language", "") or ""
            content_hashes[doc_id] = content_hash
        
        with self._matrix_lock:
            self._matrix = matrix
            self._doc_ids = doc_ids
            self._doc_types = doc_types
            self._languages = languages
            self._content_hashes = content_hashes
        
        # Update data version for freshness
        with self.lock:
            self._data_version = self.conn.execute("PRAGMA data_version").fetchone()[0]
        self._last_reload_time = time.time()
        logger.info("Loaded %d vectors for model %s (dim=%d)", n, self.model, self.dim)
    
    def _maybe_reload(self):
        """Check for external writes and reload if needed."""
        with self.lock:
            current_version = self.conn.execute("PRAGMA data_version").fetchone()[0]
            if current_version != self._data_version:
                logger.info("External write detected, reloading vector store")
                self._load_matrix()
    
    def _serialize(self, vec: np.ndarray) -> bytes:
        return vec.astype(np.float32).tobytes()
    
    def add(self, doc_id: str, vector: np.ndarray, content_hash: str, doc_type: str = "", language: str = ""):
        """Add or update a vector."""
        if vector.shape != (self.dim,):
            raise ValueError(f"Vector dim {vector.shape} != expected {self.dim}")
        
        # Ensure unit norm
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm
        
        with self.lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO doc_vectors 
                (doc_id, model, dim, content_hash, vector, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (doc_id, self.model, self.dim, content_hash, self._serialize(vector), time.time())
            )
            self.conn.commit()
        
        # Update in-memory matrix
        with self._matrix_lock:
            # Check if exists
            existing_idx = np.where(self._doc_ids == doc_id)[0]
            if len(existing_idx) > 0:
                idx = existing_idx[0]
                self._matrix[idx] = vector
                self._doc_types[idx] = doc_type
                self._languages[idx] = language
            else:
                # Append
                self._matrix = np.vstack([self._matrix, vector.reshape(1, -1)])
                self._doc_ids = np.append(self._doc_ids, doc_id)
                self._doc_types = np.append(self._doc_types, doc_type)
                self._languages = np.append(self._languages, language)
            
            self._content_hashes[doc_id] = content_hash

    # Backward compatibility
    upsert = add
    
    def remove(self, doc_id: str):
        """Remove a vector (swap-remove for O(1) delete)."""
        with self.lock:
            self.conn.execute(
                "DELETE FROM doc_vectors WHERE doc_id = ? AND model = ?",
                (doc_id, self.model)
            )
            self.conn.commit()
        
        with self._matrix_lock:
            idx_arr = np.where(self._doc_ids == doc_id)[0]
            if len(idx_arr) > 0:
                idx = idx_arr[0]
                # Swap with last element
                last_idx = len(self._doc_ids) - 1
                if idx != last_idx:
                    self._matrix[idx] = self._matrix[last_idx]
                    self._doc_ids[idx] = self._doc_ids[last_idx]
                    self._doc_types[idx] = self._doc_types[last_idx]
                    self._languages[idx] = self._languages[last_idx]
                # Remove last element
                self._matrix = self._matrix[:last_idx]
                self._doc_ids = self._doc_ids[:last_idx]
                self._doc_types = self._doc_types[:last_idx]
                self._languages = self._languages[:last_idx]
                self._content_hashes.pop(doc_id, None)

    # Backward compatibility
    delete = remove
    
    def get_content_hash(self, doc_id: str) -> Optional[str]:
        """Get stored content hash for a doc."""
        with self._matrix_lock:
            return self._content_hashes.get(doc_id)
    
    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        min_score: float = -1.0,
        allowed: Optional[Callable[[str], bool]] = None,
    ) -> list[VectorSearchResult]:
        """Search vectors using matrix multiplication + argpartition.
        
        Args:
            query_vector: Unit-norm query vector
            top_k: Number of results to return
            min_score: Minimum cosine similarity threshold
            allowed: Optional callable(doc_id) -> bool for filter pushdown
        """
        self._maybe_reload()
        
        with self._matrix_lock:
            if len(self._matrix) == 0:
                return []
            matrix = self._matrix
            doc_ids = self._doc_ids
            doc_types = self._doc_types
            languages = self._languages
        
        # Fast path: compute all cosine similarities
        # matrix is (n, dim), query is (dim,) -> scores is (n,)
        scores = matrix @ query_vector  # Already L2-normalized
        
        # Apply filter pushdown if allowed
        if allowed is not None:
            mask = np.array([allowed(doc_ids[i]) for i in range(len(doc_ids))], dtype=bool)
            if not mask.any():
                return []
            # Only consider allowed indices
            valid_indices = np.where(mask)[0]
            scores = scores[valid_indices]
            doc_ids = doc_ids[valid_indices]
            doc_types = doc_types[valid_indices]
            languages = languages[valid_indices]
        
        # Filter by min_score
        if min_score > -1.0:
            mask = scores >= min_score
            if not mask.any():
                return []
            scores = scores[mask]
            doc_ids = doc_ids[mask]
        
        # Get top-k using argpartition (O(n) instead of O(n log n))
        k = min(top_k, len(scores))
        if k <= 0:
            return []
        
        # argpartition gives indices of k largest elements (unsorted)
        top_indices = np.argpartition(scores, -k)[-k:]
        # Sort top-k by score descending
        top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]
        
        results = []
        for idx in top_indices:
            results.append(VectorSearchResult(
                doc_id=doc_ids[idx],
                score=float(scores[idx])
            ))
        return results
    
    def count(self) -> int:
        with self._matrix_lock:
            return len(self._doc_ids)
    
    def get_stats(self) -> dict:
        return {
            "count": self.count(),
            "model": self.model,
            "dim": self.dim,
        }
    
    def close(self):
        with self.lock:
            self.conn.close()


class VectorStoreManager:
    """Manager for VectorStore with embedding generation."""
    
    def __init__(
        self,
        db_path: str = "nexus_search.db",
        embedder: Optional[Embedder] = None,
    ):
        self.store = VectorStore(db_path, embedder)
        self.embedder = self.store.embedder
    
    def upsert(self, doc_id: str, text: str, doc_type: str = "", language: str = "") -> str:
        """Generate embedding for text and store it.

        Returns "created" (new vector), "updated" (content changed, vector
        re-embedded), or "unchanged" (content hash matched — no work done)."""
        content_hash = self._content_hash(text)
        existing_hash = self.store.get_content_hash(doc_id)
        if existing_hash == content_hash:
            return "unchanged"

        # Generate embedding
        vector = self.embedder.embed_query(text)
        self.store.add(doc_id, vector, content_hash, doc_type, language)
        return "updated" if existing_hash is not None else "created"
    
    def upsert_batch(self, items: list[tuple[str, str, str, str]]):
        """Batch upsert: items = [(doc_id, text, doc_type, language), ...]"""
        # Filter out unchanged
        to_embed = []
        for doc_id, text, doc_type, language in items:
            content_hash = self._content_hash(text)
            if self.store.get_content_hash(doc_id) != content_hash:
                to_embed.append((doc_id, text, content_hash, doc_type, language))
        
        if not to_embed:
            return
        
        texts = [t for _, t, _, _, _ in to_embed]
        vectors = self.embedder.embed_documents(texts)
        
        with self.store.lock:
            now = time.time()
            for (doc_id, _, content_hash, doc_type, language), vector in zip(to_embed, vectors):
                self.store.conn.execute(
                    """
                    INSERT OR REPLACE INTO doc_vectors 
                    (doc_id, model, dim, content_hash, vector, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (doc_id, self.store.model, self.store.dim, content_hash, self.store._serialize(vector), now)
                )
            self.store.conn.commit()
        
        # Reload matrix
        self.store._load_matrix()
    
    def delete(self, doc_id: str):
        self.store.remove(doc_id)
    
    def search(self, query_text: str, top_k: int = 10, min_score: float = -1.0, allowed: Optional[Callable[[str], bool]] = None):
        query_vector = self.embedder.embed_query(query_text)
        return self.store.search(query_vector, top_k, min_score, allowed)
    
    def _content_hash(self, text: str) -> str:
        import hashlib
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    
    def get_stats(self) -> dict:
        return self.store.get_stats()
    
    def close(self):
        self.store.close()


def create_vector_store(db_path: str = "nexus_search.db", embedder: Optional[Embedder] = None) -> VectorStoreManager:
    return VectorStoreManager(db_path, embedder)