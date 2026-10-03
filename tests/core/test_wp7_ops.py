"""WP7: NEXUS_RERANK_WEIGHTS JSON override + backup CLI."""
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")


class TestRerankWeightsJson(unittest.TestCase):
    def _from_env(self, **env):
        saved = {k: os.environ.get(k) for k in
                 ("NEXUS_RERANK_WEIGHTS", "NEXUS_AUTHORITY_WEIGHT")}
        try:
            for key, value in env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            from nexus_search.ranking.ranker import RankingWeights
            return RankingWeights.from_env()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_unset_changes_nothing(self):
        w = self._from_env(NEXUS_RERANK_WEIGHTS=None)
        self.assertEqual(w.title_match, 0.2)  # constructor default intact

    def test_subset_override(self):
        w = self._from_env(NEXUS_RERANK_WEIGHTS='{"title_match": 0.4, "freshness": 0}')
        self.assertEqual(w.title_match, 0.4)
        self.assertEqual(w.freshness, 0.0)
        self.assertEqual(w.bm25_score, 1.0)  # untouched fields keep defaults

    def test_values_clamped_and_garbage_ignored(self):
        w = self._from_env(NEXUS_RERANK_WEIGHTS='{"title_match": 5, "bogus": 1}')
        self.assertEqual(w.title_match, 1.0)  # clamped to [0,1]
        # unknown key ignored without breaking the parse

    def test_invalid_json_is_safe(self):
        w = self._from_env(NEXUS_RERANK_WEIGHTS='{not json')
        self.assertEqual(w.title_match, 0.2)

    def test_composes_with_scalar_envs(self):
        w = self._from_env(NEXUS_AUTHORITY_WEIGHT="0.3",
                           NEXUS_RERANK_WEIGHTS='{"title_match": 0.4}')
        self.assertEqual(w.source_authority, 0.3)
        self.assertEqual(w.title_match, 0.4)


class TestBackupCli(unittest.TestCase):
    def test_backup_and_restore_roundtrip(self):
        from nexus_search.core.backup import backup_db
        from nexus_search.core.hybrid_search import HybridSearch, SearchMode
        from nexus_search.core.indexer import Indexer
        from nexus_search.core.storage import Storage
        import sqlite3

        tmp = tempfile.mkdtemp()
        try:
            db = os.path.join(tmp, "live.db")
            storage = Storage(db)
            Indexer(storage).add_document("d1", "recoverable content",
                                          title="R")
            storage.close()

            out = backup_db(db, os.path.join(tmp, "backups"))
            self.assertTrue(os.path.exists(out))

            # restore = copy back; the restored file must answer searches
            restored = os.path.join(tmp, "restored.db")
            shutil.copy(out, restored)
            storage = Storage(restored)
            hybrid = HybridSearch(storage, db_path=restored)
            try:
                page = hybrid.search_page("recoverable", mode=SearchMode.KEYWORD)
                self.assertEqual([r.doc_id for r in page.results], ["d1"])
            finally:
                hybrid.close()
                storage.close()

            # the WAL sidecar is flushed into the snapshot: the backup must
            # not depend on -wal files that may not exist at restore time
            snap = sqlite3.connect(out)
            count = snap.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            snap.close()
            self.assertEqual(count, 1)
        finally:
            for _ in range(10):
                try:
                    shutil.rmtree(tmp)
                    break
                except PermissionError:
                    import time
                    time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
