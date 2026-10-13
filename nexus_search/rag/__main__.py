"""P7-10 CLI — local debugging for the RAG answer path.

    python -m nexus_search.rag ask "how does bm25 rank documents" [--top-k 5]

Uses the SAME components the API wires (shared HybridSearch over
NEXUS_DB, Storage, calibrated gate, provider from env — the STUB by
default in dev). Prints the answer, citations and the raw gate signals.
The rag package never imports core.api (the CLI builds its own stack
from the same pieces); no key is ever printed."""
import argparse
import os
import sys


def _boot():
    from .config import RagConfig, build_provider
    from .context import make_storage_neighbor_fetcher
    from .metrics import RagMetrics
    from .pipeline import RagService
    from .provider import StubProvider
    from .retrieval import RagRetriever
    from ..core.hybrid_search import HybridSearch
    from ..core.storage import Storage
    from ..core.vector_store import VectorStoreManager

    config = RagConfig.from_env()
    provider, status, reason = build_provider(config)
    if provider is None:
        print(f"rag subsystem {status} (reason={reason}); falling back to "
              "the offline stub FOR DEBUGGING ONLY")
        provider = StubProvider()
    db = os.environ.get("NEXUS_DB", "nexus_search.db")
    storage = Storage(db)
    try:
        vs = VectorStoreManager(db)
    except Exception as exc:
        print(f"vector subsystem unavailable ({type(exc).__name__}); "
              "keyword-only degraded retrieval")
        vs = None
    hybrid = HybridSearch(storage, vector_store=vs, db_path=db) if vs else None
    retriever = RagRetriever(hybrid, storage, bm25_search=None)
    service = RagService(config, retriever, provider, storage,
                         metrics=RagMetrics(),
                         neighbor_fetcher=make_storage_neighbor_fetcher(storage))
    return service


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m nexus_search.rag",
        description="Nexus Search RAG debug CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    ask_p = sub.add_parser("ask", help="ask one question through the "
                                       "RAG pipeline")
    ask_p.add_argument("query")
    ask_p.add_argument("--top-k", type=int, default=5)
    ask_p.add_argument("--mode", default="hybrid",
                       choices=["keyword", "semantic", "hybrid"])
    sub.add_parser("calibrate", help="re-run the refuse-gate floor "
                                     "calibration and print the report")
    args = parser.parse_args(argv)

    if args.command == "calibrate":
        from .calibration import run_calibration
        report = run_calibration(quiet=False)
        import json
        print(json.dumps(report["test"], indent=2))
        return 0

    service = _boot()
    try:
        result = service.ask(args.query, top_k=args.top_k, mode=args.mode)
    finally:
        try:
            if service.retriever.hybrid is not None:
                service.retriever.hybrid.close()
            service.storage.close()
        except Exception:
            pass
    print(f"answerable : {result.answerable}")
    if result.refusal_reason:
        print(f"refusal    : {result.refusal_reason}")
    if result.degraded_reason:
        print(f"degraded   : {result.degraded_reason}")
    print(f"answer     : {result.answer}")
    if result.citations:
        print("citations  :")
        for c in result.citations:
            print(f"  [{c.n}] {c.chunk_id} — {c.title} "
                  f"(verified={c.status})")
    if result.sources:
        print("sources    :")
        for s in result.sources:
            print(f"  [{s['n']}] {s['chunk_id']} — {s['title']}")
    signals = result.metadata.get("retrieval", {}).get("gate_signals", {})
    print(f"gate       : bm25={signals.get('top_bm25')} "
          f"(floor {signals.get('bm25_floor')}), "
          f"vector={signals.get('top_vector')} "
          f"(floor {signals.get('vector_floor')}), "
          f"embedder={signals.get('embedder')}")
    print(f"spend      : tokens_in={result.metadata.get('tokens_in')} "
          f"tokens_out={result.metadata.get('tokens_out')} "
          f"cost={result.metadata.get('estimated_cost')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
