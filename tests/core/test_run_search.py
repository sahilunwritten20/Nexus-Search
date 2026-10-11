"""WP14 Item 2 — direct unit tests for the extracted `_run_search` helper.

The endpoint `/search` must be a thin decorator shell over `_run_search`:
same validation order, same errors, same SearchResponse. These tests call
the helper DIRECTLY (no HTTP layer) to pin its contract for Phase 7's
`/ask`, plus a drift-guard: the endpoint's declared query parameters must
exactly match the helper's inputs.
"""
import importlib
import inspect
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_ENV", "dev")
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from fastapi import HTTPException

from nexus_search.core.models import SearchResponse

HELPER_PARAMS = {
    "q", "top_k", "offset", "diversity", "mode", "bm25_weight",
    "vector_weight", "fusion", "candidates", "debug", "rerank", "session",
    "sort", "highlight", "facets", "cursor", "min_score", "api_key",
}


class TestRunSearchHelper(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_db = os.environ.get("NEXUS_DB")
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(prefix="wp14_rs_"),
                                              "rs.db")
        from nexus_search.core import api
        importlib.reload(api)
        cls.api = api
        for i in range(3):
            api._indexer.add_document(doc_id=f"d{i}",
                                       content=f"alpha beta doc {i}",
                                       title=f"Doc {i}")

    @classmethod
    def tearDownClass(cls):
        if cls._saved_db is None:
            os.environ.pop("NEXUS_DB", None)
        else:
            os.environ["NEXUS_DB"] = cls._saved_db
        importlib.reload(cls.api)

    def _call(self, **overrides):
        params = dict(
            q="alpha", top_k=10, offset=0, diversity=0.0, mode="hybrid",
            bm25_weight=1.0, vector_weight=1.0, fusion="rrf",
            candidates=50, debug=False, rerank=None, session=None,
            sort="relevance", highlight=False, facets=None, cursor=None,
            min_score=None, api_key=None,
        )
        params.update(overrides)
        return self.api._run_search(**params)

    def test_helper_signature_is_the_search_contract(self):
        # drift-guard: _run_search carries exactly the /search inputs
        self.assertEqual(set(inspect.signature(self.api._run_search).parameters),
                         HELPER_PARAMS)

    def test_endpoint_declares_no_parameter_the_helper_lacks(self):
        endpoint = next(r for r in self.api.app.routes
                        if getattr(r, "path", None) == "/search")
        sig_params = set(inspect.signature(endpoint.endpoint).parameters)
        self.assertEqual(sig_params - {"request", "response"}, HELPER_PARAMS)

    def test_plain_call_returns_search_response(self):
        out = self._call()
        self.assertIsInstance(out, SearchResponse)
        self.assertGreater(len(out.results), 0)
        self.assertEqual(out.query, "alpha")

    def test_empty_query_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(q="   ")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "q must not be empty")

    def test_too_long_query_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(q="x" * 2001)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "q too long")

    def test_too_many_words_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(q=" ".join(["w"] * 129))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_window_over_max_candidates_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(offset=1000, top_k=10)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("NEXUS_MAX_CANDIDATES", ctx.exception.detail)

    def test_bad_cursor_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(cursor="!!!not-a-cursor!!!")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "invalid cursor")

    def test_both_weights_zero_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(bm25_weight=0.0, vector_weight=0.0)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "At least one weight must be > 0")

    def test_diversity_with_non_relevance_sort_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._call(diversity=0.5, sort="freshness")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_top_k_clamped_to_100(self):
        out = self._call(top_k=10_000, offset=0)
        self.assertEqual(out.top_k, 100)

    def test_cursor_decodes_offset(self):
        import base64
        cursor = base64.urlsafe_b64encode(b"offset:1").decode().rstrip("=")
        out = self._call(cursor=cursor)
        self.assertEqual(out.offset, 1)


if __name__ == "__main__":
    unittest.main()
