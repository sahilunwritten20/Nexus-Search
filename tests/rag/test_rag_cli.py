"""CLI coverage, IN-PROCESS (review COVERAGE-GATE): subprocess runs
don't count toward coverage, so the rag debug CLI is executed via
main(argv) directly — same entry point, captured stdout.
"""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage

import nexus_search.rag.__main__ as rag_cli


class TestRagCliInProcess(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ragcli-")
        self.db = os.path.join(self.dir, "cli.db")
        storage = Storage(self.db)
        indexer = Indexer(storage)
        indexer.add_document(
            "bm25-doc", "BM25 scores lexical matches over an inverted "
            "index. Term frequency and inverse document frequency drive "
            "the ranking function. BM25 lexical ranking uses the "
            "inverted index and term frequency.", title="BM25")
        for i, filler in enumerate((
                "Color theory discusses hue saturation lightness",
                "Sourdough baking requires hydration starter culture",
                "Cartography spans centuries of mapmaking traditions")):
            indexer.add_document(f"filler-{i}", filler, title=f"Filler {i}")
        storage.close()
        self._saved_env = {k: os.environ.get(k) for k in (
            "NEXUS_ENV", "NEXUS_DB", "NEXUS_EMBEDDER",
            "NEXUS_LLM_PROVIDER", "NEXUS_RAG_ALLOW_STUB",
            "NEXUS_RATE_LIMIT", "NEXUS_RAG_DAILY_TOKEN_BUDGET")}
        os.environ.update({
            "NEXUS_ENV": "dev",
            "NEXUS_DB": self.db,
            "NEXUS_EMBEDDER": "hash:384",
            "NEXUS_LLM_PROVIDER": "stub",
            "NEXUS_RAG_ALLOW_STUB": "1",
            "NEXUS_RATE_LIMIT": "0",
            "NEXUS_RAG_DAILY_TOKEN_BUDGET": "0",
        })

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _run(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = rag_cli.main(list(args))
        return rc, out.getvalue()

    def test_ask_answers_with_citations_gate_and_spend(self):
        rc, out = self._run("ask", "bm25 lexical inverted index ranking",
                            "--top-k", "3")
        self.assertEqual(rc, 0)
        self.assertIn("answerable : True", out)
        self.assertIn("[1]", out)          # citation rendered
        self.assertIn("gate       :", out)
        self.assertIn("hash:384", out)
        self.assertIn("spend      : tokens_in=", out)
        self.assertIn("sources    :", out)

    def test_ask_refuses_unanswerable_with_gate_signals(self):
        rc, out = self._run("ask", "best pasta carbonara recipe with "
                                   "guanciale")
        self.assertEqual(rc, 0)
        self.assertIn("answerable : False", out)
        self.assertIn("refusal    :", out)
        self.assertIn("no_evidence", out)  # gate reason surfaces

    def test_ask_reports_degraded_reason_when_vector_down(self):
        # a DB whose vector subsystem cannot boot -> the CLI degrades
        # GRACEFULLY (CLI-DEGRADED fix: it used to crash with
        # RuntimeError: no retriever available). The gate then runs on
        # the conservative unknown-embedder floors — an honest refusal
        # is a safe outcome here; a crash is not.
        from nexus_search.core.embedders import reset_embedder
        reset_embedder()  # the singleton would otherwise ignore the env
        os.environ["NEXUS_EMBEDDER"] = "st:definitely-not-loadable-model"
        rc, out = self._run("ask", "bm25 lexical inverted index ranking")
        self.assertEqual(rc, 0)
        self.assertIn("keyword-only degraded retrieval", out)
        self.assertIn("answerable :", out)
        self.assertIn("gate       :", out)
        self.assertIn("embedder=None", out)  # honest: no calibrated floors

    def test_calibrate_prints_floors_and_test_split(self):
        rc, out = self._run("calibrate")
        self.assertEqual(rc, 0)
        self.assertIn("bm25_floor", out)
        self.assertIn("junk_refusal_rate", out)
        self.assertIn("well_evidenced_false_refusal_rate", out)

    def test_provider_fallback_warning_printed_when_absent(self):
        # stub outside dev without opt-in -> the CLI says so and falls
        # back to the offline stub FOR DEBUGGING ONLY
        os.environ["NEXUS_ENV"] = "production"
        os.environ.pop("NEXUS_RAG_ALLOW_STUB", None)
        rc, out = self._run("ask", "bm25 lexical inverted index ranking")
        self.assertEqual(rc, 0)
        self.assertIn("FOR DEBUGGING ONLY", out)


if __name__ == "__main__":
    unittest.main()
