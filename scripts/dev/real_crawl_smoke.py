"""WP9: real-crawl smoke runner — the production path against the real web.

Safety rules baked in (docs/REAL_CRAWL_RUNBOOK.md documents the same):
- public crawl path: SSRF validation + DNS pinning + robots.txt fail-closed
  all ACTIVE (no allow_private_hosts)
- single worker, crawl delay >= 1s/host (politeness floor), depth 1,
  hard max_pages cap, wall-clock watchdog
- target: quotes.toscrape.com — a public sandbox built for scraping
  practice (already the repo's seeds.txt default)

Run:  python scripts/dev/real_crawl_smoke.py [--db tmp-crawl.db]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from nexus_search.core.indexer import Indexer  # noqa: E402
from nexus_search.core.storage import Storage  # noqa: E402
from nexus_search.links.authority import compute_authority  # noqa: E402
from nexus_search.links.graph import LinkGraph  # noqa: E402
from nexus_search.links.reports import dead_links, orphan_pages  # noqa: E402

TARGET = "https://quotes.toscrape.com/"
# Honest identity for an audit-validation crawl of a scraping-practice
# sandbox; the pipeline's placeholder-UA refusal stays active (this string
# passes it because it makes no fake contact claim).
USER_AGENT = "NexusSearchBot/0.1 (+nexus-search audit validation crawl)"
MAX_PAGES = 25
MAX_DEPTH = 1
CRAWL_DELAY = 1.0
WALL_CLOCK_CAP_S = 240.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(os.path.dirname(__file__),
                                                    "..", "..", "real_crawl.db"))
    args = parser.parse_args()
    db = os.path.abspath(args.db)
    frontier_db = os.path.splitext(db)[0] + "_frontier.db"
    print(f"real crawl smoke | target={TARGET} max_pages={MAX_PAGES} "
          f"depth={MAX_DEPTH} delay={CRAWL_DELAY}s worker=1")

    from nexus_search.crawler.cli import _frontier_db_path
    from nexus_search.crawler.pipeline import CrawlPipeline
    from nexus_search.ingestion.dedup import Deduplicator
    from nexus_search.ingestion.pipeline import make_crawler_ingest_fn
    from nexus_search.core.vector_store import VectorStoreManager
    from nexus_search.core.embedding_sync import create_embedding_sync
    from nexus_search.ingestion.failures import FailureQueue
    from nexus_search.ingestion.history import ContentHistory

    storage = Storage(db)
    indexer = Indexer(storage)
    dedup = Deduplicator(db)
    vector_store = VectorStoreManager(db)
    sync = create_embedding_sync(vector_store, batch_size=32)
    sync.attach(indexer)
    failures = FailureQueue(db)
    history = ContentHistory(db)
    graph = LinkGraph(db)

    started = time.time()
    pipeline = CrawlPipeline(
        db_path=_frontier_db_path(db),
        allowed_domains=["quotes.toscrape.com"],
        max_pages=MAX_PAGES,
        max_depth=MAX_DEPTH,
        concurrency=1,
        default_crawl_delay=CRAWL_DELAY,
        user_agent=USER_AGENT,
        ingest_fn=make_crawler_ingest_fn(indexer, dedup, chunk_size=1000,
                                          failure_queue=failures, history=history),
        link_graph=graph,
    )
    # watchdog: refuse to run past the wall-clock cap no matter what
    def _watchdog():
        if time.time() - started > WALL_CLOCK_CAP_S:
            print(f"wall-clock cap {WALL_CLOCK_CAP_S}s hit — stopping crawl")
            pipeline.frontier.mark_skipped = pipeline.frontier.mark_skipped  # no-op
            os._exit(2)
    import threading
    watchdog = threading.Timer(WALL_CLOCK_CAP_S, _watchdog)
    watchdog.daemon = True
    watchdog.start()

    try:
        pipeline.seed([TARGET])
        stats = pipeline.run()
        elapsed = time.time() - started
        watchdog.cancel()
    finally:
        sync.flush()

    print(f"crawl stats: {stats}")
    print(f"elapsed: {elapsed:.1f}s | docs indexed: {storage.document_count()}")

    print("\n=== link intelligence (real URLs) ===")
    print(f"graph: {graph.stats()}")
    comp = compute_authority(graph, force=True)
    print(f"authority: {comp.pages} pages, {comp.edges_used}/{comp.edges_in} "
          f"endorsing edges, converged={comp.converged}, "
          f"{comp.iterations} iters, {comp.seconds:.2f}s")
    sample_urls = [r[0] for r in graph.conn.execute(
        "SELECT url FROM authority_scores ORDER BY authority DESC LIMIT 5")]
    for u in sample_urls:
        found = graph.authority_for(u)
        print(f"  top: {u}  authority={found[0]:.3f} popularity={found[1]:.2f}")
    report = dead_links(db, frontier_db)
    print(f"dead links: {len(report['dead'])} | linked-but-unfetched: "
          f"{len(report['unfetched'])}")
    orphans = orphan_pages(storage, graph)
    print(f"orphans: {len(orphans)}")

    print("\n=== search over the crawled corpus ===")
    hybrid = HybridSearch(storage, vector_store=vector_store, db_path=db)
    for q in ["love", "life", "books"]:
        for mode in (SearchMode.KEYWORD, SearchMode.HYBRID):
            page = hybrid.search_page(q, top_k=3, mode=mode)
            titles = [r.title[:40] for r in page.results]
            print(f"  {mode.value:8s} '{q}': total={page.total} top={titles[:2]}")
    hybrid.close()

    sync.close()
    vector_store.close()
    graph.close()
    history.close()
    failures.close()
    dedup.close()
    storage.close()
    print("\nreal crawl smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
