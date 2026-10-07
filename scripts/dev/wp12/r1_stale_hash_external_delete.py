"""WP12 R1 — stale content hash after an EXTERNAL delete (P2-9 regression).

Two VectorStore instances share one DB. `b` deletes a row; `a` reloads
(PRAGMA data_version bumps). Pre-fix, `a._content_hashes` still held the
deleted doc's hash, so VectorStoreManager.upsert of identical text was
skipped as "unchanged" and the doc never regained a vector.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
os.environ["NEXUS_EMBEDDER"] = "hash:8"

import numpy as np  # noqa: E402

from nexus_search.core.vector_store import VectorStore, VectorStoreManager  # noqa: E402
from nexus_search.core.embedders import get_embedder  # noqa: E402


def make_vec(emb, i):
    x = np.zeros(emb.dim, dtype=np.float32)
    x[i % emb.dim] = 1
    return x


def main():
    emb = get_embedder()
    db = os.path.join(tempfile.mkdtemp(), "v.db")
    mgr = VectorStoreManager(db, embedder=emb)   # LONG-LIVED manager (like the API's)
    a = mgr.store
    b = VectorStore(db_path=db, embedder=emb)    # "external" second instance

    # Use REAL manager upserts so the stored hash is the true sha256 of text
    status0 = mgr.upsert("keep", "text of keep", "", "")
    status1 = mgr.upsert("gone", "text of gone", "", "")
    print(f"initial upserts: keep={status0} gone={status1}")
    b._maybe_reload()
    b.remove("gone")                              # EXTERNAL delete (another process)
    a._maybe_reload()

    print(f"count={a.count()} get_content_hash('gone')={a.get_content_hash('gone')!r}")
    bug = a.get_content_hash("gone") is not None
    print("hash survives external delete -> BUG" if bug
          else "hash cleared on external delete -> correct")

    # User-visible effect: the LONG-LIVED manager's re-upsert of the SAME
    # text is skipped as "unchanged", so the doc never regains a vector.
    status = mgr.upsert("gone", "text of gone", "", "")
    print(f"manager.upsert(same text) after external delete -> {status!r} "
          f"(correct: 'created'; bug: 'unchanged')")
    b._maybe_reload()
    print(f"vector present again in DB after upsert: {b.count() == 2}")
    a.close(); b.close(); mgr.close()
    return 1 if (bug or status != "created" or b.count() != 2) else 0


if __name__ == "__main__":
    raise SystemExit(main())
