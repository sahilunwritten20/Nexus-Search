"""P7-5 — citations: parse, verify against THIS request's sources through
the existing Storage path, drop-and-flag unverifiable ones, refuse
all-failed answers, plus the deterministic support/quote checks."""
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.rag.citations import (
    SUPPORT_THRESHOLD, check_quotes, check_support, parse_citations,
    sentence_support, verify_citations,
)
from nexus_search.rag.context import select_context
from nexus_search.rag.retrieval import RagChunk

CHUNK_TEXT = ("BM25 scores lexical matches over an inverted index using "
              "term frequency and inverse document frequency.")


def selection(*chunks):
    return select_context(list(chunks), max_context_tokens=10**6,
                          max_context_bytes=10**9, per_parent_cap=99,
                          dedup_threshold=1.1, stitch_neighbors=False)


def mkchunk(cid, text, index=0, parent=None, url=None):
    meta = {}
    if url:
        meta["url"] = url
    return RagChunk(chunk_id=cid, parent_id=parent or cid, title=cid,
                     doc_type="text", metadata=meta, text=text,
                     chunk_index=index, chunk_count=1, url=url)


class TestParse(unittest.TestCase):
    def test_parse_dedupes_in_order(self):
        self.assertEqual(parse_citations("a [1] b [2] c [1] [3]"), [1, 2, 3])
        self.assertEqual(parse_citations("no citations here"), [])
        self.assertEqual(parse_citations("[1"), [])   # unbalanced
        self.assertEqual(parse_citations("[1234]"), [])  # >3 digits: not a marker


class _Sel:
    """Minimal selection stand-in exposing .sources with number/chunk_id/..."""
    pass


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.storage = Storage(os.path.join(self.tmp, "cit.db"))
        indexer = Indexer(self.storage)
        indexer.add_document("p#chunk0", CHUNK_TEXT, title="BM25 Basics",
                              doc_type="text",
                              metadata={"parent_id": "p", "url": "https://x/y"})
        indexer.add_document("p#chunk1", "Second chunk about tokenization.",
                              title="BM25 Basics",
                              metadata={"parent_id": "p"})
        chunks = [mkchunk("p#chunk0", CHUNK_TEXT, 0, "p", url="https://x/y"),
                  mkchunk("p#chunk1", "Second chunk about tokenization.", 1, "p")]
        self.selection = selection(*chunks)

    def tearDown(self):
        self.storage.close()

    def test_verified_citation_resolves_through_storage(self):
        v = verify_citations("BM25 ranks things [1].", self.selection,
                             self.storage)
        self.assertTrue(v.citations_verified)
        self.assertFalse(v.all_failed)
        self.assertEqual(len(v.citations), 1)
        c = v.citations[0]
        self.assertEqual((c.n, c.doc_id, c.parent_id, c.chunk_id),
                         (1, "p", "p", "p#chunk0"))
        self.assertEqual(c.title, "BM25 Basics")
        self.assertEqual(c.url, "https://x/y")
        self.assertIn("BM25", c.snippet)

    def test_unknown_number_is_dropped_and_flagged(self):
        v = verify_citations("Answer [1] and also [99].", self.selection,
                             self.storage)
        self.assertFalse(v.citations_verified)
        self.assertEqual(v.dropped, [{"n": 99, "reason": "unknown_source_number"}])
        self.assertEqual(len(v.citations), 1)  # [1] still verifies

    def test_every_citation_failing_refuses_outright(self):
        v = verify_citations("The truth is in [99] and [42].",
                             self.selection, self.storage)
        self.assertTrue(v.all_failed)
        self.assertEqual(v.citations, [])
        self.assertFalse(v.citations_verified)

    def test_row_deleted_mid_request_is_caught_fresh(self):
        # the batched Storage re-read means a deleted row cannot verify
        indexer = Indexer(self.storage)
        indexer.delete_document("p#chunk1")
        v = verify_citations("Claims [2].", self.selection, self.storage)
        self.assertFalse(v.citations_verified)
        self.assertEqual(v.dropped, [{"n": 2, "reason": "source_row_missing"}])
        self.assertTrue(v.all_failed)

    def test_no_citations_is_neutral(self):
        v = verify_citations("plain text", self.selection, self.storage)
        self.assertTrue(v.citations_verified)
        self.assertFalse(v.all_failed)
        self.assertEqual(v.citations, [])


class TestSupportAndQuotes(unittest.TestCase):
    def setUp(self):
        self.selection = selection(
            mkchunk("c1", "Paris is the capital of France and its largest city."))

    def test_verbatim_sentence_scores_high(self):
        score = sentence_support(
            "Paris is the capital of France.",
            ["Paris is the capital of France and its largest city."])
        self.assertGreaterEqual(score, SUPPORT_THRESHOLD)
        self.assertGreater(score, 0.9)

    def test_fabricated_sentence_scores_low(self):
        score = sentence_support(
            "Quantum flux capacitors power the warp drive.",
            ["Paris is the capital of France and its largest city."])
        self.assertLess(score, SUPPORT_THRESHOLD)

    def test_unsupported_claims_listed_with_reasons(self):
        answer = ("Paris is the capital of France [1]. "
                  "Quantum flux capacitors power the warp drive [1].")
        report = check_support(answer, self.selection)
        self.assertEqual(report.scored_sentences, 2)
        self.assertEqual(len(report.unsupported_claims), 1)
        claim = report.unsupported_claims[0]
        self.assertEqual(claim["reason"], "low_lexical_support")
        self.assertIn("warp", claim["sentence"])
        self.assertLess(claim["support_score"], SUPPORT_THRESHOLD)

    def test_uncited_sentences_counted_not_scored(self):
        report = check_support("A general opening statement. "
                               "Paris is the capital [1].", self.selection)
        self.assertEqual(report.uncited_sentences, 1)
        self.assertEqual(report.scored_sentences, 1)
        self.assertEqual(report.unsupported_claims, [])

    def test_verified_quote_carries_offsets(self):
        answer = ('As the source states, "the capital of France" [1], '
                  "which settles it.")
        checks = check_quotes(answer, self.selection)
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].verified)
        start, end = checks[0].answer_offset
        self.assertEqual(answer[start:end], "the capital of France")

    def test_failed_quote_lands_in_unsupported_claims(self):
        answer = 'According to [1], "quantum flux capacitors power the drive".'
        report = check_support(answer, self.selection)
        # BOTH detectors fire on this fabricated sentence: the sentence
        # has low lexical support AND its quoted span is absent
        reasons = {u["reason"] for u in report.unsupported_claims}
        self.assertEqual(reasons, {"low_lexical_support", "quote_not_in_source"})

    def test_support_uses_as_sent_text(self):
        # quote verification checks the text AS SENT (post-redaction
        # contract): a redacted prompt still verifies verbatim quotes
        sel = selection(mkchunk("c1", "email [REDACTED-EMAIL] wrote the spec"))
        answer = 'As the source states, "[REDACTED-EMAIL] wrote the spec" [1].'
        checks = check_quotes(answer, sel)
        self.assertTrue(checks[0].verified)


class TestThresholdSanity(unittest.TestCase):
    def test_threshold_sits_in_the_measured_gap(self):
        # the calibrated default: verbatim extractive >= ~0.9 passes,
        # fabricated < ~0.2 fails — 0.4 sits between (worklog records it)
        verbatim = sentence_support(
            "Term frequency and inverse document frequency drive ranking",
            ["BM25: term frequency and inverse document frequency drive "
             "the ranking function over an inverted index."])
        fabricated = sentence_support(
            "The moon is made of aged cheddar cheese",
            ["BM25: term frequency and inverse document frequency drive "
             "the ranking function over an inverted index."])
        self.assertGreater(verbatim, SUPPORT_THRESHOLD)
        self.assertLess(fabricated, SUPPORT_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
