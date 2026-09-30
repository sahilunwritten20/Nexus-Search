"""Deterministic keyword-mode fixture for the byte-identity regression test.

Shared by scripts/dev/regen_golden.py (writes tests/golden/keyword_baseline.json)
and tests/core/test_golden_keyword.py (compares live output to the golden file).

ASCII-only on purpose: float formatting and Unicode folding are stable here, so
any diff in this file's output is a real ranking/pipeline change, not platform
noise. The corpus mixes the evaluation dataset with handcrafted docs so the
golden snapshot exercises plain queries, quoted phrases, type:/lang: filters,
boolean structure, and offset pagination through the REAL API keyword path
(HybridSearch KEYWORD plain mode -> BM25Search.search_page).
"""
from nexus_search.core.hybrid_search import HybridSearch, SearchMode
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage

# --- corpus ---------------------------------------------------------------

_EVAL_DOCS = [
    ("doc1", "Introduction to Machine Learning",
     "Machine learning is a subset of artificial intelligence that enables "
     "systems to learn from data without explicit programming."),
    ("doc2", "Deep Learning Fundamentals",
     "Deep learning uses neural networks with multiple layers to model "
     "complex patterns in data."),
    ("doc5", "Information Retrieval and Search Engines",
     "Search engines use inverted indexes and ranking algorithms like BM25. "
     "Modern systems combine lexical and semantic search."),
    ("doc6", "Python for Data Science",
     "Python is the primary language for data science. Libraries like pandas "
     "and numpy enable data manipulation."),
    ("doc7", "SQL and Relational Databases",
     "SQL is the standard language for relational databases. It supports "
     "queries, joins, aggregations, and transactions."),
    ("doc10", "Kubernetes and Container Orchestration",
     "Kubernetes automates container deployment, scaling, and management."),
]

_EXTRA_DOCS = [
    ("extra1", "Machine Learning Operations Guide",
     "Machine learning operations cover deployment, monitoring, and "
     "retraining of machine learning models in production systems.",
     "text", {}),
    ("extra2", "Learning Machines in History",
     "Early learning machines preceded modern machine learning. "
     "This page is about mechanical history, not machine learning.",
     "text", {}),
    ("extra3", "Machine Learning Tutorial (PDF)",
     "A machine learning tutorial distributed as a printable document.",
     "pdf", {}),
    ("extra4", "Machine Learning Slides",
     "Presentation slides introducing machine learning concepts.",
     "web", {"language": "en"}),
    ("extra5", "Apprentissage automatique",
     "Le machine learning est un domaine de l'intelligence artificielle.",
     "text", {"language": "fr"}),
    ("extra6", "Phrase Boost Target",
     "This document contains the exact phrase machine learning operations "
     "so quoted queries can find it inside a longer body of running text "
     "that exists purely to add length to the document for snippet testing.",
     "text", {}),
    ("extra7", "Neural Networks Textbook",
     "Neural networks and deep learning textbook chapter about backpropagation.",
     "text", {}),
    ("extra8", "Databases Cookbook",
     "Database recipes for SQL joins, window functions and query tuning.",
     "text", {}),
    ("extra9", "Long Document About Many Things",
     " ".join(f"filler paragraph number {i} about general topics" for i in range(40)),
     "text", {}),
    ("extra10", "Machine Learning Machine Learning",
     "machine learning machine learning machine learning term frequency "
     "saturation test document.",
     "text", {}),
]


def build_corpus(storage: Storage) -> Indexer:
    """Index the deterministic fixture corpus into `storage`."""
    indexer = Indexer(storage)
    for doc_id, title, content in _EVAL_DOCS:
        indexer.add_document(doc_id, content, title=title, doc_type="text")
    for doc_id, title, content, doc_type, metadata in _EXTRA_DOCS:
        indexer.add_document(doc_id, content, title=title, doc_type=doc_type,
                             metadata=metadata)
    return indexer


# --- query set -------------------------------------------------------------

GOLDEN_QUERIES = [
    {"q": "machine learning", "top_k": 10, "offset": 0},
    {"q": "machine learning", "top_k": 5, "offset": 5},
    {"q": '"machine learning operations"', "top_k": 10, "offset": 0},
    {"q": "type:pdf", "top_k": 10, "offset": 0},
    {"q": "lang:fr", "top_k": 10, "offset": 0},
    {"q": "machine AND learning", "top_k": 10, "offset": 0},
    {"q": "databases OR kubernetes", "top_k": 10, "offset": 0},
    {"q": "learning -history", "top_k": 10, "offset": 0},
    {"q": "neural networks", "top_k": 10, "offset": 0},
    {"q": "sql joins", "top_k": 10, "offset": 0},
    {"q": "filler paragraph", "top_k": 3, "offset": 0},
    {"q": "title:learning", "top_k": 10, "offset": 0},
    {"q": "bm25 ranking", "top_k": 10, "offset": 0},
    {"q": "pandas numpy", "top_k": 10, "offset": 0},
    {"q": "type:web machine learning", "top_k": 10, "offset": 0},
]


def run_golden_queries(hybrid: HybridSearch):
    """[(query_spec, SearchPage)] through the API keyword path."""
    out = []
    for spec in GOLDEN_QUERIES:
        page = hybrid.search_page(
            spec["q"], top_k=spec["top_k"], offset=spec["offset"],
            mode=SearchMode.KEYWORD)
        out.append((spec, page))
    return out


def serialize_page(page) -> dict:
    return {
        "total": page.total,
        "results": [
            {
                "doc_id": r.doc_id,
                "score": round(r.score, 6),
                "title": r.title,
                "snippet": r.snippet,
                "doc_type": r.doc_type,
                "chunk_id": r.chunk_id,
                "matched_chunks": r.matched_chunks,
            }
            for r in page.results
        ],
    }


def golden_output(db_path: str) -> dict:
    """Build the fixture in `db_path` and return the serialized golden dict."""
    storage = Storage(db_path)
    hybrid = None
    try:
        build_corpus(storage)
        hybrid = HybridSearch(storage, db_path=db_path)
        return {
            "queries": [
                {"q": spec["q"], "top_k": spec["top_k"], "offset": spec["offset"],
                 **serialize_page(page)}
                for spec, page in run_golden_queries(hybrid)
            ]
        }
    finally:
        if hybrid is not None:
            hybrid.close()  # owns a second connection to the same file (Windows)
        storage.close()
