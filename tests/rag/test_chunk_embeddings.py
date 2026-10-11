"""P7-1  chunk embeddings: every chunk row gets a vector via
EmbeddingSync; parent/chunk lifecycle (delete cascades, re-ingest
replaces) holds on the vector side too; coverage is reported honestly.

Decision recorded in docs/PHASE7_WORKLOG.md + README: retrieval is
CHUNK-ONLY  "Document Embeddings" in this system means every STORED row
(parent-when-unchunked, plus every `parent#chunkN` chunk row) receives an
embedding through the one EmbeddingSync; chunked parents have no parent
row at all (ingestion deletes it), so parent-level context reaches RAG
through chunk metadata (parent_id / chunk_index / chunk_count). There is
no separate parent-vector path.
"""
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.embedding_sync import EmbeddingSync
from nexus_search.core.storage import Storage
from nexus_search.core.vector_store import VectorStoreManager
from nexus_search.ingestion.pipeline import ingest_one
from nexus_search.ingestion.dedup import Deduplicator
from nexus_search.ingestion.types import IngestDoc


LONG_TEXT = ("Alpha retrieval payload about bm25 scoring and inverted indexes. "
             * 40)  # > chunk_size -> guaranteed multi-chunk


def _build(tmp_name="ragchunks.db"):
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, tmp_name)
    storage = Storage(db)
    indexer = Indexer(storage)
    vs = VectorStoreManager(db)
    sync = EmbeddingSync(vs, batch_size=1)
    sync.attach(indexer)
    return storage, indexer, vs, sync, db


class TestChunkEmbeddingCoverage(unittest.TestCase):
    def test_every_chunk_row_gets_a_vector_via_embedding_sync(self):
        storage, indexer, vs, sync, db = _build()
        try:
            status = ingest_one(
                IngestDoc(doc_id="doc-1", title="Chunked", content=LONG_TEXT,
                          doc_type="text", metadata={}),
                indexer, Deduplicator(db), chunk_size=500, sync=sync)
            self.assertEqual(status, "indexed")
            sync.flush()
            chunk_ids = storage.chunk_ids("doc-1")
            self.assertGreaterEqual(len(chunk_ids), 2)
            # the PARENT row is absent for chunked docs (ingestion removes
            # the previously-unchunked row)  chunks are the stored rows
            self.assertIsNone(storage.get_document("doc-1"))
            for cid in chunk_ids:
                self.assertIsNotNone(
                    vs.store.get_content_hash(cid),
                    f"chunk {cid} has no vector row")
            # a stored chunk row embeds to itself as top-1 (score ~ 1.0)
            first = sorted(chunk_ids)[0]
            doc = storage.get_document(first)
            hits = vs.search(f"{doc.title} {doc.content}", top_k=5)
            self.assertEqual(hits[0].doc_id, first)
            self.assertGreater(hits[0].score, 0.99)
        finally:
            sync.close(); vs.close(); storage.close()

    def test_vector_coverage_is_reported_honestly(self):
        storage, indexer, vs, sync, db = _build("cov.db")
        try:
            ingest_one(
                IngestDoc(doc_id="doc-1", title="Chunked", content=LONG_TEXT,
                          doc_type="text", metadata={}),
                indexer, Deduplicator(db), chunk_size=500, sync=sync)
            sync.flush()
            rows = 1 + len(storage.chunk_ids("doc-1"))  # + the doc below
            indexer.add_document("plain", content="a plain unchunked row")
            sync.flush()
            vectors = vs.get_stats()["count"]
            doc_count = storage.document_count()
            self.assertEqual(doc_count, rows)
            self.assertEqual(vectors, doc_count)
            self.assertEqual(vectors / doc_count, 1.0)  # full coverage
            # now smuggle a row PAST the indexer (no embedding event) ->
            # coverage must drop below 1.0 (never silently rounded up)
            storage.upsert_document_with_postings(
                "sneaky", "bypassed the indexer", "bypassed the indexer",
                "text", 3, {}, {"bypassed": 1})
            vectors2 = vs.get_stats()["count"]
            self.assertLess(vectors2 / storage.document_count(), 1.0)
        finally:
            sync.close(); vs.close(); storage.close()


class TestChunkVectorLifecycle(unittest.TestCase):
    def test_delete_parent_cascades_chunk_rows_and_vectors(self):
        storage, indexer, vs, sync, db = _build("cascade.db")
        try:
            ingest_one(
                IngestDoc(doc_id="doc-1", title="Chunked", content=LONG_TEXT,
                          doc_type="text", metadata={}),
                indexer, Deduplicator(db), chunk_size=500, sync=sync)
            sync.flush()
            chunk_ids = storage.chunk_ids("doc-1")
            self.assertTrue(chunk_ids)
            for cid in chunk_ids:
                self.assertIsNotNone(vs.store.get_content_hash(cid))
            self.assertTrue(indexer.delete_document("doc-1"))  # cascades
            sync.flush()
            for cid in chunk_ids:
                self.assertIsNone(storage.get_document(cid))
                self.assertIsNone(vs.store.get_content_hash(cid),
                                  f"vector for {cid} survived the cascade")
        finally:
            sync.close(); vs.close(); storage.close()

    def test_reingest_replaces_old_chunks_and_their_vectors(self):
        storage, indexer, vs, sync, db = _build("reingest.db")
        try:
            ingest_one(
                IngestDoc(doc_id="doc-1", title="V1", content=LONG_TEXT,
                          doc_type="text", metadata={}),
                indexer, Deduplicator(db), chunk_size=500, sync=sync)
            sync.flush()
            old_ids = set(storage.chunk_ids("doc-1"))
            self.assertGreaterEqual(len(old_ids), 2)

            new_content = "Beta content with different vocabulary entirely. " * 40
            status = ingest_one(
                IngestDoc(doc_id="doc-1", title="V2", content=new_content,
                          doc_type="text", metadata={}),
                indexer, Deduplicator(db), chunk_size=500, sync=sync)
            self.assertEqual(status, "indexed")
            sync.flush()
            new_ids = set(storage.chunk_ids("doc-1"))
            # re-ingest REPLACED the old chunks: no stale rows, no stale
            # vectors  the stored set is exactly the new set
            self.assertTrue(new_ids)
            self.assertNotEqual(new_ids, set())  # (replaced, not deleted)
            for cid in old_ids - new_ids:
                self.assertIsNone(storage.get_document(cid))
                self.assertIsNone(vs.store.get_content_hash(cid),
                                  "stale vector survived re-ingest")
            self.assertEqual(vs.get_stats()["count"], len(new_ids))
            # the vectors hold the NEW text: search the new vocabulary and
            # every hit must come from the re-ingested chunks
            hits = vs.search("Beta content different vocabulary", top_k=10)
            self.assertTrue(hits)
            for h in hits:
                self.assertIn(h.doc_id, new_ids)
        finally:
            sync.close(); vs.close(); storage.close()

    def test_reingest_with_fewer_chunks_leaves_no_stale_vector(self):
        storage, indexer, vs, sync, db = _build("shrink.db")
        try:
            ingest_one(
                IngestDoc(doc_id="doc-1", title="V1", content=LONG_TEXT,
                          doc_type="text", metadata={}),
                indexer, Deduplicator(db), chunk_size=500, sync=sync)
            sync.flush()
            old_ids = set(storage.chunk_ids("doc-1"))

            # re-ingest SHORT -> no chunking, a single parent row
            status = ingest_one(
                IngestDoc(doc_id="doc-1", title="V2", content="short now",
                          doc_type="text", metadata={}),
                indexer, Deduplicator(db), chunk_size=500, sync=sync)
            self.assertEqual(status, "indexed")
            sync.flush()
            self.assertFalse(storage.chunk_ids("doc-1"))
            for cid in old_ids:
                self.assertIsNone(storage.get_document(cid))
                self.assertIsNone(vs.store.get_content_hash(cid),
                                  "stale chunk vector survived re-ingest")
            self.assertIsNotNone(vs.store.get_content_hash("doc-1"))
        finally:
            sync.close(); vs.close(); storage.close()


if __name__ == "__main__":
    unittest.main()
