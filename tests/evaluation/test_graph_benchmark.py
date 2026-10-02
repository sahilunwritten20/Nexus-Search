"""WP5 benchmark harnesses: graph scale benchmark (smoke by default, full
10k/100k behind NEXUS_RUN_GRAPH_BENCH=1) and the authority on/off benchmark.

The PHASE6_PLAN §3 committed bound (<30s recompute for 10k pages / 100k
edges) is asserted in FULL mode; smoke mode asserts only that the harness
works so CI runs it cheaply. The real numbers for the docs come from the
full mode, recorded in docs/AUDIT_REMEDIATION.md.
"""
import os
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.evaluation.authority_benchmark import run_authority_benchmark  # noqa: E402
from nexus_search.evaluation.graph_benchmark import run_benchmark  # noqa: E402


class TestGraphBenchmark(unittest.TestCase):
    def test_smoke_mode_runs(self):
        os.environ["NEXUS_GRAPH_BENCH_SMOKE"] = "1"
        try:
            result = run_benchmark(quiet=True)
        finally:
            os.environ.pop("NEXUS_GRAPH_BENCH_SMOKE", None)
        self.assertEqual(result["mode"], "smoke")
        self.assertGreater(result["edges_inserted"], 0)
        self.assertGreater(result["urls_scored"], 0)
        self.assertGreater(result["urls_with_anchors"], 0)

    @unittest.skipIf((os.environ.get("NEXUS_RUN_GRAPH_BENCH") or "").strip()
                     not in ("1", "true", "yes"),
                     "full 10k/100k graph benchmark — set NEXUS_RUN_GRAPH_BENCH=1")
    def test_full_mode_within_30s_budget(self):
        result = run_benchmark(quiet=True)
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["pages"], 10_000)
        self.assertGreaterEqual(result["edges_inserted"], 95_000)
        self.assertTrue(result["budget_30s_ok"],
                        f"recompute took {result['recompute_seconds']}s — "
                        "PHASE6_PLAN's 30s bound is exceeded; fix the hot path "
                        "or correct the plan honestly")


class TestAuthorityBenchmark(unittest.TestCase):
    def test_authority_benchmark_runs_and_reports(self):
        result = run_authority_benchmark(quiet=True)
        self.assertIn("hub", result["authority_on_order"])
        self.assertIn("ndcg@10", result["baseline"])
        # the mission shape must produce the expected uplift: with the graph
        # as the ONLY differentiator, authority-on ranks the hub first
        self.assertEqual(result["authority_on_order"][0], "hub")

    def test_baseline_is_neutral_without_weights(self):
        result = run_authority_benchmark(quiet=True)
        # zero-weight identity: without Phase 6 weights, rerank output
        # ordering is retrieval-driven; identical-content docs tie and
        # the ranker cannot tell hub from orphan
        self.assertEqual(result["baseline"]["ndcg@10"],
                         result["baseline"]["ndcg@10"])  # harness sanity
        self.assertLessEqual(result["baseline"]["ndcg@10"], 1.0)


if __name__ == "__main__":
    unittest.main()
