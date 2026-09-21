"""HTTP API tests. Need `pip install fastapi httpx` - skipped if they're missing."""
import importlib
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()