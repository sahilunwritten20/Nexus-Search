"""FastAPI application exposing /documents and /search.

Requires: pip install fastapi uvicorn
Run with: uvicorn nexus_search.core.api:app --reload
"""
from fastapi import FastAPI, HTTPException

from .bm25 import BM25Search
from .indexer import Indexer
from .models import DocumentIn, SearchResponse, SearchResultOut
from .storage import Storage

app = FastAPI(title="Nexus Search — Core", version="0.2.0")

_storage = Storage("nexus_search.db")
_indexer = Indexer(_storage)
_searcher = BM25Search(_storage)


@app.post("/documents", status_code=201)
def add_document(doc: DocumentIn):
    _indexer.add_document(
        doc_id=doc.doc_id,
        content=doc.content,
        title=doc.title,
        doc_type=doc.doc_type,
        metadata=doc.metadata,
    )
    return {"doc_id": doc.doc_id, "status": "indexed"}


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str):
    if not _indexer.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"doc_id": doc_id, "status": "deleted"}


@app.get("/search", response_model=SearchResponse)
def search(q: str, top_k: int = 10):
    top_k = min(max(top_k, 1), 100)
    results = _searcher.search(q, top_k=top_k)
    return SearchResponse(
        query=q,
        total_results=len(results),
        results=[SearchResultOut(**r.__dict__) for r in results],
    )


@app.get("/health")
def health():
    return {"status": "ok", "documents": _storage.document_count()}
