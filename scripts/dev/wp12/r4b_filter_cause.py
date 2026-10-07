"""WP12 R4b — cause check: allowed(doc_id) calls get_document for EVERY
vector in the matrix on a filtered hybrid query."""
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
os.environ["NEXUS_EMBEDDER"] = "hash:384"

from nexus_search.core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from nexus_search.core.indexer import Indexer  # noqa: E402
from nexus_search.core.embedding_sync import EmbeddingSync  # noqa: E402
from nexus_search.core.storage import Storage  # noqa: E402
from nexus_search.core.vector_store import VectorStoreManager  # noqa: E402

rng = random.Random(7)
vocab = [f"w{j}" for j in range(3000)]
db = os.path.join(tempfile.mkdtemp(), "r4b.db")
storage = Storage(db)
indexer = Indexer(storage)
vsm = VectorStoreManager(db)
sync = EmbeddingSync(vsm, batch_size=64)
sync.attach(indexer)

for i in range(300):
    content = " ".join(rng.choice(vocab) for _ in range(150))
    indexer.add_document(f"doc{i}", content=content, title=f"t{i}",
                         doc_type="pdf" if i % 2 == 0 else "html",
                         metadata={"language": "en" if i % 2 == 0 else "de"})
sync.flush()

counter = {"n": 0}
orig_get = storage.get_document
storage.get_document = lambda d: (counter.__setitem__("n", counter["n"] + 1) or orig_get(d))

hs = HybridSearch(storage, vector_store=vsm, db_path=db)
counter["n"] = 0
hs.search_page("w10 w20 + type:pdf", top_k=10, mode=SearchMode.HYBRID)
print(f"corpus=300 docs, one filtered query -> get_document calls: {counter['n']} "
      f"(O(corpus) if ~= 300)")
sync.close(); vsm.close(); storage.close()
