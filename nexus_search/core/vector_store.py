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
        
        # In-memory matrix: (n_vectors, dim) float32, L2-normalized.
        # Amortized-capacity buffers (P2-9): `self._matrix`/`self._doc_ids`
        # are always VIEWS of exactly the valid rows of over-allocated
        # buffers, so append is O(1) amortized instead of a full-matrix
        # np.vstack (quadratic bulk ingest). All access is under
        # _matrix_lock; views never expose rows past the valid count.
        self._matrix_buf: np.ndarray = np.empty((16, self.dim), dtype=np.float32)
        self._doc_ids_buf: np.ndarray = np.empty(16, dtype=object)
        self._doc_types_buf: np.ndarray = np.empty(16, dtype=object)
        self._languages_buf: np.ndarray = np.empty(16, dtype=object)
        self._matrix: np.ndarray = self._matrix_buf[:0]
        self._doc_ids: np.ndarray = self._doc_ids_buf[:0]
        self._doc_types: np.ndarray = self._doc_types_buf[:0]
        self._languages: np.ndarray = self._languages_buf[:0]
        self._content_hashes: dict[str, str] = {}  # doc_id -> content_hash
        # doc_id -> valid-row index (WP12-B5): add/remove are O(1) lookups
        # instead of a full np.where scan over the object-dtype doc_ids
        # array per operation (O(n) per add — quadratic bulk ingest).
        self._id_to_idx: dict[str, int] = {}
        self._matrix_lock = threading.RLock()
        # Bumped on every in-memory mutation (add/remove/batch merge) so a
        # concurrent _load_matrix can tell whether the live buffers moved
        # under it while it was building replacements (WP12-B1).
        self._mem_version = 0
        
        # Freshness tracking
        self._data_version = 0
        self._last_reload_time = 0.0
        
        self._load_matrix()
    
    def _init_schema(self):
        with self.lock:
            # Versioned like the other stores (v1 = this schema as-is;
            # idempotent DDL so baselining a pre-migrations DB is a no-op).
            from .migrations import apply_migrations
            self.schema_version = apply_migrations(
                self.conn, "doc_vectors", [(1, SCHEMA)]
            )
    
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
        """Load all vectors for this model into memory.

        WP12-B1 (audit R1/R5b): the replacement buffers AND the
        content-hash dict are built OUTSIDE the locks (a concurrent
        search() keeps matmul'ing the old buffers instead of stalling
        behind a full fill), then swapped in under ONE short critical
        section. The hash map is REPLACED, not merged: after an external
        delete + reload, get_content_hash must return None — the P2-9
        version kept the deleted doc's stale hash forever, so a
        VectorStoreManager.upsert of identical text was skipped as
        'unchanged' and the doc never regained a vector.

        The swap is guarded by _mem_version: an in-process add/remove that
        lands while we build would otherwise be clobbered by the swap (its
        own commits don't bump PRAGMA data_version, so nothing would ever
        trigger another reload — the silent-loss class P2-9 fixed for
        add()). On a version mismatch we simply retry; after bounded
        attempts we fall back to building under the locks (slow but
        consistent).
        """
        for attempt in range(3):
            with self.lock:
                # Read data_version BEFORE the fetch: a write that lands
                # during the build bumps it again, so the next
                # _maybe_reload re-syncs instead of missing it.
                version = self.conn.execute("PRAGMA data_version").fetchone()[0]
                mem_version = self._mem_version
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

            import json
            n = len(rows)
            cap = max(n, 16)
            matrix_buf = np.empty((cap, self.dim), dtype=np.float32)
            doc_ids_buf = np.empty(cap, dtype=object)
            doc_types_buf = np.empty(cap, dtype=object)
            languages_buf = np.empty(cap, dtype=object)
            content_hashes: dict[str, str] = {}
            id_to_idx: dict[str, int] = {}
            for i, (doc_id, vec_blob, content_hash, doc_type, metadata) in enumerate(rows):
                matrix_buf[i] = np.frombuffer(vec_blob, dtype=np.float32)
                doc_ids_buf[i] = doc_id
                doc_types_buf[i] = doc_type or ""
                meta = json.loads(metadata) if metadata else {}
                languages_buf[i] = meta.get("language", "") or ""
                content_hashes[doc_id] = content_hash
                id_to_idx[doc_id] = i

            with self.lock:
                with self._matrix_lock:
                    if self._mem_version != mem_version:
                        continue  # in-memory mutation raced us: rebuild
                    self._matrix_buf = matrix_buf
                    self._doc_ids_buf = doc_ids_buf
                    self._doc_types_buf = doc_types_buf
                    self._languages_buf = languages_buf
                    self._content_hashes = content_hashes
                    self._id_to_idx = id_to_idx
                    self._set_view(n)
                    self._data_version = version
            self._last_reload_time = time.time()
            logger.info("Loaded %d vectors for model %s (dim=%d)", n, self.model, self.dim)
            return
        # Contention path (rare): last resort, build under the locks so the
        # result can never clobber a concurrent merge.
        with self.lock:
            with self._matrix_lock:
                import json as _json
                n = len(rows)
                self._reset_buffers(n)
                for i, (doc_id, vec_blob, content_hash, doc_type, metadata) in enumerate(rows):
                    self._matrix_buf[i] = np.frombuffer(vec_blob, dtype=np.float32)
                    self._doc_ids_buf[i] = doc_id
                    self._doc_types_buf[i] = doc_type or ""
                    meta = _json.loads(metadata) if metadata else {}
                    self._languages_buf[i] = meta.get("language", "") or ""
                self._content_hashes = {}
                self._id_to_idx = {}
                for i, (doc_id, _v, content_hash, _t, _m) in enumerate(rows):
                    self._content_hashes[doc_id] = content_hash
                    self._id_to_idx[doc_id] = i
                self._set_view(n)
                self._data_version = version
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
    
    def _reset_buffers(self, n: int) -> None:
        """(Re)allocate the capacity buffers for `n` valid rows. Caller
        holds _matrix_lock."""
        cap = max(n, 16)
        # keep existing capacity when it already fits (load-after-load)
        if self._matrix_buf.shape[0] >= n and self._matrix is not None \
                and len(self._matrix) == 0:
            cap = self._matrix_buf.shape[0]
        self._matrix_buf = np.empty((cap, self.dim), dtype=np.float32)
        self._doc_ids_buf = np.empty(cap, dtype=object)
        self._doc_types_buf = np.empty(cap, dtype=object)
        self._languages_buf = np.empty(cap, dtype=object)
        self._set_view(0)

    def _set_view(self, n: int) -> None:
        """Publish exactly the first `n` buffer rows. Caller holds
        _matrix_lock."""
        self._matrix = self._matrix_buf[:n]
        self._doc_ids = self._doc_ids_buf[:n]
        self._doc_types = self._doc_types_buf[:n]
        self._languages = self._languages_buf[:n]

    def _grow_buffers(self, need: int) -> None:
        """Ensure capacity for `need` valid rows, doubling amortized.
        Caller holds _matrix_lock."""
        cap = self._matrix_buf.shape[0]
        if need <= cap:
            return
        new_cap = max(need, cap * 2, 16)
        matrix = np.empty((new_cap, self.dim), dtype=np.float32)
        matrix[:len(self._matrix)] = self._matrix
        doc_ids = np.empty(new_cap, dtype=object)
        doc_ids[:len(self._doc_ids)] = self._doc_ids
        doc_types = np.empty(new_cap, dtype=object)
        doc_types[:len(self._doc_types)] = self._doc_types
        languages = np.empty(new_cap, dtype=object)
        languages[:len(self._languages)] = self._languages
        self._matrix_buf = matrix
        self._doc_ids_buf = doc_ids
        self._doc_types_buf = doc_types
        self._languages_buf = languages

    def _merge_in_memory(self, doc_id: str, vector: np.ndarray,
                         content_hash: str, doc_type: str, language: str) -> None:
        """In-memory upsert of one already-committed row (caller holds
        self.lock + _matrix_lock). Update-in-place or amortized append —
        P2-9: the per-add np.vstack was quadratic bulk ingest. WP12-B5:
        the doc_id lookup is an O(1) dict hit (the np.where scan over the
        object-dtype doc_ids array was O(n) per add)."""
        idx = self._id_to_idx.get(doc_id)
        if idx is not None:
            self._matrix[idx] = vector
            self._doc_types[idx] = doc_type
            self._languages[idx] = language
        else:
            n = len(self._doc_ids)
            self._grow_buffers(n + 1)
            self._matrix_buf[n] = vector
            self._doc_ids_buf[n] = doc_id
            self._doc_types_buf[n] = doc_type
            self._languages_buf[n] = language
            self._set_view(n + 1)
            self._id_to_idx[doc_id] = n
        self._content_hashes[doc_id] = content_hash
        self._mem_version += 1  # WP12-B1: reloads must not clobber this

    def add(self, doc_id: str, vector: np.ndarray, content_hash: str, doc_type: str = "", language: str = ""):
        """Add or update a vector."""
        # Refresh our view of other processes' writes BEFORE merging ours into
        # the in-memory matrix, same as search(): without this, a stale local
        # view would drop vectors other instances committed. This does NOT
        # make concurrent writes at scale safe (that's the shared-store
        # design); it closes the silent-loss window between commits.
        self._maybe_reload()
        if vector.shape != (self.dim,):
            raise ValueError(f"Vector dim {vector.shape} != expected {self.dim}")

        # Ensure unit norm
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm

        # SQL commit and in-memory merge happen inside ONE critical section
        # (self.lock -> self._matrix_lock, the same order _load_matrix uses).
        # Splitting them let other threads observe/commit between the two,
        # and a crash left a committed row invisible to this process's matrix
        # (our own commits don't move PRAGMA data_version for us).
        with self.lock:
            with self._matrix_lock:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO doc_vectors
                    (doc_id, model, dim, content_hash, vector, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (doc_id, self.model, self.dim, content_hash, self._serialize(vector), time.time())
                )
                self.conn.commit()
                self._merge_in_memory(doc_id, vector, content_hash, doc_type, language)

    # Backward compatibility
    upsert = add

    def remove(self, doc_id: str):
        """Remove a vector (swap-remove for O(1) delete). SQL + memory under
        ONE critical section, same as add()."""
        with self.lock:
            with self._matrix_lock:
                self.conn.execute(
                    "DELETE FROM doc_vectors WHERE doc_id = ? AND model = ?",
                    (doc_id, self.model)
                )
                self.conn.commit()

                idx = self._id_to_idx.pop(doc_id, None)
                if idx is not None:
                    # Swap with last element (WP12-B5: O(1) index lookup;
                    # the moved row's map entry moves with it)
                    last_idx = len(self._doc_ids) - 1
                    if idx != last_idx:
                        self._matrix[idx] = self._matrix[last_idx]
                        moved_id = self._doc_ids[last_idx]
                        self._doc_ids[idx] = moved_id
                        self._doc_types[idx] = self._doc_types[last_idx]
                        self._languages[idx] = self._languages[last_idx]
                        self._id_to_idx[moved_id] = idx
                    # Publish one fewer row: the view shrinks (no copy —
                    # the capacity buffer stays for reuse; P2-9). search()
                    # snapshots scores+doc_ids under the same lock, so no
                    # reader can pair rows across the shrink.
                    self._set_view(last_idx)
                    self._content_hashes.pop(doc_id, None)
                    self._mem_version += 1  # WP12-B1: reloads must not clobber this

    # Backward compatibility
    delete = remove
    
    def get_content_hash(self, doc_id: str) -> Optional[str]:
        """Get stored content hash for a doc.

        Refreshes first (_maybe_reload): the hash gates
        VectorStoreManager.upsert's unchanged-skip, so without this an
        external delete followed by a same-text upsert would answer from
        a stale in-memory dict and skip re-embedding forever (WP12-B1)."""
        self._maybe_reload()
        with self._matrix_lock:
            return self._content_hashes.get(doc_id)
    
    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
        min_score: float = -1.0,
        allowed: Optional[Callable[[str], bool]] = None,
        allowed_ids: Optional[set] = None,
    ) -> list[VectorSearchResult]:
        """Search vectors using matrix multiplication + argpartition.

        Args:
            query_vector: Unit-norm query vector
            top_k: Number of results to return
            min_score: Minimum cosine similarity threshold
            allowed: Optional callable(doc_id) -> bool for filter pushdown
            allowed_ids: Optional precomputed set of permitted doc_ids
                (WP12-B3): masked with ONE vectorized np.isin instead of
                a per-vector allowed() call that did a full-content
                get_document per vector (O(corpus) SQL per query).
                Takes precedence over `allowed` when both are given.
        """
        self._maybe_reload()

        # P2-8: snapshot under the lock WITHOUT copying the matrix — the
        # matmul runs inside the critical section and produces a private
        # `scores` array; only doc_ids (n*8 bytes, not n*dim*4) is copied.
        # scores and doc_ids are captured under the SAME lock hold, so they
        # are always a consistent pair (add()/remove() mutate only under
        # the same lock). The old code copied the whole matrix + two unused
        # columns on EVERY query (O(n*dim) per search).
        with self._matrix_lock:
            if len(self._matrix) == 0:
                return []
            scores = self._matrix @ query_vector
            doc_ids = self._doc_ids.copy()

        # Apply filter pushdown if allowed
        if allowed_ids is not None:
            if not allowed_ids:
                return []
            # set-lookup mask: O(n) membership checks. np.isin on the
            # object-dtype doc_ids array degrades to O(n*m) Python-level
            # comparisons (measured ~290 ms at 3K docs vs sub-ms here).
            mask = np.fromiter((d in allowed_ids for d in doc_ids),
                               dtype=bool, count=len(doc_ids))
            if not mask.any():
                return []
            valid_indices = np.where(mask)[0]
            scores = scores[valid_indices]
            doc_ids = doc_ids[valid_indices]
        elif allowed is not None:
            mask = np.array([allowed(doc_ids[i]) for i in range(len(doc_ids))], dtype=bool)
            if not mask.any():
                return []
            # Only consider allowed indices
            valid_indices = np.where(mask)[0]
            scores = scores[valid_indices]
            doc_ids = doc_ids[valid_indices]

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
        # embedding happens OUTSIDE the store locks — it's the slow part and
        # touches no shared state
        vectors = self.embedder.embed_documents(texts)

        # Pick up other processes' committed rows BEFORE our own merge (same
        # eventual-consistency contract as add(); P2-9 removed the per-batch
        # full-reload that made bulk ingest quadratic).
        self.store._maybe_reload()

        # Commit + in-memory merge inside ONE critical section (same pattern
        # as add()/remove()): without it, a concurrent search() could observe
        # a stale matrix after the batch's rows were already durable.
        with self.store.lock:
            with self.store._matrix_lock:
                now = time.time()
                for (doc_id, _, content_hash, doc_type, language), vector in zip(to_embed, vectors):
                    self.store.conn.execute(
                        """
                        INSERT OR REPLACE INTO doc_vectors
                        (doc_id, model, dim, content_hash, vector, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (doc_id, self.store.model, self.store.dim, content_hash,
                         self.store._serialize(vector), now)
                    )
                self.store.conn.commit()
                for (doc_id, _, content_hash, doc_type, language), vector in zip(to_embed, vectors):
                    self.store._merge_in_memory(doc_id, vector, content_hash,
                                                doc_type, language)
    
    def delete(self, doc_id: str):
        self.store.remove(doc_id)
    
    def search(self, query_text: str, top_k: int = 10, min_score: float = -1.0, allowed: Optional[Callable[[str], bool]] = None, allowed_ids: Optional[set] = None):
        query_vector = self.embedder.embed_query(query_text)
        return self.store.search(query_vector, top_k, min_score, allowed, allowed_ids)
    
    def _content_hash(self, text: str) -> str:
        import hashlib
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
    
    def get_stats(self) -> dict:
        return self.store.get_stats()
    
    def close(self):
        self.store.close()


def create_vector_store(db_path: str = "nexus_search.db", embedder: Optional[Embedder] = None) -> VectorStoreManager:
    return VectorStoreManager(db_path, embedder)