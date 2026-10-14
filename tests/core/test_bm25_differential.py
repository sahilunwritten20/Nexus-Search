"""Differential test: BM25Search vs an INDEPENDENT hand-derived
reference implementation (mission B1).

The system under test scores via SQLite postings + the production code
path (storage -> BM25Search.search_page). The reference is written from
the DOCUMENTED formula only:

    idf(t)      = ln(1 + (N - n_t + 0.5) / (n_t + 0.5))      [Lucene-style,
                                                                 always > 0]
    score(t, d) = idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len_d / avg_len))
    k1 = 1.5, b = 0.75

System semantics that the reference must mirror (verified by this test):
- duplicate query terms count ONCE (the system iterates a term set);
- docs whose title contains none of the query terms get title multiplier
  exactly 1.0 (TITLE_BOOST path inactive);
- no phrases in the query -> no PHRASE_BOOST;
- tie-break by doc_id ascending.

Scores must match to 1e-9 over random corpora (deterministic via
hypothesis derandomize + seeded backstop cases), and the full ranking
order must be identical.
"""
import math
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus_search.core.bm25 import BM25Search, B, K1
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage

import os
import tempfile


def _reference_bm25(corpus_tokens, query_terms, k1=K1, b=B):
    corpus_tokens = [list(t) for t in corpus_tokens]
    n_docs = len(corpus_tokens)
    doc_tokens = corpus_tokens
    lengths = [len(t) for t in doc_tokens]
    avg = sum(lengths) / n_docs if n_docs else 0.0
    df = {}
    for toks in doc_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    scores = {}
    for term in set(query_terms):  # duplicate terms count once
        n_t = df.get(term, 0)
        if n_t == 0:
            continue
        idf = math.log(1 + (n_docs - n_t + 0.5) / (n_t + 0.5))
        for i, toks in enumerate(doc_tokens):
            tf = toks.count(term)
            if tf == 0:
                continue
            norm = 1 - b + b * (lengths[i] / avg if avg else 1)
            scores[i] = scores.get(i, 0.0) + idf * (tf * (k1 + 1)) / (tf + k1 * norm)
    return scores


def _build(storage, indexer, docs):
    for doc_id, title, content in docs:
        indexer.add_document(doc_id, content, title=title)


class TestBM25Differential(unittest.TestCase):
    def _assert_page_matches_reference(self, docs, query, top_k=None):
        """docs: [(doc_id, title, content)]; query: raw query string.

        The system indexes tokenize(f"{title} {content}") — the doc
        LENGTH and the term frequencies both span title+content (pinned
        by the golden corpus) — so the reference models exactly that.

        A FRESH DB per call: hypothesis reuses the test instance across
        examples, so instance-level setUp state would leak docs between
        examples (found live — the differential caught its own harness
        bug before the system's)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            storage = Storage(path)
            indexer = Indexer(storage)
            searcher = BM25Search(storage)
            for doc_id, title, content in docs:
                indexer.add_document(doc_id, content, title=title)
            query_terms = query.split()
            full_texts = [f"{t} {c}".split() for _, t, c in docs]
            reference = _reference_bm25(full_texts, query_terms)
            ref_by_doc = {docs[i][0]: s for i, s in reference.items()}
            page = searcher.search_page(query,
                                        top_k=top_k or max(1, len(docs)),
                                        group_chunks=False)
            got = {r.doc_id: r.score for r in page.results}
            for doc_id, ref_score in ref_by_doc.items():
                self.assertIn(doc_id, got)
                self.assertAlmostEqual(got[doc_id], ref_score, places=9,
                                       msg=f"score mismatch for {doc_id} on {query!r}")
            self.assertEqual(set(got), set(ref_by_doc))
            ref_order = sorted(ref_by_doc, key=lambda d: (-ref_by_doc[d], d))
            self.assertEqual([r.doc_id for r in page.results], ref_order)
        finally:
            storage.close()
            try:
                os.remove(path)
            except PermissionError:
                pass

    def test_single_term_scores_match_reference(self):
        docs = [
            ("d1", "unrelated title", "alpha beta beta gamma"),
            ("d2", "", "alpha"),
            ("d3", "", "alpha alpha alpha delta epsilon"),
            ("d4", "", "zeta eta theta"),
        ]
        self._assert_page_matches_reference(docs, "alpha")

    def test_multi_term_scores_match_reference(self):
        docs = [
            ("a", "", "alpha beta gamma"),
            ("b", "", "beta beta alpha alpha alpha"),
            ("c", "", "gamma delta"),
            ("d", "", "beta"),
            ("e", "", "epsilon zeta beta"),
        ]
        self._assert_page_matches_reference(docs, "alpha beta")

    def test_duplicate_query_terms_count_once(self):
        docs = [("x1", "", "dup dup dup"), ("x2", "", "dup")]
        self._assert_page_matches_reference(docs, "dup dup dup dup")

    def test_all_docs_match_term_still_positive(self):
        # Lucene-style idf stays positive even when n_t == N
        docs = [("p1", "", "every"), ("p2", "", "every"), ("p3", "", "every")]
        self._assert_page_matches_reference(docs, "every")
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            storage = Storage(path)
            indexer = Indexer(storage)
            for doc_id, _, content in docs:
                indexer.add_document(doc_id, content)
            page = BM25Search(storage).search_page(
                "every", top_k=10, group_chunks=False)
            self.assertTrue(all(r.score > 0 for r in page.results),
                            "all-docs-match term must not produce negative idf")
        finally:
            storage.close()
            os.remove(path)

    def test_length_normalization_matches_reference(self):
        # one very long doc, one short: b=0.75 must bite exactly as written
        docs = [("long", "", " ".join(f"w{i % 7}" for i in range(400))),
                ("short", "", "w1"),
                ("mid", "", "w1 w2 w3")]
        self._assert_page_matches_reference(docs, "w1")

    def test_title_terms_absent_from_query_yield_identity_boost(self):
        # title boost must be exactly 1.0 when no query term hits the title
        docs = [("t1", "completely different title words", "target content here")]
        self._assert_page_matches_reference(docs, "target content")

    @settings(derandomize=True, max_examples=40, deadline=None)
    @given(
        n_docs=st.integers(min_value=1, max_value=25),
        vocab=st.integers(min_value=3, max_value=12),
        seed=st.integers(min_value=0, max_value=10_000),
    )
    def test_random_corpora_scores_match(self, n_docs, vocab, seed):
        import random
        rng = random.Random(seed)
        words = [f"w{i}" for i in range(vocab)]
        docs = []
        for i in range(n_docs):
            content = " ".join(rng.choice(words)
                               for _ in range(rng.randint(1, 30)))
            # titles deliberately avoid the vocabulary so the title boost
            # stays an identity multiplier in this differential
            docs.append((f"doc{i:03d}", f"Title {i} of the corpus", content))
        query_terms = rng.sample(words, k=min(3, vocab))
        self._assert_page_matches_reference(docs, " ".join(query_terms))


if __name__ == "__main__":
    unittest.main()
