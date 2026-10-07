"""WP12 R2 — RRF ignores raw similarity / vector search has no default
min_score floor / weighted-fusion top-1 is always 1.0.

Demonstrates the scored-signal problem Phase 7's refuse-gate must NOT
inherit: fused scores are rank/normalization artifacts, not confidence.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
os.environ["NEXUS_EMBEDDER"] = "hash:8"

import numpy as np  # noqa: E402
from nexus_search.core.hybrid_search import _rrf_fuse, _weighted_fuse  # noqa: E402
from nexus_search.core.vector_store import VectorStore  # noqa: E402
from nexus_search.core.embedders import get_embedder  # noqa: E402

# (a) same doc, wildly different raw vector similarity -> identical RRF score
hi = _rrf_fuse({"a": (9.1, None)}, {"a": (0.92, "a")})[0][1]
lo = _rrf_fuse({"a": (0.2, None)}, {"a": (0.05, "a")})[0][1]
print(f"RRF score with vector sim 0.92: {hi:.5f}")
print(f"RRF score with vector sim 0.05: {lo:.5f}")
print(f"invariant to raw similarity: {abs(hi - lo) < 1e-12}")

# (b) vector search returns hits for a nonsense query (no default floor)
emb = get_embedder()
import tempfile  # noqa: E402
vs = VectorStore(os.path.join(tempfile.mkdtemp(), "v.db"), embedder=emb)
vs.add("d1", emb.embed_query("alpha beta gamma"), "h1")
vs.add("d2", emb.embed_query("one two three"), "h2")
hits = vs.search(emb.embed_query("zzzqqq nonsense"), top_k=5)
print(f"nonsense query hits (min_score default -1.0): {[(h.doc_id, round(h.score, 3)) for h in hits]}")

# (c) weighted fusion top-1 is always 1.0 (min-max normalization artifact)
w1 = _weighted_fuse({"a": (9.1, None)}, {"a": (0.92, "a")}, 1.0, 1.0)[0]
w2 = _weighted_fuse({"a": (0.2, None), "b": (0.1, None)}, {"a": (0.05, "a"), "b": (0.01, "b")}, 1.0, 1.0)[0]
print(f"weighted fusion top-1 (strong signals): {w1[0]} -> {w1[1]:.3f}")
print(f"weighted fusion top-1 (weak signals):   {w2[0]} -> {w2[1]:.3f}")
vs.close()
