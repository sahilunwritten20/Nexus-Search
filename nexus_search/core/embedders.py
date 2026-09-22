"""Embedder layer for Nexus Search Phase 4 (T1).

Provides a clean interface for different embedding backends:
- HashEmbedder: deterministic, offline, lexical (NOT semantic)
- SentenceTransformerEmbedder: real semantic embeddings
- SynonymEmbedder: test-only, maps synonyms to similar vectors

All embedders output unit-norm float32 vectors.
"""
import hashlib
import os
import threading
import logging
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import List, Optional
import numpy as np

logger = logging.getLogger("nexus_search.embedders")


class EmbedderUnavailable(Exception):
    """Raised when an embedder cannot be loaded or used."""
    pass


class Embedder(ABC):
    """Abstract base class for all embedders."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this embedder (e.g., 'st:all-MiniLM-L6-v2', 'hash:384')."""
        pass
    
    @property
    @abstractmethod
    def dim(self) -> int:
        """Embedding dimension."""
        pass
    
    @abstractmethod
    def embed_documents(self, texts: List[str], batch_size: int = 32) -> List[np.ndarray]:
        """Embed multiple documents. Returns list of unit-norm float32 vectors."""
        pass
    
    @abstractmethod
    def embed_query(self, text: str) -> np.ndarray:
        """Embed a single query. Returns unit-norm float32 vector."""
        pass
    
    # Backward compatibility with old EmbeddingManager interface
    def get_or_compute(self, doc_id: str, text: str) -> np.ndarray:
        """Get or compute embedding for a document (backward compatibility)."""
        return self.embed_query(text)
    
    def invalidate(self, doc_id: str):
        """Invalidate cached embedding for a document (backward compatibility)."""
        pass  # No-op for stateless embedders
    
    def invalidate_batch(self, doc_ids: list[str]):
        """Invalidate cached embeddings for multiple documents (backward compatibility)."""
        pass


def _stable_hash(feat: str, dim: int) -> tuple[int, float]:
    """Deterministic hash using blake2b - same across process restarts.
    
    Returns (index, sign) where index is in [0, dim) and sign is +1.0 or -1.0.
    """
    # Use blake2b with fixed seed for determinism
    h = hashlib.blake2b(feat.encode('utf-8'), digest_size=8, person=b'nxs')
    digest = int.from_bytes(h.digest(), 'little')
    idx = digest % dim
    sign = 1.0 if (digest & 1) == 0 else -1.0
    return idx, sign


class HashEmbedder(Embedder):
    """Deterministic hash-based embedder using word+trigram hashing trick.
    
    This is a LEXICAL embedder, NOT semantic. It maps similar word overlap
    to similar vectors, but does NOT understand meaning/synonyms.
    
    Used for: testing, offline environments, when NEXUS_EMBEDDER=hash
    """
    
    def __init__(self, dim: int = 384, seed: int = 42):
        self._dim = dim
        self._seed = seed
        self._rng = np.random.RandomState(seed)
        # Projection matrix: (vocab_hash_space, dim) -> but we use signed hashing trick
        # Instead of storing matrix, we hash on the fly
        self._query_cache = {}
        self._cache_lock = threading.Lock()
    
    @property
    def name(self) -> str:
        return f"hash:{self._dim}"
    
    @property
    def dim(self) -> int:
        return self._dim
    
    def _text_to_features(self, text: str) -> np.ndarray:
        """Convert text to feature vector using signed hashing trick."""
        # Word-level features
        words = text.lower().split()
        # Character trigram features
        trigrams = []
        for word in words:
            for i in range(len(word) - 2):
                trigrams.append(word[i:i+3])
        
        all_features = words + trigrams
        
        # Hashing trick with signed features - deterministic using blake2b
        vec = np.zeros(self._dim, dtype=np.float32)
        for feat in all_features:
            idx, sign = _stable_hash(feat, self._dim)
            vec[idx] += sign
        
        # L2 normalize
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec.astype(np.float32)
    
    def embed_documents(self, texts: List[str], batch_size: int = 32) -> List[np.ndarray]:
        return [self._text_to_features(t) for t in texts]
    
    def embed_query(self, text: str) -> np.ndarray:
        # LRU cache for queries
        with self._cache_lock:
            if text in self._query_cache:
                return self._query_cache[text]
            emb = self._text_to_features(text)
            if len(self._query_cache) >= 256:
                # Simple eviction - remove first item
                first_key = next(iter(self._query_cache))
                del self._query_cache[first_key]
            self._query_cache[text] = emb
            return emb


class SentenceTransformerEmbedder(Embedder):
    """Wrapper around sentence-transformers for semantic embeddings."""
    
    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._model = None
        self._dim = None
        self._lock = threading.Lock()
        self._query_cache = {}
        self._cache_lock = threading.Lock()
        # Probe dimension on first use
        self._load_model()
    
    def _load_model(self):
        """Load model and determine dimension."""
        with self._lock:
            if self._model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(self._model_name)
                    # Get dimension by encoding a test string
                    test_emb = self._model.encode(["test"], convert_to_numpy=True, show_progress_bar=False)
                    self._dim = test_emb.shape[1]
                    logger.info("Loaded embedding model: %s (dim=%d)", self._model_name, self._dim)
                except Exception as exc:
                    logger.error("Failed to load sentence-transformers model '%s': %s", self._model_name, exc)
                    raise EmbedderUnavailable(f"Cannot load model '{self._model_name}': {exc}")
    
    @property
    def name(self) -> str:
        return f"st:{self._model_name}"
    
    @property
    def dim(self) -> int:
        if self._dim is None:
            self._probe_dim()
        return self._dim
    
    def embed_documents(self, texts: List[str], batch_size: int = 32) -> List[np.ndarray]:
        if self._model is None:
            self._probe_dim()
        
        # sentence-transformers handles batching internally
        embeddings = self._model.encode(
            texts, 
            batch_size=batch_size,
            convert_to_numpy=True, 
            show_progress_bar=False,
            normalize_embeddings=True  # Ensure unit norm
        )
        return [e.astype(np.float32) for e in embeddings]
    
    def embed_query(self, text: str) -> np.ndarray:
        # LRU cache for queries
        with self._cache_lock:
            if text in self._query_cache:
                return self._query_cache[text]
            
            if self._model is None:
                self._probe_dim()
            
            emb = self._model.encode([text], convert_to_numpy=True, show_progress_bar=False, normalize_embeddings=True)[0]
            emb = emb.astype(np.float32)
            
            if len(self._query_cache) >= 256:
                first_key = next(iter(self._query_cache))
                del self._query_cache[first_key]
            self._query_cache[text] = emb
            return emb


# Global embedder instance (lazy initialization)
_embedder_instance: Optional[Embedder] = None
_embedder_lock = threading.Lock()


def get_embedder() -> Embedder:
    """Get the configured embedder instance (singleton).
    
    Configuration via NEXUS_EMBEDDER env var:
    - "hash[:dim]" - HashEmbedder (default dim=384)
    - "st:model_name" - SentenceTransformerEmbedder
    Default: "st:sentence-transformers/all-MiniLM-L6-v2"
    """
    global _embedder_instance
    if _embedder_instance is not None:
        return _embedder_instance
    
    with _embedder_lock:
        if _embedder_instance is not None:
            return _embedder_instance
        
        config = os.environ.get("NEXUS_EMBEDDER", "st:sentence-transformers/all-MiniLM-L6-v2")
        
        if config.startswith("hash"):
            if ":" in config:
                try:
                    dim = int(config.split(":")[1])
                except ValueError:
                    raise ValueError(f"Invalid hash dim in NEXUS_EMBEDDER: '{config}'")
            else:
                dim = 384
            _embedder_instance = HashEmbedder(dim=dim)
            logger.info("Using HashEmbedder (dim=%d) - LEXICAL ONLY, not semantic", dim)
        
        elif config.startswith("st:"):
            model_name = config[3:]
            _embedder_instance = SentenceTransformerEmbedder(model_name)
            logger.info("Using SentenceTransformerEmbedder: %s", model_name)
        
        else:
            raise ValueError(f"Invalid NEXUS_EMBEDDER: '{config}'. Use 'hash[:dim]' or 'st:model_name'")
        
        return _embedder_instance


def reset_embedder():
    """Reset the global embedder (for testing)."""
    global _embedder_instance
    with _embedder_lock:
        _embedder_instance = None


# For backward compatibility - will be removed
def create_embedding_manager(db_path: str = "nexus_search.db", model: str = "sentence-transformers/all-MiniLM-L6-v2"):
    """Deprecated: Use EmbeddingSync with get_embedder() instead."""
    import warnings
    warnings.warn("create_embedding_manager is deprecated", DeprecationWarning, stacklevel=2)
    from nexus_search.core.embeddings import EmbeddingManager
    return EmbeddingManager(db_path, model)


DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384