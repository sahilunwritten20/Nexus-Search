"""Reviewer's exact R1 repro (variant): plain two-instance stale-hash check.

HEAD -> `1 h-gone` (BUG per reviewer); cb64c36 -> `1 None`.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["NEXUS_EMBEDDER"] = "hash:8"

import numpy as np  # noqa: E402
from nexus_search.core.vector_store import VectorStore  # noqa: E402
from nexus_search.core.embedders import get_embedder  # noqa: E402

emb = get_embedder()
db = os.path.join(tempfile.mkdtemp(), "v.db")
a = VectorStore(db_path=db, embedder=emb)
b = VectorStore(db_path=db, embedder=emb)


def v(i):
    x = np.zeros(emb.dim, dtype=np.float32)
    x[i % emb.dim] = 1
    return x


a.add("keep", v(0), "h-keep")
a.add("gone", v(1), "h-gone")
b._maybe_reload()
b.remove("gone")
a._maybe_reload()
print(a.count(), a.get_content_hash("gone"))
