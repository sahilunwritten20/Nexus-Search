"""WP12 R4 — filtered hybrid search is O(corpus) per query.

Corpus sizes 500/1,500/3,000 (3,000-word vocab, 150 words/doc, half
doc_type=pdf, half language=en). Times plain "w10 w20" vs "+ type:pdf"
vs "+ lang:en" (HYBRID, top_k=10).

Reviewer saw plain/filtered ~3.5/10, 12/29, 25/62 ms (filtered = linear
in corpus: every vector candidate costs a full get_document via
allowed(doc_id) pushdown).
"""
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
os.environ["NEXUS_EMBEDDER"] = "hash:384"

from nexus_search.core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from nexus_search.core.indexer import Indexer  # noqa: E402
from nexus_search.core.embedding_sync import EmbeddingSync  # noqa: E402
from nexus_search.core.storage import Storage  # noqa: E402
from nexus_search.core.vector_store import VectorStoreManager  # noqa: E402

VOCAB = [f"w{i}" for i in range(3000)]


def build(n, seed):
    rng = random.Random(seed)
    db = os.path.join(tempfile.mkdtemp(), f"r4_{n}.db")
    storage = Storage(db)
    indexer = Indexer(storage)
    vsm = VectorStoreManager(db)
    sync = EmbeddingSync(vsm, batch_size=64)
    sync.attach(indexer)
    for i in range(n):
        content = " ".join(rng.choice(VOCAB) for _ in range(150))
        doc_type = "pdf" if i % 2 == 0 else "html"
        lang = "en" if i % 2 == 0 else "de"
        indexer.add_document(f"doc{i}", content=content, title=f"t{i}",
                            doc_type=doc_type, metadata={"language": lang})
    sync.flush()
    hs = HybridSearch(storage, vector_store=vsm, db_path=db)
    return hs, storage, sync, vsm


def timeit(hs, q, reps=5):
    best = None
    for _ in range(reps):
        t0 = time.perf_counter()
        page = hs.search_page(q, top_k=10, mode=SearchMode.HYBRID)
        dt = (time.perf_counter() - t0) * 1000
        best = dt if best is None else min(best, dt)
    return best, len(page.results)


for n in (500, 1500, 3000):
    hs, storage, sync, vsm = build(n, seed=n)
    for q in ("w10 w20", "w10 w20 + type:pdf", "w10 w20 + lang:en"):
        ms, k = timeit(hs, q)
        print(f"corpus={n:5d} {q!r:24} -> {ms:6.1f} ms ({k} results)")
    sync.close(); vsm.close(); storage.close()
