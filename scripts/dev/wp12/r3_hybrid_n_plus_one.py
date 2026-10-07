"""WP12 R3 — N+1 Storage.get_document calls per hybrid query.

Corpus: 1,500 docs, hash:384, 400-word vocab, 120 words/doc via Indexer +
EmbeddingSync. Counter-wrapped Storage.get_document; HYBRID search_page
for "w5" and "w5 w6" (top_k=10).

Reviewer saw: 886 and 1,661 get_document calls (88-107 ms).
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

N_DOCS = 1500
VOCAB = [f"w{i}" for i in range(400)]
WORDS_PER_DOC = 120
rng = random.Random(42)

db = os.path.join(tempfile.mkdtemp(), "r3.db")
storage = Storage(db)
indexer = Indexer(storage)
vsm = VectorStoreManager(db)
sync = EmbeddingSync(vsm, batch_size=64)
sync.attach(indexer)

for i in range(N_DOCS):
    content = " ".join(rng.choice(VOCAB) for _ in range(WORDS_PER_DOC))
    indexer.add_document(f"doc{i}", content=content, title=f"t{i}")

counter = {"n": 0}
orig_get = storage.get_document


def counting_get(doc_id):
    counter["n"] += 1
    return orig_get(doc_id)


storage.get_document = counting_get

hs = HybridSearch(storage, vector_store=vsm, db_path=db)
for q in ("w5", "w5 w6"):
    counter["n"] = 0
    t0 = time.perf_counter()
    page = hs.search_page(q, top_k=10, mode=SearchMode.HYBRID)
    dt = (time.perf_counter() - t0) * 1000
    print(f"query {q!r}: results={len(page.results)} get_document_calls={counter['n']} "
          f"latency={dt:.0f} ms")

storage.get_document = orig_get
sync.close(); vsm.close(); storage.close()
