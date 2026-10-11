"""P7-2 — context selection: budget invariants (property-style), per-parent
cap, near-duplicate suppression, neighbor stitching (budget-guarded, same
parent only, llm_allowed-respecting), stable numbering, llm_allowed
exclusion, honest drop reporting."""
import os
import random
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.rag.context import (
    ContextSelection, RagChunk, estimate_tokens, jaccard,
    make_storage_neighbor_fetcher, select_context,
)
from nexus_search.rag.egress import llm_allowed


def chunk(cid, text, parent=None, index=None, count=None, meta=None):
    return RagChunk(chunk_id=cid, parent_id=parent or cid,
                    chunk_index=index, chunk_count=count, title=cid,
                    doc_type="text", url=None, metadata=meta or {}, text=text)


class TestLLMAllowedEgress(unittest.TestCase):
    def test_false_excludes_true_and_missing_allow(self):
        self.assertFalse(llm_allowed({"llm_allowed": False}))
        self.assertTrue(llm_allowed({"llm_allowed": True}))
        self.assertTrue(llm_allowed({}))
        self.assertTrue(llm_allowed(None))
        self.assertTrue(llm_allowed("junk"))

    def test_excluded_chunks_never_enter_the_selection(self):
        secret = chunk("s1", "classified answer text",
                       meta={"llm_allowed": False})
        ok = chunk("ok1", "public answer text")
        out = select_context([secret, ok], max_context_tokens=1000,
                             max_context_bytes=10000, stitch_neighbors=False)
        self.assertEqual([s.chunk_id for s in out.sources], ["ok1"])
        self.assertEqual(out.dropped, [{"chunk_id": "s1", "reason": "llm_allowed"}])
        for s in out.sources:
            self.assertNotIn("classified", s.text)


class TestBudgetInvariants(unittest.TestCase):
    def _random_chunks(self, rng, n):
        out = []
        for i in range(n):
            size = rng.choice([80, 200, 500, 1500])
            body = " ".join(rng.choices(
                ["alpha", "beta", "gamma", "delta", "unique"], k=size // 6))
            out.append(chunk(f"c{i}", body, parent=f"p{i % 4}",
                             index=i % 3, count=3))
        return out

    def test_budget_never_exceeded_property(self):
        rng = random.Random(1234)
        for trial in range(40):
            chunks = self._random_chunks(rng, rng.randint(1, 25))
            max_tokens = rng.choice([200, 600, 2000])
            max_bytes = rng.choice([1000, 4000, 20000])
            out = select_context(chunks, max_context_tokens=max_tokens,
                                max_context_bytes=max_bytes,
                                per_parent_cap=99, dedup_threshold=1.1,
                                stitch_neighbors=False)
            self.assertLessEqual(out.stats["context_tokens_estimated"],
                                 max_tokens, f"trial {trial}: token budget blown")
            self.assertLessEqual(out.stats["context_bytes"], max_bytes,
                                 f"trial {trial}: byte budget blown")
            # every input is accounted for exactly once
            accounted = {s.chunk_id for s in out.sources} | \
                        {d["chunk_id"] for d in out.dropped}
            self.assertEqual(accounted, {c.chunk_id for c in chunks},
                            f"trial {trial}: chunk accounting broken")

    def test_budget_is_greedy_rank_order_and_deterministic(self):
        chunks = [chunk(f"c{i}", f"body {i} " * 30) for i in range(10)]
        a = select_context(chunks, max_context_tokens=100,
                           max_context_bytes=10**9, stitch_neighbors=False)
        b = select_context(chunks, max_context_tokens=100,
                           max_context_bytes=10**9, stitch_neighbors=False)
        self.assertEqual([s.chunk_id for s in a.sources],
                         [s.chunk_id for s in b.sources])
        # greedy: the first chunks that fit are taken, later ones dropped
        self.assertEqual(a.sources[0].chunk_id, "c0")
        self.assertIn("budget_tokens", a.stats["drop_reasons"])
        for d in a.dropped:
            if d["reason"] == "budget_tokens":
                self.assertGreater(d["chunk_id"], a.sources[-1].chunk_id)

    def test_byte_budget_variants(self):
        chunks = [chunk(f"c{i}", "x" * 300) for i in range(4)]
        out = select_context(chunks, dedup_threshold=1.1,  # off: identical text
                             max_context_tokens=10**9,
                             max_context_bytes=1000, stitch_neighbors=False)
        self.assertEqual(len(out.sources), 3)  # 300*3 <= 1000 < 300*4
        self.assertIn("budget_bytes", out.stats["drop_reasons"])


class TestPerParentCapAndDedup(unittest.TestCase):
    def test_per_parent_cap(self):
        chunks = [chunk(f"p1#chunk{i}", f"distinct body {i} number {i}",
                        parent="p1", index=i, count=6) for i in range(6)]
        out = select_context(chunks, per_parent_cap=2, dedup_threshold=1.1,
                             max_context_tokens=10**6,
                             max_context_bytes=10**9, stitch_neighbors=False)
        self.assertEqual(len(out.sources), 2)
        self.assertEqual(len([d for d in out.dropped
                              if d["reason"] == "per_parent_cap"]), 4)

    def test_near_duplicate_suppression_jaccard(self):
        original = "the crawler follows links politely with a crawl delay"
        mirror = "the crawler follows links politely with a crawl delay period"
        distinct = "vector embeddings map text into dense cosine space"
        out = select_context(
            [chunk("a", original), chunk("b", mirror), chunk("c", distinct)],
            dedup_threshold=0.6, max_context_tokens=10**6,
            max_context_bytes=10**9, stitch_neighbors=False)
        self.assertEqual([s.chunk_id for s in out.sources], ["a", "c"])
        self.assertEqual(out.dropped,
                         [{"chunk_id": "b", "reason": "near_duplicate"}])

    def test_jaccard_zero_on_empty_sets(self):
        self.assertEqual(jaccard(frozenset(), frozenset("a")), 0.0)
        self.assertEqual(jaccard(frozenset(), frozenset()), 0.0)


class TestStableNumbering(unittest.TestCase):
    def test_numbers_are_1_to_n_in_rank_order(self):
        chunks = [chunk(f"c{i}", f"text {i}") for i in range(5)]
        out = select_context(chunks, stitch_neighbors=False,
                             max_context_tokens=10**6,
                             max_context_bytes=10**9)
        self.assertEqual([s.number for s in out.sources], [1, 2, 3, 4, 5])
        self.assertEqual(out.numbers_by_chunk_id,
                         {f"c{i}": i + 1 for i in range(5)})


class TestNeighborStitching(unittest.TestCase):
    DOC = {
        0: "zeroth part about sitemaps",
        1: "first part about sitemaps and urls",
        2: "second part about canonical addresses",
        3: "third part about robots",
    }

    def _fetch(self, parent, indexes):
        return {i: self.DOC[i] for i in indexes if i in self.DOC}

    def test_stitching_pulls_adjacent_chunks_same_parent(self):
        base = chunk("p#chunk1", self.DOC[1], parent="p", index=1, count=4)
        out = select_context(
            [base], stitch_neighbors=True, fetch_neighbors=self._fetch,
            max_context_tokens=10**6, max_context_bytes=10**9)
        self.assertEqual(len(out.sources), 1)
        src = out.sources[0]
        self.assertEqual(src.stitched_with, ["p#chunk0", "p#chunk2"])
        self.assertIn(self.DOC[0], src.text)
        self.assertIn(self.DOC[1], src.text)
        self.assertIn(self.DOC[2], src.text)
        self.assertNotIn(self.DOC[3], src.text)  # non-adjacent stays out
        # citation still maps to the selected chunk itself
        self.assertEqual(src.chunk_id, "p#chunk1")

    def test_stitching_respects_boundary_indexes(self):
        first = chunk("p#chunk0", self.DOC[0], parent="p", index=0, count=4)
        out = select_context(
            [first], stitch_neighbors=True, fetch_neighbors=self._fetch,
            max_context_tokens=10**6, max_context_bytes=10**9)
        self.assertEqual(out.sources[0].stitched_with, ["p#chunk1"])

    def test_stitching_is_budget_guarded(self):
        base = chunk("p#chunk1", self.DOC[1], parent="p", index=1, count=4)
        # a token budget that admits the chunk alone but not both neighbors
        alone = estimate_tokens(base.text, 4)
        out = select_context(
            [base], stitch_neighbors=True, fetch_neighbors=self._fetch,
            max_context_tokens=alone, max_context_bytes=10**9)
        self.assertEqual(out.sources[0].stitched_with, [])
        self.assertEqual(out.sources[0].text, base.text)
        self.assertLessEqual(out.stats["context_tokens_estimated"], alone)

    def test_stitched_neighbor_not_reselected(self):
        base = chunk("p#chunk1", self.DOC[1], parent="p", index=1, count=4)
        later = chunk("p#chunk0", self.DOC[0], parent="p", index=0, count=4)
        out = select_context(
            [base, later], stitch_neighbors=True, fetch_neighbors=self._fetch,
            max_context_tokens=10**6, max_context_bytes=10**9)
        self.assertEqual(len(out.sources), 1)
        self.assertEqual(out.dropped,
                         [{"chunk_id": "p#chunk0", "reason": "stitched_earlier"}])

    def test_stitching_disabled(self):
        base = chunk("p#chunk1", self.DOC[1], parent="p", index=1, count=4)
        out = select_context(
            [base], stitch_neighbors=False, fetch_neighbors=self._fetch,
            max_context_tokens=10**6, max_context_bytes=10**9)
        self.assertEqual(out.sources[0].text, base.text)
        self.assertEqual(out.sources[0].stitched_with, [])

    def test_non_chunk_rows_never_stitch(self):
        plain = chunk("plain", "just a plain row, no chunk metadata")
        out = select_context(
            [plain], stitch_neighbors=True, fetch_neighbors=self._fetch,
            max_context_tokens=10**6, max_context_bytes=10**9)
        self.assertEqual(out.sources[0].text, plain.text)


class TestProductionNeighborFetcher(unittest.TestCase):
    def test_fetcher_is_batched_and_egress_safe(self):
        import tempfile
        from nexus_search.core.indexer import Indexer
        from nexus_search.core.storage import Storage
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "nb.db")
        storage = Storage(db)
        try:
            indexer = Indexer(storage)
            indexer.add_document("p#chunk0", "public neighbor zero",
                                 metadata={"parent_id": "p", "chunk_index": 0,
                                           "chunk_count": 3})
            indexer.add_document("p#chunk2", "secret neighbor two",
                                 metadata={"parent_id": "p", "chunk_index": 2,
                                           "chunk_count": 3,
                                           "llm_allowed": False})
            calls = {"n": 0}
            real = storage.get_documents

            def counting(ids):
                calls["n"] += 1
                return real(ids)

            storage.get_documents = counting
            fetch = make_storage_neighbor_fetcher(storage)
            out = fetch("p", [0, 2])
            self.assertEqual(calls["n"], 1)  # ONE batched read
            self.assertEqual(out, {0: "public neighbor zero"})  # secret filtered
        finally:
            storage.close()

    def test_fetcher_missing_rows_absent(self):
        import tempfile
        from nexus_search.core.storage import Storage
        tmp = tempfile.mkdtemp()
        storage = Storage(os.path.join(tmp, "nb2.db"))
        try:
            fetch = make_storage_neighbor_fetcher(storage)
            self.assertEqual(fetch("ghost", [0, 1]), {})
        finally:
            storage.close()


class TestDropReporting(unittest.TestCase):
    def test_every_drop_has_a_reason_and_stats_summarize(self):
        chunks = [
            chunk("secret", "s", meta={"llm_allowed": False}),
            chunk("p1#chunk0", "duplicate body text alpha", parent="p1",
                 index=0, count=9),
            chunk("p1#chunk1", "duplicate body text alpha", parent="p1",
                 index=1, count=9),
            chunk("p1#chunk2", "another duplicate body text alpha", parent="p1",
                 index=2, count=9),
            chunk("p1#chunk3", "yet another duplicate body text alpha", parent="p1",
                 index=3, count=9),
            chunk("big", "x" * 5000),
        ]
        out = select_context(chunks, per_parent_cap=1, dedup_threshold=0.6,
                             max_context_tokens=10**6, max_context_bytes=10**9,
                             stitch_neighbors=False)
        self.assertIsInstance(out, ContextSelection)
        self.assertTrue(out.dropped)
        for d in out.dropped:
            self.assertIn("reason", d)
            self.assertIn(d["reason"],
                          ("llm_allowed", "per_parent_cap", "near_duplicate",
                           "budget_tokens", "budget_bytes"))
        self.assertEqual(out.stats["sources"], len(out.sources))
        self.assertEqual(out.stats["dropped"], len(out.dropped))
        self.assertEqual(sorted(out.stats["drop_reasons"]),
                         sorted({d["reason"] for d in out.dropped}))


if __name__ == "__main__":
    unittest.main()
