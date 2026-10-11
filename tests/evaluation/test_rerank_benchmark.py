"""WP14-6c — smoke test for evaluation/rerank_benchmark.py.

Step 0 finding H7: the module ran fine (exit 0) but had ZERO test coverage
and no references anywhere. Deleting it would lose the A/B quick-check the
Phase 5 framework exists for, so instead: this test pins that it RUNS,
refuses on <100% vector coverage, and returns the documented metric shape
(it reuses evaluation/benchmark.py's machinery, so this also guards the
import seam the module holds).
"""
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")


class TestRerankBenchmarkRuns(unittest.TestCase):
    def test_run_returns_comparison_metrics(self):
        from nexus_search.evaluation.rerank_benchmark import run_rerank_benchmark
        db = os.path.join(tempfile.mkdtemp(prefix="wp14_rrb_"), "rrb.db")
        out = run_rerank_benchmark(db_path=db)
        self.assertIsInstance(out, dict)
        self.assertNotIn("error", out, "benchmark must reach 100% coverage")
        for mode in ("hybrid", "hybrid+rerank"):
            self.assertIn(mode, out, out.keys())
            metrics = out[mode]["aggregate"]
            for key in ("precision@1", "precision@10", "recall@10", "mrr", "ndcg@10"):
                self.assertIn(key, metrics)


if __name__ == "__main__":
    unittest.main()
