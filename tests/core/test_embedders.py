"""Tests for the new embedder layer (T1)."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import numpy as np

# Use hash embedder for offline tests
os.environ["NEXUS_EMBEDDER"] = "hash:384"


class SynonymEmbedder:
    """Test-only embedder that maps synonyms to similar vectors."""
    
    def __init__(self, dim: int = 64):
        self._dim = dim
        # Define synonym groups - each group shares the same vector
        self._synonym_groups = [
            {"car", "automobile", "vehicle"},
            {"big", "large", "huge"},
            {"fast", "quick", "speedy"},
        ]
        # Build word -> canonical concept mapping
        self._word_to_concept = {}
        for group in self._synonym_groups:
            canonical = min(group)  # Use lexicographically first as canonical
            for word in group:
                self._word_to_concept[word] = canonical
        
        self._base_vectors = {}
        self._rng = np.random.RandomState(42)
    
    @property
    def name(self) -> str:
        return f"synonym:{self._dim}"
    
    @property
    def dim(self) -> int:
        return self._dim
    
    def _get_base_vector(self, concept: str) -> np.ndarray:
        if concept not in self._base_vectors:
            vec = self._rng.randn(self._dim).astype(np.float32)
            norm = np.linalg.norm(vec)
            if norm > 0:
                vec = vec / norm
            self._base_vectors[concept] = vec
        return self._base_vectors[concept]
    
    def _resolve_concept(self, word: str) -> str:
        return self._word_to_concept.get(word.lower(), word.lower())
    
    def embed_documents(self, texts: list[str], batch_size: int = 32) -> list[np.ndarray]:
        return [self.embed_query(t) for t in texts]
    
    def embed_query(self, text: str) -> np.ndarray:
        words = text.lower().split()
        if not words:
            vec = np.zeros(self._dim, dtype=np.float32)
            vec[0] = 1.0
            return vec
        # Find first word that has a concept mapping
        for w in words:
            if w in self._word_to_concept:
                concept = self._word_to_concept[w]
                return self._get_base_vector(concept).copy()
        # No known concept, use first word as-is
        concept = self._resolve_concept(words[0])
        return self._get_base_vector(concept).copy()


class TestEmbedderInterface(unittest.TestCase):
    """Test the embedder base interface and implementations."""

    def test_embedder_base_interface(self):
        from nexus_search.core.embedders import Embedder
        
        # Check that Embedder is an abstract base class with required methods
        self.assertTrue(hasattr(Embedder, 'name'))
        self.assertTrue(hasattr(Embedder, 'dim'))
        self.assertTrue(hasattr(Embedder, 'embed_documents'))
        self.assertTrue(hasattr(Embedder, 'embed_query'))

    def test_hash_embedder(self):
        from nexus_search.core.embedders import HashEmbedder
        
        embedder = HashEmbedder(dim=384)
        self.assertEqual(embedder.name, "hash:384")
        self.assertEqual(embedder.dim, 384)
        
        # Test embedding generation
        texts = ["hello world", "goodbye world"]
        embeddings = embedder.embed_documents(texts)
        
        self.assertEqual(len(embeddings), 2)
        self.assertEqual(embeddings[0].shape, (384,))
        self.assertEqual(embeddings[1].shape, (384,))
        self.assertEqual(embeddings[0].dtype, np.float32)
        
        # Same text should give same embedding
        emb1 = embedder.embed_query("hello world")
        emb2 = embedder.embed_query("hello world")
        np.testing.assert_array_equal(emb1, emb2)
        
        # Different text should give different embedding
        emb3 = embedder.embed_query("different text")
        self.assertFalse(np.array_equal(emb1, emb3))
        
        # Embeddings should be unit norm (or close to it)
        norm = np.linalg.norm(emb1)
        self.assertAlmostEqual(norm, 1.0, places=5)

    def test_hash_embedder_different_dims(self):
        from nexus_search.core.embedders import HashEmbedder
        
        for dim in [128, 256, 384, 512, 768]:
            embedder = HashEmbedder(dim=dim)
            self.assertEqual(embedder.dim, dim)
            emb = embedder.embed_query("test")
            self.assertEqual(emb.shape, (dim,))

    def test_synonym_embedder(self):
        """Test-only embedder that maps synonyms to similar vectors."""
        embedder = SynonymEmbedder(dim=64)
        self.assertEqual(embedder.dim, 64)
        
        # "car" and "automobile" should have similar embeddings
        emb_car = embedder.embed_query("car")
        emb_auto = embedder.embed_query("automobile")
        similarity = np.dot(emb_car, emb_auto)
        self.assertGreater(similarity, 0.8)  # High similarity for synonyms
        
        # "car" and "banana" should be dissimilar
        emb_banana = embedder.embed_query("banana")
        similarity = np.dot(emb_car, emb_banana)
        self.assertLess(similarity, 0.3)

    def test_embedder_lru_cache(self):
        from nexus_search.core.embedders import HashEmbedder
        
        embedder = HashEmbedder(dim=32)
        
        # Fill cache beyond capacity (default 256)
        for i in range(300):
            embedder.embed_query(f"query {i}")
        
        # First queries should be evicted but we can't easily test that
        # Just verify it doesn't crash
        emb = embedder.embed_query("query 0")
        self.assertEqual(emb.shape, (32,))


class TestSentenceTransformerEmbedder(unittest.TestCase):
    """Tests for SentenceTransformerEmbedder - requires the model to be installed."""
    
    @unittest.skipUnless(os.environ.get("NEXUS_RUN_MODEL_TESTS") == "1", "Requires sentence-transformers")
    def test_st_embedder_basic(self):
        from nexus_search.core.embedders import SentenceTransformerEmbedder
        
        embedder = SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2")
        self.assertEqual(embedder.dim, 384)
        self.assertTrue(embedder.name.startswith("st:"))
        
        texts = ["hello world", "machine learning"]
        embeddings = embedder.embed_documents(texts)
        
        self.assertEqual(len(embeddings), 2)
        self.assertEqual(embeddings[0].shape, (384,))
        
        # Check unit norm
        for emb in embeddings:
            norm = np.linalg.norm(emb)
            self.assertAlmostEqual(norm, 1.0, places=5)
        
        # Query embedding
        q_emb = embedder.embed_query("hello world")
        self.assertEqual(q_emb.shape, (384,))
        norm = np.linalg.norm(q_emb)
        self.assertAlmostEqual(norm, 1.0, places=5)

    @unittest.skipUnless(os.environ.get("NEXUS_RUN_MODEL_TESTS") == "1", "Requires sentence-transformers")
    def test_st_embedder_batch(self):
        from nexus_search.core.embedders import SentenceTransformerEmbedder
        
        embedder = SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2")
        
        # Test batch processing
        texts = [f"document {i}" for i in range(100)]
        embeddings = embedder.embed_documents(texts, batch_size=32)
        
        self.assertEqual(len(embeddings), 100)
        for emb in embeddings:
            self.assertEqual(emb.shape, (384,))
            norm = np.linalg.norm(emb)
            self.assertAlmostEqual(norm, 1.0, places=5)


class TestEmbedderConfig(unittest.TestCase):
    """Test embedder configuration via environment variable."""
    
    def setUp(self):
        # Reset global embedder before each test
        from nexus_search.core.embedders import reset_embedder
        reset_embedder()
    
    def tearDown(self):
        from nexus_search.core.embedders import reset_embedder
        reset_embedder()
    
    def test_hash_embedder_from_env(self):
        """Test NEXUS_EMBEDDER=hash:256"""
        with patch.dict(os.environ, {"NEXUS_EMBEDDER": "hash:256"}):
            from nexus_search.core.embedders import get_embedder, HashEmbedder
            embedder = get_embedder()
            self.assertIsInstance(embedder, HashEmbedder)
            self.assertEqual(embedder.dim, 256)

    def test_hash_embedder_default_dim(self):
        """Test NEXUS_EMBEDDER=hash (default dim)"""
        with patch.dict(os.environ, {"NEXUS_EMBEDDER": "hash"}):
            from nexus_search.core.embedders import get_embedder, HashEmbedder
            embedder = get_embedder()
            self.assertIsInstance(embedder, HashEmbedder)
            self.assertEqual(embedder.dim, 384)  # default

    @unittest.skipUnless(os.environ.get("NEXUS_RUN_MODEL_TESTS") == "1", "Requires sentence-transformers")
    def test_st_embedder_from_env(self):
        """Test NEXUS_EMBEDDER=st:model-name"""
        with patch.dict(os.environ, {"NEXUS_EMBEDDER": "st:sentence-transformers/all-MiniLM-L6-v2"}):
            from nexus_search.core.embedders import get_embedder, SentenceTransformerEmbedder
            embedder = get_embedder()
            self.assertIsInstance(embedder, SentenceTransformerEmbedder)
            self.assertEqual(embedder.dim, 384)

    def test_invalid_embedder_config(self):
        """Test invalid NEXUS_EMBEDDER value"""
        with patch.dict(os.environ, {"NEXUS_EMBEDDER": "invalid"}):
            from nexus_search.core.embedders import get_embedder
            with self.assertRaises(ValueError):
                get_embedder()


class TestEmbedderUnavailable(unittest.TestCase):
    """Test EmbedderUnavailable exception and retry behavior."""
    
    def setUp(self):
        from nexus_search.core.embedders import reset_embedder
        reset_embedder()
    
    def tearDown(self):
        from nexus_search.core.embedders import reset_embedder
        reset_embedder()
    
    def test_embedder_unavailable_exception(self):
        from nexus_search.core.embedders import EmbedderUnavailable
        
        with self.assertRaises(EmbedderUnavailable):
            raise EmbedderUnavailable("Model not found")
    
    def test_st_embedder_raises_on_load_failure(self):
        """Test that ST embedder raises EmbedderUnavailable when model fails to load"""
        from nexus_search.core.embedders import SentenceTransformerEmbedder, EmbedderUnavailable
        
        with self.assertRaises(EmbedderUnavailable):
            SentenceTransformerEmbedder("nonexistent-model-that-does-not-exist-12345")
    
    def test_no_silent_hash_fallback(self):
        """CRITICAL: Ensure hash vectors are NEVER stored under ST model name"""
        with patch.dict(os.environ, {"NEXUS_EMBEDDER": "st:nonexistent"}):
            from nexus_search.core.embedders import get_embedder, EmbedderUnavailable
            
            with self.assertRaises(EmbedderUnavailable):
                get_embedder()


class TestHashEmbedderDeterminism(unittest.TestCase):
    """Cross-process determinism tests for HashEmbedder.
    
    These tests run the embedder in a subprocess to verify that the same
    text produces identical vectors across process boundaries.
    """
    
    def _run_embedder_in_subprocess(self, text: str, dim: int = 384) -> list[float]:
        """Run embedder in a separate process and return the embedding as a list."""
        code = f"""
import os
os.environ["NEXUS_EMBEDDER"] = "hash:{dim}"
import sys
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))}")

from nexus_search.core.embedders import HashEmbedder
import numpy as np

embedder = HashEmbedder(dim={dim})
emb = embedder.embed_query({repr(text)})
print(",".join(str(x) for x in emb.tolist()))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            self.fail(f"Subprocess failed: {result.stderr}")
        return [float(x) for x in result.stdout.strip().split(",")]
    
    def test_hash_embedder_deterministic_across_processes(self):
        """Same text should produce identical vectors in separate processes."""
        test_texts = [
            "hello world",
            "machine learning and artificial intelligence",
            "the quick brown fox jumps over the lazy dog",
            "special chars: !@#$%^&*()",
            "unicode: café 🌟 naïve",
            "repeated word " * 10,
        ]
        
        for text in test_texts:
            # Run in two separate subprocesses
            emb1 = self._run_embedder_in_subprocess(text)
            emb2 = self._run_embedder_in_subprocess(text)
            
            self.assertEqual(
                emb1, emb2,
                f"Embedding for '{text}' differs across processes"
            )
    
    def test_hash_embedder_consistent_dimensions(self):
        """Embeddings should have correct dimension."""
        for dim in [128, 256, 384, 512]:
            emb = self._run_embedder_in_subprocess("test text", dim=dim)
            self.assertEqual(len(emb), dim)
    
    def test_hash_embedder_unit_norm(self):
        """Embeddings should be unit norm."""
        import math
        emb = self._run_embedder_in_subprocess("unit norm test")
        norm = math.sqrt(sum(x*x for x in emb))
        self.assertAlmostEqual(norm, 1.0, places=5)
    
    def test_hash_embedder_different_texts_different_vectors(self):
        """Different texts should produce different vectors (with high probability)."""
        emb1 = self._run_embedder_in_subprocess("text one")
        emb2 = self._run_embedder_in_subprocess("text two")
        self.assertNotEqual(emb1, emb2)


if __name__ == "__main__":
    unittest.main()