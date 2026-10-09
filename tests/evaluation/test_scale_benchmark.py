"""WP13-2: the scale benchmark's selective-query scenario (reviewer B-item:
the original corpus put the query terms in EVERY document, so each query
matched 100% of the corpus and hybrid latency grew linearly by
construction — a worst case labelled as such, never a representative
number). This pins the scenario's construction at smoke size: the
Zipfian corpus is deterministic, the query's match set stays in the
1-5% band, and the query must return results in all three modes."""
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from nexus_search.evaluation.scale_benchmark import _build_selective_corpus  # noqa: E402


class TestSelectiveBenchmarkScenario(unittest.TestCase):
    N = 400  # smoke size: fast offline, still enough for the band check

    def test_selective_query_matches_1_to_5_percent(self):
        tmp = tempfile.mkdtemp()
        try:
            hybrid, query, frac = _build_selective_corpus(
                os.path.join(tmp, "sel.db"), self.N)
            try:
                self.assertGreaterEqual(frac, 0.01,
                                        f"query {query!r} matched {frac:.2%} "
                                        "of docs — below the 1% floor")
                self.assertLessEqual(frac, 0.05,
                                     f"query {query!r} matched {frac:.2%} "
                                     "of docs — above the 5% ceiling")
                # deterministic construction: same seed -> same query+frac
                self.assertIn("topic", query)
                # the selective query answers in all three modes
                for mode in (SearchMode.HYBRID, SearchMode.KEYWORD,
                              SearchMode.SEMANTIC):
                    with self.subTest(mode=mode):
                        page = hybrid.search_page(query, top_k=10, mode=mode)
                        self.assertTrue(page.results,
                                        f"{mode} returned nothing for {query!r}")
            finally:
                hybrid._bench_sync.close()
                hybrid.close()
                hybrid._bench_vs.close()
                hybrid._bench_storage.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_selective_corpus_is_deterministic(self):
        """Same seed -> same query and match fraction across builds (the
        benchmark's numbers must be comparable run-to-run)."""
        tmp = tempfile.mkdtemp()
        try:
            h1, q1, f1 = _build_selective_corpus(
                os.path.join(tmp, "a.db"), self.N)
            h1._bench_sync.close(); h1.close(); h1._bench_vs.close(); h1._bench_storage.close()
            h2, q2, f2 = _build_selective_corpus(
                os.path.join(tmp, "b.db"), self.N)
            h2._bench_sync.close(); h2.close(); h2._bench_vs.close(); h2._bench_storage.close()
            self.assertEqual((q1, f1), (q2, f2))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
