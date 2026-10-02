"""WP5 API surface: /graph/neighbors + /graph/report + background PageRank.

/graph/neighbors follows the /search/explain posture: key-gated when a key
is configured, open in dev, bounded inputs. The background worker is opt-in
via NEXUS_AUTHORITY_RECOMPUTE_INTERVAL (0 = off), guarded against overlap,
and stops on lifespan shutdown.
"""
import importlib
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


def _boot(tmpdir: str, **env):
    """Reload the API module against a fresh temp DB. Defaults: dev mode,
    no key; pass NEXUS_ENV/NEXUS_API_KEY to override; any other kwargs are
    plain environment variables."""
    settings = {"NEXUS_ENV": "dev",
                "NEXUS_API_KEY": None,
                "NEXUS_DB": os.path.join(tmpdir, "api.db")}
    for key in ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB"):
        if key in env:
            settings[key] = env.pop(key)
    os.environ["NEXUS_ENV"] = settings["NEXUS_ENV"]
    if settings["NEXUS_API_KEY"] is None:
        os.environ.pop("NEXUS_API_KEY", None)
    else:
        os.environ["NEXUS_API_KEY"] = settings["NEXUS_API_KEY"]
    os.environ["NEXUS_DB"] = settings["NEXUS_DB"]
    for key, value in env.items():
        os.environ[key] = value
    from nexus_search.core import api
    return importlib.reload(api)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestGraphEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB",
                        "NEXUS_RATE_LIMIT", "NEXUS_CACHE_TTL",
                        "NEXUS_AUTHORITY_RECOMPUTE_INTERVAL")}
        os.environ["NEXUS_RATE_LIMIT"] = "10000/minute"
        self.api = _boot(self.tmpdir)
        self.client = TestClient(self.api.app)
        # a small known graph, written through the production choke point
        graph = self.api._link_graph
        graph.record_edge("http://a.com/x", "http://b.com/y", "see b", "")
        graph.record_edge("http://b.com/y", "http://c.com/z", "", "nofollow")
        graph.record_edge("http://ext.com/p", "http://a.com/x", "back", "")

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from nexus_search.core import api
        importlib.reload(api)
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_neighbors_out_in_both(self):
        r = self.client.get("/graph/neighbors",
                            params={"url": "http://a.com/x", "direction": "out"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["edges"][0]["to"], "http://b.com/y")
        self.assertEqual(body["edges"][0]["anchor_text"], "see b")

        r = self.client.get("/graph/neighbors",
                            params={"url": "http://a.com/x", "direction": "in"})
        self.assertEqual([e["from"] for e in r.json()["edges"]],
                          ["http://ext.com/p"])

        r = self.client.get("/graph/neighbors",
                            params={"url": "http://a.com/x", "direction": "both"})
        self.assertEqual(r.json()["count"], 2)

    def test_variant_url_resolves(self):
        # normalization choke point applies to traversal lookups too
        r = self.client.get("/graph/neighbors",
                            params={"url": "http://a.com/x#frag",
                                    "direction": "out"})
        self.assertEqual(r.json()["count"], 1)

    def test_bad_direction_and_limit_rejected(self):
        r = self.client.get("/graph/neighbors",
                            params={"url": "http://a.com/x", "direction": "sideways"})
        self.assertEqual(r.status_code, 422)
        r = self.client.get("/graph/neighbors",
                            params={"url": "http://a.com/x", "limit": 500})
        self.assertEqual(r.status_code, 422)

    def test_key_gated_in_production(self):
        prod_api = _boot(self.tmpdir, NEXUS_ENV="production",
                         NEXUS_API_KEY="graph-secret")
        client = TestClient(prod_api.app)
        try:
            r = client.get("/graph/neighbors", params={"url": "http://a.com/x"})
            self.assertEqual(r.status_code, 401,
                             "graph introspection must be key-gated in production")
            r = client.get("/graph/neighbors", params={"url": "http://a.com/x"},
                           headers={"X-API-Key": "graph-secret"})
            self.assertEqual(r.status_code, 200)
        finally:
            pass

    def test_graph_report_counts(self):
        r = self.client.get("/graph/report")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["edges"], 3)
        # a->b->c is one component; ext->a joins it => one component of 4
        self.assertEqual(body["component_count"], 1)
        self.assertEqual(body["largest_component"], 4)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestBackgroundAuthorityWorker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB",
                        "NEXUS_RATE_LIMIT", "NEXUS_CACHE_TTL",
                        "NEXUS_AUTHORITY_RECOMPUTE_INTERVAL")}

    def tearDown(self):
        # stop THIS module's worker before the reload, then restore env
        from nexus_search.core import api as current
        if getattr(current, "_authority_worker", None) is not None:
            current._authority_worker.stop()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(current)
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                break
            except PermissionError:
                time.sleep(0.2)

    def test_worker_recomputes_and_lifespan_stops_it(self):
        self.api = _boot(self.tmpdir, NEXUS_AUTHORITY_RECOMPUTE_INTERVAL="0.5",
                         NEXUS_RATE_LIMIT="10000/minute")
        self.worker = self.api._authority_worker
        self.assertIsNotNone(self.worker)
        with TestClient(self.api.app) as client:  # lifespan active
            # a "crawler process" writes edges through its own connection
            from nexus_search.links.graph import LinkGraph
            writer = LinkGraph(self.api._NEXUS_DB if hasattr(self.api, "_NEXUS_DB")
                               else os.environ["NEXUS_DB"])
            try:
                for i in range(4):
                    writer.record_edge(f"http://d{i}.com", "http://hub.com/page")
            finally:
                writer.close()
            # the worker must pick the new edges up and refresh scores
            deadline = time.time() + 8
            found = None
            while time.time() < deadline:
                found = self.api._link_graph.authority_for("http://hub.com/page")
                if found is not None:
                    break
                time.sleep(0.25)
            self.assertIsNotNone(found,
                                 "background worker must recompute new edges")
            self.assertGreater(found[0], 0.0)
        # lifespan shutdown: the worker thread is stopped and stays stopped
        deadline = time.time() + 10
        while self.worker.alive and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(self.worker.alive,
                         "lifespan shutdown must stop the background worker")

    def test_worker_off_by_default(self):
        os.environ.pop("NEXUS_AUTHORITY_RECOMPUTE_INTERVAL", None)
        self.api = _boot(self.tmpdir, NEXUS_RATE_LIMIT="10000/minute")
        self.assertIsNone(self.api._authority_worker,
                          "NEXUS_AUTHORITY_RECOMPUTE_INTERVAL unset = no worker")


class TestReports(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.main_db = os.path.join(self.dir, "main.db")
        self.frontier_db = os.path.join(self.dir, "frontier.db")

    def tearDown(self):
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_dead_links_report(self):
        import sqlite3
        from nexus_search.links.graph import LinkGraph
        from nexus_search.links.reports import dead_links

        graph = LinkGraph(self.main_db)
        graph.record_edge("http://src.com/page", "http://gone.com/missing")
        graph.record_edge("http://src.com/other", "http://gone.com/missing")
        graph.record_edge("http://src.com/page", "http://ok.com/fine")
        graph.close()

        frontier = sqlite3.connect(self.frontier_db)
        frontier.executescript(
            "CREATE TABLE IF NOT EXISTS visited (url TEXT PRIMARY KEY, "
            "last_crawled_at REAL NOT NULL, content_hash TEXT, etag TEXT, "
            "last_modified TEXT);"
            "CREATE TABLE IF NOT EXISTS crawl_errors (url TEXT NOT NULL, "
            "error TEXT NOT NULL, timestamp REAL NOT NULL);")
        frontier.execute("INSERT INTO visited VALUES ('http://ok.com/fine', 1, NULL, NULL, NULL)")
        frontier.execute("INSERT INTO crawl_errors VALUES (?, ?, ?)",
                         ("http://gone.com/missing", "HTTP 404", 100.0))
        frontier.commit()
        frontier.close()

        report = dead_links(self.main_db, self.frontier_db)
        self.assertEqual(len(report["dead"]), 1)
        entry = report["dead"][0]
        self.assertEqual(entry["url"], "http://gone.com/missing")
        self.assertEqual(entry["error"], "HTTP 404")
        self.assertEqual(entry["inlink_count"], 2)
        self.assertIn("http://src.com/page", entry["inlinks"])
        # the successfully crawled target is not listed anywhere
        self.assertEqual(report["unfetched"], [])

    def test_orphans_report(self):
        from nexus_search.core.indexer import Indexer
        from nexus_search.core.storage import Storage
        from nexus_search.links.graph import LinkGraph
        from nexus_search.links.reports import orphan_pages

        storage = Storage(self.main_db)
        ix = Indexer(storage)
        graph = LinkGraph(self.main_db)
        ix.add_document("web:linked", "content", title="L", doc_type="web",
                        metadata={"url": "http://site.com/linked", "depth": 1})
        ix.add_document("web:orphan", "content", title="O", doc_type="web",
                        metadata={"url": "http://site.com/orphan", "depth": 1})
        ix.add_document("web:seed", "content", title="S", doc_type="web",
                        metadata={"url": "http://site.com/seed", "depth": 0})
        ix.add_document("plain", "content", title="P", doc_type="text")
        graph.record_edge("http://other.com/x", "http://site.com/linked")

        orphans = orphan_pages(storage, graph)
        self.assertEqual(orphans, [{"doc_id": "web:orphan",
                                    "url": "http://site.com/orphan"}],
                         "linked page excluded, seed excluded, non-web excluded")

        with_seeds = orphan_pages(storage, graph, exclude_seeds=False)
        urls = {o["url"] for o in with_seeds}
        self.assertEqual(urls, {"http://site.com/orphan", "http://site.com/seed"})
        graph.close()
        storage.close()


if __name__ == "__main__":
    unittest.main()
