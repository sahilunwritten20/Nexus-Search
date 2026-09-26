"""Evaluation stack: benchmark round-trip + dataset sanity."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.evaluation.benchmark import run_benchmark
from nexus_search.evaluation.dataset import create_benchmark_dataset
from nexus_search.evaluation.metrics import evaluate_all


class TestBenchmark(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_dataset_has_documents_and_judgments(self):
        docs, queries = create_benchmark_dataset()
        self.assertGreaterEqual(len(docs), 5)
        self.assertGreaterEqual(len(queries), 3)
        for q in queries:
            self.assertTrue(q.relevant_docs)
            for doc_id, rel in q.relevant_docs.items():
                self.assertIn(doc_id, {d.doc_id for d in docs})
                self.assertIn(rel, (0, 1, 2, 3))

    def test_benchmark_runs_all_modes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            comparison = run_benchmark(os.path.join(self.dir, "bench.db"))
        self.assertEqual(set(comparison), {"keyword", "semantic", "hybrid"})
        for mode, report in comparison.items():
            agg = report["aggregate"]
            for key in ("precision@1", "recall@10", "mrr", "ndcg@10", "latency_mean"):
                self.assertIn(key, agg)
            # deterministic dataset + hash embedder: keyword mode must find
            # the judged docs for at least one query perfectly at k=10
            self.assertGreaterEqual(agg["recall@10"], 0.5)


if __name__ == "__main__":
    unittest.main()
