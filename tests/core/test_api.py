"""HTTP API tests. Need `pip install fastapi httpx` - skipped if they're missing."""
import importlib
import os
import tempfile
import unittest

# Use hash embedder for offline tests
os.environ["NEXUS_EMBEDDER"] = "hash:384"
# These tests exercise API behavior, not boot policy (TestApiBootPolicy below
# covers that): opt the module into the explicit dev mode the API requires
# when no NEXUS_API_KEY is set.
os.environ.setdefault("NEXUS_ENV", "dev")

try:
    from fastapi.testclient import TestClient
except ImportError:  # fastapi / httpx not installed
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApiBootPolicy(unittest.TestCase):
    """Fail-closed startup: no NEXUS_API_KEY outside dev must not boot."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("NEXUS_ENV", "NEXUS_API_KEY")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from nexus_search.core import api
        importlib.reload(api)  # back to a booting state for other tests

    def test_production_default_without_key_refuses_to_boot(self):
        os.environ.pop("NEXUS_API_KEY", None)
        os.environ.pop("NEXUS_ENV", None)  # unset == production
        from nexus_search.core import api
        with self.assertRaises(RuntimeError) as ctx:
            importlib.reload(api)
        self.assertIn("NEXUS_API_KEY", str(ctx.exception))
        self.assertIn("NEXUS_ENV=dev", str(ctx.exception))

    def test_explicit_production_without_key_refuses_to_boot(self):
        os.environ.pop("NEXUS_API_KEY", None)
        os.environ["NEXUS_ENV"] = "production"
        from nexus_search.core import api
        with self.assertRaises(RuntimeError):
            importlib.reload(api)

    def test_dev_without_key_boots_open(self):
        os.environ.pop("NEXUS_API_KEY", None)
        os.environ["NEXUS_ENV"] = "dev"
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "boot.db")
        from nexus_search.core import api
        importlib.reload(api)
        self.assertEqual(TestClient(api.app).get("/health").status_code, 200)

    def test_production_with_key_boots(self):
        os.environ["NEXUS_API_KEY"] = "boot-secret"
        os.environ["NEXUS_ENV"] = "production"
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "boot.db")
        from nexus_search.core import api
        importlib.reload(api)
        self.assertEqual(TestClient(api.app).get("/health").status_code, 200)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApiRateLimit(unittest.TestCase):
    """NEXUS_RATE_LIMIT gate on /search and /documents: 429 + Retry-After."""

    _SAVED_KEYS = ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_RATE_LIMIT")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._SAVED_KEYS}
        os.environ["NEXUS_ENV"] = "dev"
        os.environ.pop("NEXUS_API_KEY", None)
        os.environ["NEXUS_RATE_LIMIT"] = "3/minute"
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "rl.db")
        from nexus_search.core import api
        importlib.reload(api)
        self.client = TestClient(api.app)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from nexus_search.core import api
        importlib.reload(api)

    def test_exceeding_limit_is_429_with_retry_after(self):
        for i in range(3):
            r = self.client.get("/search", params={"q": "x"})
            self.assertEqual(r.status_code, 200, f"request {i} should pass")
        r = self.client.get("/search", params={"q": "x"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("retry-after", {k.lower() for k in r.headers.keys()})

        # un-limited endpoints still work for the same client
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_writes_are_limited_too(self):
        for i in range(3):
            r = self.client.post("/documents", json={"doc_id": f"d{i}", "content": "c"})
            self.assertEqual(r.status_code, 201)
        r = self.client.post("/documents", json={"doc_id": "d3", "content": "c"})
        self.assertEqual(r.status_code, 429)

    def test_limit_is_scoped_per_api_key(self):
        # clients presenting an API key are bucketed per key; no header -> by IP
        for i in range(3):
            r = self.client.get("/search", params={"q": "x"},
                                headers={"X-API-Key": "client-a"})
            self.assertEqual(r.status_code, 200)
        r = self.client.get("/search", params={"q": "x"},
                            headers={"X-API-Key": "client-a"})
        self.assertEqual(r.status_code, 429)
        # a different key still has a fresh budget
        r = self.client.get("/search", params={"q": "x"},
                            headers={"X-API-Key": "client-b"})
        self.assertEqual(r.status_code, 200)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApi(unittest.TestCase):
    def setUp(self):
        os.environ["NEXUS_ENV"] = "dev"
        os.environ.pop("NEXUS_API_KEY", None)
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

    def test_oversized_metadata_rejected(self):
        big = "x" * 120_000
        r = self.add("big", "content", metadata={"blob": big})
        self.assertIn(r.status_code, (400, 422))
        # bulk path validates nested docs too
        rb = self.client.post("/documents/bulk", json={"documents": [
            {"doc_id": f"m{i}", "content": "c",
             "metadata": {"blob": big if i == 0 else ""}} for i in range(3)]})
        self.assertIn(rb.status_code, (400, 422))
        # right at the bound is fine
        ok = self.client.post("/documents", json={"doc_id": "ok", "content": "c",
                                                  "metadata": {"k": "v"}})
        self.assertEqual(ok.status_code, 201)

    def test_delete(self):
        self.add("a", "alpha")
        self.assertEqual(self.client.delete("/documents/a").status_code, 200)
        self.assertEqual(self.client.delete("/documents/a").status_code, 404)
        self.assertEqual(self.client.get("/search", params={"q": "alpha"}).json()["total_results"], 0)

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")

    def test_misspelled_query_is_corrected_by_default(self):
        # query understanding is wired into retrieval by default: an OOV
        # typo ("pythn") resolves to the in-vocabulary term via `python` —
        # keyword mode would find NOTHING pre-wiring, now it does
        self.add("pydoc", "python programming language tutorial")
        self.add("other", "unrelated cooking content")
        body = self.client.get("/search", params={"q": "pythn"}).json()
        self.assertEqual(body["results"][0]["doc_id"], "pydoc")
        self.assertGreaterEqual(body["total_results"], 1)
        # keyword mode gets the same correction
        kw = self.client.get("/search", params={"q": "pythn", "mode": "keyword"}).json()
        self.assertEqual(kw["results"][0]["doc_id"], "pydoc")

    def test_boolean_structure_survives_understanding(self):
        # rewriting must not drop NOT semantics (structure guard)
        self.add("a", "python programming language")
        self.add("b", "python snake habitat")
        body = self.client.get("/search", params={"q": "python NOT snake"}).json()
        self.assertEqual({r["doc_id"] for r in body["results"]}, {"a"})

    def test_synonym_expansion_widens_recall(self):
        self.add("car1", "automobile maintenance guide")
        body = self.client.get("/search", params={"q": "car"}).json()
        self.assertEqual(body["results"][0]["doc_id"], "car1")  # via synonym

    def test_ready(self):
        self.assertEqual(self.client.get("/ready").json()["ready"], True)

    def test_get_document_by_id(self):
        self.add("a1", "alpha beta", title="First")
        r = self.client.get("/documents/a1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["content"], "alpha beta")
        self.assertEqual(self.client.get("/documents/nope").status_code, 404)

    def test_bulk_index_and_partial_failure(self):
        r = self.client.post("/documents/bulk", json={
            "documents": [{"doc_id": "b1", "content": "bulk one vector"},
                          {"doc_id": "b2", "content": "bulk two vector"}]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["indexed"], body["failed"], body["total"]), (2, [], 2))
        # wrote for real: both searchable
        self.assertEqual(self.client.get("/search", params={"q": "vector"}).json()["total_results"], 2)

    def test_bulk_empty_is_422_and_cap_enforced(self):
        self.assertEqual(self.client.post("/documents/bulk", json={"documents": []}).status_code, 422)

    def test_query_length_cap(self):
        r = self.client.get("/search", params={"q": "x" * 2001})
        self.assertEqual(r.status_code, 400)

    def test_diversity_param_works_and_conflicts(self):
        bodies = [
            "alpha programming language tutorial",
            "alpha dog training basics",
            "alpha centauri star system",
            "alpha finance investment strategy",
            "alpha antenna radio design",
            "alpha cooking recipes pasta",
        ]
        for i, body in enumerate(bodies):
            self.add(f"m{i}", body, title=f"alpha topic {i}")
        r = self.client.get("/search", params={"q": "alpha", "diversity": 0.7, "top_k": 3})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["results"]), 3)
        # diversity and non-relevance sort are mutually exclusive
        r2 = self.client.get("/search",
                             params={"q": "alpha", "diversity": 0.5, "sort": "freshness"})
        self.assertEqual(r2.status_code, 400)

    def test_diversity_changes_ordering_on_near_duplicates(self):
        # near-mirror docs: without diversification they flood the top of the
        # page; with diversity the MMR anti-flood gate must change the outcome
        # 11 shared tokens, one differing token each => Jaccard ~0.92 between
        # dupes; query terms must exist in the docs (an OOV query term gets
        # spell-corrected toward whatever the vocabulary has — fixture design)
        near_dupes = ["the quick brown fox jumps over the lazy dog every morning variant one",
                      "the quick brown fox jumps over the lazy dog every morning variant two",
                      "the quick brown fox jumps over the lazy dog every morning variant three"]
        for i, body in enumerate(near_dupes + ["a completely unrelated fox weather report about rain"]):
            self.add(f"nd{i}", body, title=f"doc {i}")
        plain = self.client.get("/search", params={"q": "fox dog", "diversity": 0.0}).json()
        diverse = self.client.get("/search", params={"q": "fox dog", "diversity": 1.0}).json()
        plain_ids = [r["doc_id"] for r in plain["results"]]
        diverse_ids = [r["doc_id"] for r in diverse["results"]]
        # the diversified page must differ: the unrelated document gets
        # promoted over the duplicate family's followers (MMR penalty)
        self.assertNotEqual(plain_ids, diverse_ids)
        self.assertIn("nd3", diverse_ids[:2])      # novelty wins a top slot
        self.assertNotIn("nd3", plain_ids[:2])     # but not in plain relevance order

    def test_facets_truncated_flag(self):
        # facet pass samples at most 500 docs; beyond that the response must
        # SAY the counts are partial instead of silently lying
        for batch_start in range(0, 520, 130):
            self.client.post("/documents/bulk", json={"documents": [
                {"doc_id": f"f{i}", "content": "facetprobe", "doc_type": "t",
                 "metadata": {"language": "en"}}
                for i in range(batch_start, batch_start + 130)]})
        big = self.client.get("/search", params={"q": "facetprobe", "facets": "doc_type"}).json()
        self.assertTrue(big["metadata"]["facets_truncated"])
        small = self.client.get("/search", params={"q": "facetprobe", "top_k": 5,
                                                   "facets": "doc_type"})
        self.assertEqual(small.json()["facets"]["doc_type"]["t"], 500)  # sample cap
        small2 = self.client.get("/search", params={"q": "facetprobe type:t",
                                                    "facets": "doc_type"})
        self.assertTrue(small2.json()["metadata"]["facets_truncated"])  # still >500 matches

    def test_hybrid_total_is_true_match_count(self):
        # 30 docs match "common"; candidate pool is tiny — total must still
        # report the corpus-wide match count, and has_more must reflect the
        # pageable pool, not total.
        for i in range(30):
            self.add(f"d{i}", "common word")
        r = self.client.get("/search", params={"q": "common", "mode": "hybrid",
                                               "top_k": 10, "candidates": 5})
        body = r.json()
        self.assertEqual(body["total_results"], 30)
        self.assertEqual(body["metadata"]["merged_candidates"] <= 10, True)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApiExposure(unittest.TestCase):
    """Prod/dev surface: docs hidden in production, CORS opt-in, /metrics gated."""

    KEYS = ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_CORS_ORIGINS", "NEXUS_DB")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from nexus_search.core import api
        importlib.reload(api)

    def _reload(self, env, key, cors=None):
        os.environ["NEXUS_ENV"] = env
        if key:
            os.environ["NEXUS_API_KEY"] = key
        else:
            os.environ.pop("NEXUS_API_KEY", None)
        if cors is None:
            os.environ.pop("NEXUS_CORS_ORIGINS", None)
        else:
            os.environ["NEXUS_CORS_ORIGINS"] = cors
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "api.db")
        from nexus_search.core import api
        importlib.reload(api)
        return TestClient(api.app)

    def test_docs_hidden_in_production(self):
        client = self._reload("production", "secret")
        self.assertEqual(client.get("/docs").status_code, 404)
        self.assertEqual(client.get("/openapi.json").status_code, 404)

    def test_docs_visible_in_dev(self):
        client = self._reload("dev", None)
        self.assertEqual(client.get("/docs").status_code, 200)

    def test_metrics_gated_by_key(self):
        client = self._reload("production", "secret")
        self.assertEqual(client.get("/metrics").status_code, 401)
        self.assertEqual(client.get("/metrics", headers={"X-API-Key": "secret"}).status_code, 200)

    def test_explain_gated_by_key(self):
        client = self._reload("production", "secret")
        self.assertEqual(client.post("/search/explain",
                                     json={"query": "x"}).status_code, 401)
        self.assertEqual(client.post("/search/explain", headers={"X-API-Key": "secret"},
                                     json={"query": "x"}).status_code, 200)

    def test_cors_opt_in(self):
        client = self._reload("dev", None, cors="https://app.example")
        r = client.options("/search", headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "GET"})
        self.assertEqual(r.headers.get("access-control-allow-origin"), "https://app.example")
        noset = self._reload("dev", None)
        r2 = noset.get("/health", headers={"Origin": "https://app.example"})
        self.assertNotIn("access-control-allow-origin",
                         {k.lower() for k in r2.headers.keys()})

    def test_reads_open_by_default_with_key_set(self):
        # with the flag unset reads stay open even though writes require a key
        client = self._reload("production", "secret")
        self.assertEqual(client.get("/search", params={"q": "x"}).status_code, 200)
        self.assertEqual(client.get("/suggest", params={"q": "x"}).status_code, 200)

    def test_reads_gated_when_flag_set(self):
        saved = os.environ.get("NEXUS_REQUIRE_AUTH_FOR_READS")
        os.environ["NEXUS_REQUIRE_AUTH_FOR_READS"] = "1"
        try:
            client = self._reload("production", "secret")
            self.assertEqual(client.get("/search", params={"q": "x"}).status_code, 401)
            self.assertEqual(
                client.get("/search", params={"q": "x"},
                           headers={"X-API-Key": "secret"}).status_code, 200)
        finally:
            if saved is None:
                os.environ.pop("NEXUS_REQUIRE_AUTH_FOR_READS", None)
            else:
                os.environ["NEXUS_REQUIRE_AUTH_FOR_READS"] = saved

    def test_storage_error_not_leaked(self):
        client = self._reload("dev", None)
        client.post("/documents", json={"doc_id": "x", "content": "c"})
        # corrupt the documents table name away, then write: the 500 body must
        # not carry the sqlite error text (paths/SQL) to the client
        api_mod = __import__("nexus_search.core.api", fromlist=["api"])
        api_mod._storage.conn.execute("ALTER TABLE documents RENAME TO documents_gone")
        api_mod._storage.conn.commit()
        try:
            r = client.post("/documents", json={"doc_id": "y", "content": "c"})
            self.assertEqual(r.status_code, 500)
            self.assertEqual(r.json()["detail"], "storage error")
        finally:
            pass  # each test gets a fresh DB+module anyway


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApiAuth(unittest.TestCase):
    """API-key auth on write endpoints (NEXUS_API_KEY). Real HTTP layer."""

    def setUp(self):
        import importlib
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "auth.db")
        os.environ["NEXUS_API_KEY"] = "test-secret-key"
        from nexus_search.core import api
        importlib.reload(api)
        self.client = TestClient(api.app)

    def tearDown(self):
        import importlib
        os.environ.pop("NEXUS_API_KEY", None)
        from nexus_search.core import api
        importlib.reload(api)  # restore open/default state for other tests

    def test_write_without_key_is_401(self):
        self.assertEqual(self.client.post("/documents", json={"doc_id": "x", "content": "c"}).status_code, 401)

    def test_write_with_wrong_key_is_401(self):
        r = self.client.post("/documents", json={"doc_id": "x", "content": "c"},
                             headers={"X-API-Key": "wrong"})
        self.assertEqual(r.status_code, 401)

    def test_write_with_key_is_201(self):
        r = self.client.post("/documents", json={"doc_id": "x", "content": "c"},
                             headers={"X-API-Key": "test-secret-key"})
        self.assertEqual(r.status_code, 201)

    def test_delete_requires_key(self):
        self.client.post("/documents", json={"doc_id": "x", "content": "c"},
                         headers={"X-API-Key": "test-secret-key"})
        self.assertEqual(self.client.delete("/documents/x").status_code, 401)
        self.assertEqual(self.client.delete("/documents/x",
                                            headers={"X-API-Key": "test-secret-key"}).status_code, 200)

    def test_reads_stay_open_without_key(self):
        self.client.post("/documents", json={"doc_id": "x", "content": "alpha"},
                         headers={"X-API-Key": "test-secret-key"})
        self.assertEqual(self.client.get("/search", params={"q": "alpha"}).status_code, 200)
        self.assertEqual(self.client.get("/health").status_code, 200)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApiPhase5Ux(unittest.TestCase):
    """Phase 5 Stage 4 API surface: /suggest, /related, facets, sort, highlight,
    cursor pagination, has_more. Real HTTP layer (TestClient), not internals."""

    def setUp(self):
        os.environ["NEXUS_ENV"] = "dev"
        os.environ.pop("NEXUS_API_KEY", None)
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