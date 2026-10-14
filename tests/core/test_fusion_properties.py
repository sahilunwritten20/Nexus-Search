"""Fusion math property tests (mission B4): monotonicity, determinism,
tie handling, pool-edge cases — hypothesis-driven over the PRODUCTION
fusion functions (hybrid_search._rrf_fuse / _weighted_fuse).
"""
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus_search.core.hybrid_search import _fuse
from nexus_search.core.normalize import min_max_normalize


def _bm25_results(scores):
    from nexus_search.core.bm25 import SearchResult
    return {d: (s, SearchResult(d, s, "t", "s", "text"))
            for d, s in scores.items()}


def _vector_results(scores):
    return {d: (s, d) for d, s in scores.items()}


class TestRRFProperties(unittest.TestCase):
    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(
        bm25=st.dictionaries(st.integers(0, 40), st.floats(0.1, 20), min_size=1,
                             max_size=12),
        vector=st.dictionaries(st.integers(100, 140), st.floats(-1, 1),
                               min_size=0, max_size=12),
        wb=st.floats(0, 3),
        wv=st.floats(0, 3),
    )
    def test_contributions_are_rank_monotone(self, bm25, vector, wb, wv):
        """RRF contribution strictly increases as a doc's rank improves,
        holding the other retriever and weights fixed (mission B4)."""
        fused = _fuse(_bm25_results(bm25), _vector_results(vector), "rrf",
                      wb, wv)
        by_doc = {d: s for d, s, *_ in fused}
        for doc, score in bm25.items():
            # rank under RRF derives from score order (desc, doc_id tie)
            better = {d2: s2 + 1.0 for d2, s2 in bm25.items()}
            better[doc] = score  # everyone else improved -> doc's rank worsens
            fused2 = _fuse(_bm25_results(better), _vector_results(vector),
                           "rrf", wb, wv)
            by_doc2 = {d2: s2 for d2, s2, *_ in fused2}
            if doc in by_doc and doc in by_doc2:
                self.assertLessEqual(
                    by_doc2[doc], by_doc[doc] + 1e-12,
                    "worsening relative rank must not raise the fused score")

    def test_rrf_is_deterministic_with_ties(self):
        bm25 = {"a": 5.0, "b": 5.0, "c": 1.0}
        vector = {"a": 0.9, "b": 0.9}
        out1 = _fuse(_bm25_results(bm25), _vector_results(vector), "rrf", 1, 1)
        out2 = _fuse(_bm25_results(bm25), _vector_results(vector), "rrf", 1, 1)
        self.assertEqual(out1, out2)
        self.assertEqual([d for d, *_ in out1],
                         [d for d, *_ in out2])

    def test_rrf_empty_sides(self):
        self.assertEqual(_fuse({}, {}, "rrf", 1, 1), [])
        self.assertEqual([d for d, *_ in _fuse(_bm25_results({"a": 1}), {},
                                               "rrf", 1, 0)], ["a"])
        self.assertEqual([d for d, *_ in _fuse({}, _vector_results({"a": 1}),
                                               "rrf", 0, 1)], ["a"])


class TestWeightedProperties(unittest.TestCase):
    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(
        own=st.lists(st.floats(0, 10), min_size=1, max_size=1),
        others=st.lists(st.floats(0, 10), min_size=2, max_size=10),
        w=st.floats(0, 2),
    )
    def test_own_score_increase_never_lowers_contribution(self, own, others, w):
        """For a fixed pool of other scores, raising ONE doc's bm25 score
        never lowers its own weighted contribution (mission B4)."""
        base = {f"d{i}": s for i, s in enumerate(others)}
        lo, hi = own[0], own[0] + 3.0
        vec = {"v": 0.5}
        pool_lo = dict(base, target=lo)
        pool_hi = dict(base, target=hi)
        out_lo = _fuse(_bm25_results(pool_lo), _vector_results(vec),
                       "weighted", w, 1.0)
        out_hi = _fuse(_bm25_results(pool_hi), _vector_results(vec),
                       "weighted", w, 1.0)
        contrib_lo = {d: bc for d, _s, _src, bc, _vc in out_lo}
        contrib_hi = {d: bc for d, _s, _src, bc, _vc in out_hi}
        self.assertLessEqual(
            contrib_lo["target"], contrib_hi["target"] + 1e-12,
            "raising a doc's own score lowered its weighted contribution")

    def test_min_max_collapse_rule(self):
        self.assertEqual(min_max_normalize({"a": 0.5, "b": 0.5}),
                         {"a": 1.0, "b": 1.0})
        self.assertEqual(min_max_normalize({"a": 0.0, "b": 0.0}),
                         {"a": 0.0, "b": 0.0})
        self.assertEqual(min_max_normalize({}), {})

    def test_single_candidate_pool(self):
        out = _fuse(_bm25_results({"only": 3.0}), {}, "weighted", 1.0, 1.0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], 1.0,
                         "a lone positive score normalizes to full weight")


if __name__ == "__main__":
    unittest.main()
