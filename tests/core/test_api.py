"""HTTP API tests. Need `pip install fastapi httpx` - skipped if they're missing."""
import importlib
import os
import tempfile
import unittest

# Use hash embedder for offline tests
os.environ["NEXUS_EMBEDDER"] = "hash:384"

try:
    from fastapi.testclient import TestClient
except ImportError:  # fastapi / httpx not installed
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApi(unittest.TestCase):
    def setUp(self):
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "api.db")
        from nexus_search.core import api

        importlib.reload(api)  # fresh empty database for every test
        self.client = TestClient(api.app)

    def add(self, doc_id, content, **extra):
        return self.client.post("/documents", json={"doc_id": doc_id, "content": content, **extra})

    def test_add_then_search(self):
        self.assertEqual(self.add("a", "alpha beta", title="First").status_code, 201)
        body = self.client.get("/search", params={"q": "alpha"}).json()
        self.assertEqual(body["total_results"], 1)
        self.assertEqual(body["results"][0]["doc_id"], "a")

    def test_pagination_reports_true_total(self):
        for i in range(25):
            self.add(f"d{i}", "common word")
        body = self.client.get("/search", params={"q": "common", "top_k": 10, "offset": 20}).json()
        self.assertEqual((body["total_results"], len(body["results"])), (25, 5))

    def test_top_k_is_clamped(self):
        self.add("a", "alpha")
        body = self.client.get("/search", params={"q": "alpha", "top_k": 9999}).json()
        self.assertEqual(body["top_k"], 100)

    def test_blank_query_is_400(self):
        self.assertEqual(self.client.get("/search", params={"q": "   "}).status_code, 400)

    def test_empty_doc_id_is_rejected(self):
        self.assertEqual(self.add("", "x").status_code, 422)

    def test_delete(self):
        self.add("a", "alpha")
        self.assertEqual(self.client.delete("/documents/a").status_code, 200)
        self.assertEqual(self.client.delete("/documents/a").status_code, 404)
        self.assertEqual(self.client.get("/search", params={"q": "alpha"}).json()["total_results"], 0)

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApiPhase5Ux(unittest.TestCase):
    """Phase 5 Stage 4 API surface: /suggest, /related, facets, sort, highlight,
    cursor pagination, has_more. Real HTTP layer (TestClient), not internals."""

    def setUp(self):
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "api.db")
        from nexus_search.core import api
        importlib.reload(api)  # fresh empty database for every test
        self.client = TestClient(api.app)

    def seed(self):
        self.client.post("/documents", json={"doc_id": "a1", "content": "python web crawler tutorial",
                                             "title": "Python Crawler", "doc_type": "web",
                                             "metadata": {"language": "en"}})
        self.client.post("/documents", json={"doc_id": "a2", "content": "python data pipelines",
                                             "title": "Alpha Pipelines", "doc_type": "file",
                                             "metadata": {"language": "en"}})
        self.client.post("/documents", json={"doc_id": "a3", "content": "rust systems programming",
                                             "title": "Rust Systems", "doc_type": "code",
                                             "metadata": {"language": "en"}})

    # ----- /suggest ---------------------------------------------------------
    def test_suggest_endpoint(self):
        self.seed()
        body = self.client.get("/suggest", params={"q": "pyth"}).json()
        self.assertIn("suggestions", body)
        self.assertIn("python", body["suggestions"])

    def test_suggest_empty_prefix(self):
        body = self.client.get("/suggest", params={"q": ""}).json()
        self.assertEqual(body["suggestions"], [])

    def test_related_endpoint_no_log_no_crash(self):
        self.seed()
        r = self.client.get("/related", params={"q": "python"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("related", r.json())

    # ----- sort --------------------------------------------------------------
    def test_sort_title_and_default_unchanged(self):
        self.seed()
        by_title = self.client.get("/search", params={"q": "python", "sort": "title"}).json()
        titles = [r["title"] for r in by_title["results"]]
        self.assertEqual(titles, sorted(titles))
        base = self.client.get("/search", params={"q": "python"}).json()
        relev = self.client.get("/search", params={"q": "python", "sort": "relevance"}).json()
        # default and explicit relevance are byte-identical
        self.assertEqual([(r["doc_id"], r["score"]) for r in base["results"]],
                         [(r["doc_id"], r["score"]) for r in relev["results"]])

    def test_sort_invalid_value_422(self):
        self.seed()
        self.assertEqual(self.client.get("/search", params={"q": "python", "sort": "bogus"}).status_code, 422)

    def test_sort_freshness(self):
        self.seed()
        from nexus_search.core import api as _api  # already reloaded in setUp
        _api._storage.conn.execute("UPDATE documents SET added_at = 1 WHERE doc_id = 'a1'")
        _api._storage.conn.commit()
        body = self.client.get("/search", params={"q": "python", "sort": "freshness"}).json()
        self.assertNotEqual(body["results"][0]["doc_id"], "a1")  # a1 is now oldest

    # ----- highlight ----------------------------------------------------------
    def test_highlight_true_wraps_mark(self):
        self.seed()
        body = self.client.get("/search", params={"q": "python", "highlight": "true"}).json()
        self.assertIn("<mark>python</mark>", body["results"][0]["snippet"].lower())

    def test_highlight_default_off(self):
        self.seed()
        body = self.client.get("/search", params={"q": "python"}).json()
        self.assertNotIn("<mark>", body["results"][0]["snippet"])

    # ----- facets ----------------------------------------------------------
    def test_facets_counts_over_match_population(self):
        self.seed()
        # keyword mode: only the 2 python docs match; hybrid mode ALSO pulls
        # vector candidates (a3), which is correct hybrid behavior
        kw = self.client.get("/search", params={"q": "python", "mode": "keyword",
                                                "facets": "doc_type,language"}).json()
        self.assertIsNotNone(kw.get("facets"))
        self.assertEqual(sum(kw["facets"]["doc_type"].values()),
                         kw["total_results"])  # counts cover ALL matches
        self.assertEqual(kw["facets"]["language"], {"en": 2})
        hy = self.client.get("/search", params={"q": "python", "facets": "doc_type"}).json()
        self.assertEqual(sum(hy["facets"]["doc_type"].values()), hy["total_results"])

    def test_facets_default_absent(self):
        self.seed()
        body = self.client.get("/search", params={"q": "python"}).json()
        self.assertIsNone(body.get("facets"))

    # ----- pagination ---------------------------------------------------------
    def test_has_more_and_next_cursor(self):
        for i in range(5):
            self.client.post("/documents", json={"doc_id": f"d{i}", "content": "common shared term"})
        page1 = self.client.get("/search", params={"q": "common", "top_k": 2}).json()
        self.assertTrue(page1["metadata"]["has_more"])
        self.assertIsNotNone(page1["metadata"]["next_cursor"])
        page2 = self.client.get("/search", params={"q": "common", "top_k": 2,
                                                   "cursor": page1["metadata"]["next_cursor"]}).json()
        ids1 = {r["doc_id"] for r in page1["results"]}
        ids2 = {r["doc_id"] for r in page2["results"]}
        self.assertFalse(ids1 & ids2)  # cursor moves FORWARD, no overlap
        last = self.client.get("/search", params={"q": "common", "top_k": 2, "offset": 4}).json()
        self.assertFalse(last["metadata"]["has_more"])
        self.assertIsNone(last["metadata"]["next_cursor"])

    def test_invalid_cursor_is_400(self):
        self.seed()
        self.assertEqual(self.client.get("/search", params={"q": "python",
                                                            "cursor": "!!!not-base64!!!"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()