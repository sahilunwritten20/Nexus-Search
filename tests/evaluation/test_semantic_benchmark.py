"""WP10 benchmark regression floors.

The fixture is deterministic (SPLIT_SEED committed), so these numbers are
reproducible. Floors pin CURRENT measured behavior with a small margin —
they exist to catch silent quality regressions (an embedder wiring change,
a fusion math bug), not to gate future improvements.

CI-safe (hash embedder) floors always run; the st floors are gated behind
NEXUS_RUN_MODEL_TESTS=1 like the rest of the model suite.
"""
import os
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.evaluation.semantic_benchmark.runner import run_benchmark  # noqa: E402


def _bench():
    # built once per process; the fixture is ~330 docs on the hash embedder
    # (a couple of seconds) — the st arm only loads with the model present
    if _bench._cache is None:
        _bench._cache = run_benchmark(quiet=True)
    return _bench._cache


_bench._cache = None


class TestHashFloors(unittest.TestCase):
    """Offline (hash-embedder) quality floors — runs in default CI."""

    def test_keyword_floor(self):
        row = _bench()["systems"]["keyword"]
        self.assertGreaterEqual(row["ndcg@10"], 0.55,
                                f"keyword NDCG@10 regressed: {row}")

    def test_semantic_hash_floor(self):
        row = _bench()["systems"]["semantic-hash"]
        self.assertGreaterEqual(row["ndcg@10"], 0.65,
                                f"hash semantic NDCG@10 regressed: {row}")

    def test_exact_keyword_floor(self):
        exact = _bench()["per_category"]["keyword"]["exact"]["ndcg@10"]
        self.assertGreaterEqual(exact, 0.85, "exact-keyword handling regressed")

    def test_typo_hash_rescue_floor(self):
        # the hash embedder's char-trigram features recover typos — a real
        # behavior of the lexical control, pinned honestly
        typo = _bench()["per_category"]["semantic-hash"]["typo"]["ndcg@10"]
        self.assertGreaterEqual(typo, 0.40, "hash typo rescue regressed")

    def test_coverage_gate(self):
        self.assertNotIn("error", _bench(),
                         "vector coverage must be 100% on the fixture")


@unittest.skipIf((os.environ.get("NEXUS_RUN_MODEL_TESTS") or "").strip()
                 not in ("1", "true", "yes"),
                 "st floors need NEXUS_RUN_MODEL_TESTS=1 + the cached model")
class TestStFloors(unittest.TestCase):
    """Model-gated floors for the real sentence-transformers arm."""

    def test_st_available(self):
        self.assertTrue(_bench().get("st_available"),
                        "model tests enabled but the ST embedder did not load")

    def test_st_paraphrase_beats_hash(self):
        bench = _bench()
        st = bench["per_category"]["semantic-st"]["paraphrase"]["ndcg@10"]
        hsh = bench["per_category"]["semantic-hash"]["paraphrase"]["ndcg@10"]
        self.assertGreaterEqual(st, 0.60,
                                f"st paraphrase NDCG@10 regressed: {st}")
        self.assertGreater(st, hsh,
                           "the real semantic model must beat the lexical "
                           "control on paraphrase queries — investigate the "
                           "embedder wiring if this flips")


if __name__ == "__main__":
    unittest.main()
