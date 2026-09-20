"""FastAPI application exposing /documents and /search.

Run with: uvicorn nexus_search.core.api:app --reload
DB path: env NEXUS_DB (default nexus_search.db)
"""
import os
import sqlite3

from fastapi import FastAPI, HTTPException

from .bm25 import BM25Search
from .indexer import Indexer
from .models import DocumentIn, SearchResponse, SearchResultOut
from .storage import Storage

app = FastAPI(title="Nexus Search — Core", version="0.3.0")

_storage = Storage(os.environ.get("NEXUS_DB", "nexus_search.db"))
_indexer = Indexer(_storage)
_searcher = BM25Search(_storage)


@app.post("/documents", status_code=201)
def add_document(doc: DocumentIn):
    try:
        _indexer.add_document(
            doc_id=doc.doc_id, content=doc.content, title=doc.title,
            doc_type=doc.doc_type, metadata=doc.metadata,
        )
    except sqlite3.Error as exc:
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")
    return {"doc_id": doc.doc_id, "status": "indexed"}


@app.delete("/documents/{doc_id}")
def delete_document(doc_id: str):
    if not _indexer.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"doc_id": doc_id, "status": "deleted"}


@app.get("/search", response_model=SearchResponse)
def search(q: str, top_k: int = 10, offset: int = 0):
    if not q.strip():
        raise HTTPException(status_code=400, detail="q must not be empty")
    top_k = min(max(top_k, 1), 100)
    offset = min(max(offset, 0), 10_000)
    page = _searcher.search_page(q, top_k=top_k, offset=offset)
    return SearchResponse(
        query=q,
        total_results=page.total,
        offset=offset,
        top_k=top_k,
        results=[SearchResultOut(**r.__dict__) for r in page.results],
    )


@app.get("/health")
def health():
    return {"status": "ok", "documents": _storage.document_count()}