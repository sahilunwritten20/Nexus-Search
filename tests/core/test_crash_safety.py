"""Chaos: crash-safety of the critical write sections (mission C-data).

A writer process is HARD-KILLED at every critical section; the store
must then open cleanly, report integrity ok, and never serve a
half-written document (row without postings, or postings without a row).
Windows note: TerminateProcess is this platform's kill -9 (no cleanup
handlers, no flushes) — exactly the failure mode under test.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WRITE_SCRIPT = r"""
import os, sys, time
sys.path.insert(0, {repo!r})
from nexus_search.core.storage import Storage
storage = Storage({db!r})
marker = {marker!r}
# Signal readiness, then write docs in a loop until killed.
with open(marker, "w") as f:
    f.write("ready")
i = 0
while True:
    i += 1
    storage.upsert_document_with_postings(
        doc_id=f"crash-{{i}}", title=f"t{{i}}",
        content=f"content number {{i}} with words",
        doc_type="text", length=4,
        metadata={{"i": i}}, term_freqs={{"content": 1, "number": 1}})
    if i % 5 == 0:
        with open(marker, "a") as f:
            f.write(f"{{i}};")
""".strip()


class TestCrashMidWrite(unittest.TestCase):
    def _spawn_writer(self, db, marker):
        code = WRITE_SCRIPT.format(repo=REPO, db=db, marker=marker)
        return subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _kill_after(self, proc, marker, delay):
        deadline = time.time() + 20
        while time.time() < deadline:
            if os.path.exists(marker):
                time.sleep(delay)  # let it write a few documents
                proc.kill()       # TerminateProcess == kill -9
                return proc.wait(timeout=10)
            time.sleep(0.05)
        proc.kill()
        self.fail("writer never became ready")

    def _consistency_check(self, db):
        """Documents and postings must be PAIRED: no orphan postings, no
        postings-less document rows (the mission's index-vs-docs checker)."""
        import sqlite3
        conn = sqlite3.connect(db)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        orphan_postings = conn.execute(
            "SELECT COUNT(*) FROM postings p WHERE NOT EXISTS "
            "(SELECT 1 FROM documents d WHERE d.doc_id = p.doc_id)"
        ).fetchone()[0]
        docs_without_postings = conn.execute(
            "SELECT COUNT(*) FROM documents d WHERE NOT EXISTS "
            "(SELECT 1 FROM postings p WHERE p.doc_id = d.doc_id)"
        ).fetchone()[0]
        conn.close()
        return integrity, orphan_postings, docs_without_postings

    def test_kill9_mid_write_leaves_consistent_index(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "crash.db")
        marker = os.path.join(d, "ready.marker")
        for delay in (0.0, 0.05, 0.15, 0.3):
            with self.subTest(kill_delay=delay):
                proc = self._spawn_writer(db, marker)
                self._kill_after(proc, marker, delay)
                # the store must OPEN on the killed file...
                from nexus_search.core.storage import Storage
                storage = Storage(db)
                try:
                    self.assertEqual(storage.conn.execute(
                        "PRAGMA integrity_check").fetchone()[0], "ok")
                    integrity, orphan_p, bare_d = self._consistency_check(db)
                    self.assertEqual(integrity, "ok")
                    self.assertEqual(orphan_p, 0,
                                     "postings rows without their document")
                    self.assertEqual(bare_d, 0,
                                     "documents with zero postings")
                    # and the next writer continues without error
                    storage.upsert_document_with_postings(
                        "after-crash", "t", "c", "text", 1, {}, {"c": 1})
                    self.assertIsNotNone(storage.get_document("after-crash"))
                finally:
                    storage.close()
                # reset marker for the next round
                if os.path.exists(marker):
                    os.remove(marker)

    def test_kill9_during_bulk_then_query_paths_work(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "crash2.db")
        marker = os.path.join(d, "ready2.marker")
        proc = self._spawn_writer(db, marker)
        self._kill_after(proc, marker, 0.1)
        # a full SEARCH stack must boot on the crashed file
        from nexus_search.core.bm25 import BM25Search
        from nexus_search.core.storage import Storage
        storage = Storage(db)
        try:
            page = BM25Search(storage).search_page("content number", top_k=10)
            self.assertIsInstance(page.total, int)
        finally:
            storage.close()


if __name__ == "__main__":
    unittest.main()
