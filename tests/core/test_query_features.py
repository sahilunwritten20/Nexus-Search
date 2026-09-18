import os
import tempfile
import unittest

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.core.query_parser import parse_query


class TestQueryFeatures(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.search = BM25Search(self.storage)

    def tearDown(self):
        self.storage.close()
        os.remove(self.path)

    def test_parser_supports_phrases_and_filters(self):
        q = parse_query('"machine learning" type:pdf lang:en')
        self.assertEqual(q.phrases, ['machine learning'])
        self.assertEqual(q.filters, {'doc_type': 'pdf', 'language': 'en'})

    def test_title_match_gets_boost(self):
        self.indexer.add_document('d1', 'general information', title='Python')
        self.indexer.add_document('d2', 'python information', title='General')
        results = self.search.search('python')
        self.assertEqual(results[0].doc_id, 'd1')

    def test_phrase_match_gets_boost(self):
        self.indexer.add_document('d1', 'machine learning is useful')
        self.indexer.add_document('d2', 'machine systems and learning systems')
        results = self.search.search('"machine learning"')
        self.assertEqual(results[0].doc_id, 'd1')

    def test_type_and_language_filters(self):
        self.indexer.add_document('d1', 'python guide', doc_type='pdf', metadata={'language': 'en'})
        self.indexer.add_document('d2', 'python guide', doc_type='web', metadata={'language': 'en'})
        self.indexer.add_document('d3', 'python guide', doc_type='pdf', metadata={'language': 'fr'})
        results = self.search.search('python type:pdf lang:en')
        self.assertEqual([r.doc_id for r in results], ['d1'])

    def test_top_k_non_positive_returns_empty(self):
        self.indexer.add_document('d1', 'python')
        self.assertEqual(self.search.search('python', top_k=0), [])


if __name__ == '__main__':
    unittest.main()
