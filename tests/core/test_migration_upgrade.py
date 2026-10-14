"""Populated-DB migration upgrade tests (mission C-data / B6).

The migration-runner unit tests cover fresh files; these tests build
REALISTIC databases at every HISTORICAL schema version (with rows), open
them with the CURRENT store classes, and verify:
  (a) the store reports the current version,
  (b) every pre-existing row survives readable and byte-identical,
  (c) the new-version features actually work on the upgraded file,
  (d) data backfills (link-graph domain/is_internal columns) ran,
  (e) the pre-migrations-runner era (no schema_version table at all)
      baselines without data loss.
"""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from nexus_search.core.migrations import apply_migrations, get_version
from nexus_search.core.storage import SCHEMA as STORAGE_V1
from nexus_search.crawler.frontier import SCHEMA as FRONTIER_V1
from nexus_search.links.graph import SCHEMA as LINKGRAPH_V1, LinkGraph
from nexus_search.core.storage import Storage
from nexus_search.crawler.frontier import Frontier


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "hist.db")

    def tearDown(self):
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)


class TestStorageUpgrade(_TempDb):
    DOCS = [
        ("web:https://old.example/a", "Page A", "alpha beta content", "web",
         3, '{"url": "https://old.example/a", "quality": 0.7}', 1700000000.5),
        ("file:report.txt", "Report", "gamma delta epsilon", "file",
         3, '{"path": "report.txt"}', 1700000001.5),
        ("doc-no-meta", "", "zeta", "text", 1, "{}", 1700000002.5),
    ]

    def _build_era(self, version):
        conn = sqlite3.connect(self.path)
        migrations = [(1, STORAGE_V1)]
        if version >= 2:
            migrations.append((2, "CREATE INDEX IF NOT EXISTS idx_documents_added_at "
                                  "ON documents (added_at);"))
        if version >= 3:
            migrations.append((3, "CREATE TABLE IF NOT EXISTS rag_token_usage ("
                                  "bucket TEXT NOT NULL, day TEXT NOT NULL, "
                                  "tokens INTEGER NOT NULL DEFAULT 0, "
                                  "PRIMARY KEY (bucket, day))"))
        if version >= 4:
            migrations.append((4, "CREATE TABLE IF NOT EXISTS rag_sessions ("
                                  "scope TEXT NOT NULL, session_id TEXT NOT NULL, "
                                  "turn_idx INTEGER NOT NULL, role TEXT NOT NULL, "
                                  "text TEXT NOT NULL, updated_at REAL NOT NULL, "
                                  "PRIMARY KEY (scope, session_id, turn_idx))"))
        apply_migrations(conn, "storage", migrations)
        for row in self.DOCS:
            conn.execute(
                "INSERT INTO documents (doc_id, title, content, doc_type, "
                "length, metadata, added_at) VALUES (?,?,?,?,?,?,?)", row)
        conn.execute("INSERT INTO postings (term, doc_id, term_freq) VALUES "
                     "('alpha', ?, 1)", (self.DOCS[0][0],))
        if version >= 3:
            conn.execute("INSERT INTO rag_token_usage (bucket, day, tokens) "
                         "VALUES ('key:legacy', '2026-01-01', 5000)")
        if version >= 4:
            conn.execute("INSERT INTO rag_sessions (scope, session_id, turn_idx, "
                         "role, text, updated_at) VALUES "
                         "('key:legacy', 's-old', 0, 'user', 'old turn', 1.5)")
        conn.commit()
        conn.close()

    def _assert_upgraded_and_intact(self, storage):
        self.assertEqual(storage.schema_version, 4)
        for doc_id, title, content, doc_type, length, meta, added in self.DOCS:
            doc = storage.get_document(doc_id)
            self.assertIsNotNone(doc, f"{doc_id} lost in upgrade")
            self.assertEqual((doc.title, doc.content, doc.doc_type, doc.length),
                             (title, content, doc_type, length))
            self.assertEqual(doc.metadata, eval(meta))  # small literals
            self.assertAlmostEqual(doc.added_at, added)
        # postings survived and are queryable through the search path
        self.assertEqual(storage.document_frequency("alpha"), 1)

    def test_upgrade_from_v1_populated(self):
        self._build_era(1)
        storage = Storage(self.path)
        try:
            self._assert_upgraded_and_intact(storage)
            # v3/v4 features now work on the upgraded file
            self.assertEqual(storage.rag_usage_get("key:legacy"), 0,
                             "no legacy usage rows exist on a v1 file")
            storage.rag_usage_add("key:new", 100)
            self.assertEqual(storage.rag_usage_get("key:new"), 100)
            storage.rag_session_append("key:new", "s", 0, "user", "hi", 1.0)
            self.assertEqual(len(storage.rag_session_turns("key:new", "s")), 1)
        finally:
            storage.close()

    def test_upgrade_from_v3_populated_keeps_usage_rows(self):
        self._build_era(3)
        storage = Storage(self.path)
        try:
            self._assert_upgraded_and_intact(storage)
            # the v3-era budget row must survive the v3->v4 upgrade
            self.assertEqual(storage.rag_usage_get("key:legacy", day="2026-01-01"),
                             5000)
            # and the v4 session feature works
            storage.rag_session_append("k", "s", 0, "assistant", "a", 2.0)
            self.assertEqual(storage.rag_session_count(), 1)
        finally:
            storage.close()

    def test_upgrade_from_v4_populated_keeps_sessions(self):
        self._build_era(4)
        storage = Storage(self.path)
        try:
            self._assert_upgraded_and_intact(storage)
            turns = storage.rag_session_turns("key:legacy", "s-old")
            self.assertEqual([(t[1], t[2]) for t in turns], [("user", "old turn")])
            self.assertEqual(storage.rag_usage_get("key:legacy", day="2026-01-01"),
                             5000)
        finally:
            storage.close()

    def test_pre_migrations_era_db_baselines_without_loss(self):
        """A database written BEFORE the migration runner existed (no
        schema_version table) must baseline and keep its rows."""
        conn = sqlite3.connect(self.path)
        conn.executescript(STORAGE_V1)  # raw DDL, no version table
        conn.execute("INSERT INTO documents (doc_id, title, content, doc_type, "
                     "length, metadata, added_at) VALUES "
                     "('pre-runner', 'P', 'old data', 'text', 2, '{}', 1.0)")
        conn.commit()
        conn.close()
        storage = Storage(self.path)
        try:
            self.assertEqual(storage.schema_version, 4)  # baselined
            doc = storage.get_document("pre-runner")
            self.assertEqual(doc.content, "old data")
        finally:
            storage.close()

    def test_reopen_after_upgrade_is_stable(self):
        self._build_era(1)
        s = Storage(self.path)
        s.rag_usage_add("k", 5)
        s.close()
        s = Storage(self.path)
        try:
            self.assertEqual(s.schema_version, 4)
            self.assertEqual(s.rag_usage_get("k"), 5)
        finally:
            s.close()


class TestFrontierUpgrade(_TempDb):
    def _build_v1(self, with_etag_columns=True):
        conn = sqlite3.connect(self.path)
        apply_migrations(conn, "frontier", [(1, FRONTIER_V1)])
        cols = "" if with_etag_columns else \
            ", etag TEXT, last_modified TEXT"
        # simulate era-correct rows
        conn.execute("INSERT INTO frontier (url, depth, priority, status, "
                     "added_at) VALUES ('https://old.example/x', 2, 5, "
                     "'pending', 1000.0)")
        conn.execute("INSERT INTO visited (url, last_crawled_at, content_hash"
                     + (", etag, last_modified" if with_etag_columns else "") +
                     ") VALUES ('https://old.example/done', 2000.0, 'abc'"
                     + (", '\"v1\"', 'Mon, 01 Jan 2026 00:00:00 GMT'"
                        if with_etag_columns else "") + ")")
        conn.execute("INSERT INTO crawl_errors (url, error, timestamp) "
                     "VALUES ('https://old.example/e', 'HTTP 404', 1500.0)")
        conn.commit()
        conn.close()

    def test_upgrade_from_v1_populated(self):
        self._build_v1()
        frontier = Frontier(self.path)
        try:
            self.assertEqual(frontier.schema_version, 2)
            # v2 columns exist and default correctly
            row = frontier.conn.execute(
                "SELECT retry_count, next_retry_at FROM frontier "
                "WHERE url = 'https://old.example/x'").fetchone()
            self.assertEqual(row, (0, 0.0))
            # crawl-history rows survived
            visited = frontier.get_visited("https://old.example/done")
            self.assertEqual(visited[1], "abc")
            self.assertEqual(visited[2], '"v1"')
            # the frontier still serves the pending URL with validators
            batch = frontier.next_batch(10)
            self.assertEqual(len(batch), 1)
            # errors preserved
            n = frontier.conn.execute("SELECT COUNT(*) FROM crawl_errors"
                                      ).fetchone()[0]
            self.assertEqual(n, 1)
            # mark_error now uses the v2 backoff columns
            frontier.mark_error("https://old.example/x", "test", permanent=True)
            self.assertEqual(frontier.pending_count(), 0)
        finally:
            frontier.close()

    def test_ancient_visited_without_etag_columns_repaired(self):
        """Pre-runner visited rows lacking etag/last_modified (the
        belt-and-braces _ensure_column path) must gain them silently."""
        conn = sqlite3.connect(self.path)
        conn.executescript(FRONTIER_V1)
        # drop the era columns to simulate the ANCIENT shape
        conn.execute("CREATE TABLE visited_old (url TEXT PRIMARY KEY, "
                     "last_crawled_at REAL NOT NULL, content_hash TEXT)")
        conn.execute("INSERT INTO visited_old VALUES "
                     "('https://ancient.example/p', 300.0, 'hash')")
        conn.execute("DROP TABLE visited")
        conn.execute("ALTER TABLE visited_old RENAME TO visited")
        conn.commit()
        conn.close()
        frontier = Frontier(self.path)
        try:
            row = frontier.conn.execute(
                "SELECT etag, last_modified FROM visited "
                "WHERE url = 'https://ancient.example/p'").fetchone()
            self.assertEqual(row, (None, None))
        finally:
            frontier.close()


class TestLinkGraphUpgrade(_TempDb):
    def _build_v1(self):
        conn = sqlite3.connect(self.path)
        apply_migrations(conn, "link_graph", [(1, LINKGRAPH_V1)])
        edges = [
            ("http://inside.com/a", "http://inside.com/b", "internal anchor", ""),
            ("http://inside.com/a", "http://outside.com/c", "external", "nofollow"),
        ]
        for f, t, a, r in edges:
            conn.execute(
                "INSERT INTO link_edges (from_url, to_url, anchor_text, "
                "rel_attrs, first_seen, last_seen) VALUES (?,?,?,?,100.0,200.0)",
                (f, t, a, r))
        conn.commit()
        conn.close()

    def test_upgrade_from_v1_backfills_and_keeps_edges(self):
        self._build_v1()
        graph = LinkGraph(self.path)
        try:
            self.assertEqual(graph.schema_version, 3)
            # v2 backfill: domain columns populated from the URLs
            rows = graph.conn.execute(
                "SELECT from_url, from_domain, to_domain, is_internal "
                "FROM link_edges ORDER BY to_url").fetchall()
            self.assertEqual(rows[0], ("http://inside.com/a", "inside.com",
                                        "inside.com", 1))
            self.assertEqual(rows[1], ("http://inside.com/a", "inside.com",
                                        "outside.com", 0))
            # v3 tables exist and work
            graph.bump_edges_version()
            self.assertEqual(graph.edges_version(), 1)
            # the full authority pipeline runs on the upgraded file
            from nexus_search.links.authority import compute_authority
            stats = compute_authority(graph, force=True)
            self.assertEqual(stats.pages, 2)
            scores = graph.conn.execute(
                "SELECT COUNT(*) FROM authority_scores").fetchone()[0]
            self.assertEqual(scores, 2)
            # nofollow edge was correctly ignored for endorsement
            self.assertEqual(stats.edges_used, 1)
        finally:
            graph.close()

    def test_reopen_after_upgrade_is_stable(self):
        self._build_v1()
        graph = LinkGraph(self.path)
        graph.close()
        graph = LinkGraph(self.path)
        try:
            self.assertEqual(graph.schema_version, 3)
            self.assertEqual(graph.edge_count(), 2)
        finally:
            graph.close()


class TestVectorStoreLegacyTables(_TempDb):
    def test_legacy_embeddings_and_vectors_tables_are_dropped(self):
        """The Phase-4 'doc_vectors' consolidation drops the legacy
        embeddings/vectors tables on first open — downgrade story: the
        old tables' DATA is intentionally discarded (re-embed via
        vector_maintenance), which must happen loudly (logger) and
        without touching doc_vectors."""
        import logging
        from nexus_search.core.vector_store import VectorStore, SCHEMA as VS_V1

        conn = sqlite3.connect(self.path)
        apply_migrations(conn, "doc_vectors", [(1, VS_V1)])
        conn.execute("CREATE TABLE embeddings (doc_id TEXT PRIMARY KEY, "
                     "vector BLOB)")
        conn.execute("CREATE TABLE vectors (doc_id TEXT PRIMARY KEY, "
                     "model TEXT, vector BLOB)")
        conn.execute("INSERT INTO embeddings VALUES ('legacy-1', X'0102')")
        conn.execute("INSERT INTO vectors VALUES ('legacy-2', 'old', X'0304')")
        conn.commit()
        conn.close()

        with self.assertLogs("nexus_search.vector_store", level="INFO") as logs:
            store = VectorStore(self.path, embedder=_EmbedderStub())
        try:
            exists = store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND "
                "name IN ('embeddings','vectors')").fetchall()
            self.assertEqual(exists, [], "legacy tables must be dropped")
            self.assertTrue(any("Dropping legacy table" in line
                                for line in logs.output))
            # the current store works on the same file
            import numpy as np
            store.add("new-doc", np.zeros(8, dtype=np.float32), "h1")
            self.assertEqual(store.count(), 1)
        finally:
            store.close()


class _EmbedderStub:
    name = "hash:8"

    def __init__(self, dim=8):
        self.dim = dim


if __name__ == "__main__":
    unittest.main()
