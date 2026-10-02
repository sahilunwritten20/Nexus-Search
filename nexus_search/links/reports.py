"""WP5: link-graph reports — dead-link and orphan-page detection.

Both are READ-ONLY cross-references over data the system already has:
- dead links: link-graph targets whose latest crawl attempt ended in a
  PERMANENT failure (4xx) recorded in the frontier's crawl_errors log
- orphan pages: indexed web pages with ZERO inbound edges (excluding
  crawl seeds — a seed nobody links to yet is not a discovery failure)

Neither report ever triggers a fetch (same rule as the rest of Phase 6).
"""
import json
import sqlite3

from .graph import LinkGraph

# rel values are irrelevant here; anchors are the interesting metadata
_MAX_INLINKS_PER_TARGET = 10

# terminal HTTP failures: a 4xx is permanent except 408 (timeout) and 429
# (rate limit), which stay retryable (see crawler/frontier.py).
_TERMINAL_4XX_PREFIX = "HTTP 4"
_RETRYABLE_4XX = ("HTTP 408", "HTTP 429")


def _last_error_for_target(frontier_conn, url: str):
    row = frontier_conn.execute(
        "SELECT error, timestamp FROM crawl_errors WHERE url = ? "
        "ORDER BY timestamp DESC LIMIT 1", (url,)).fetchone()
    return (row[0], row[1]) if row else (None, None)


def dead_links(main_db: str, frontier_db: str) -> dict:
    """Targets of at least one link whose latest recorded crawl attempt was
    a permanent 4xx. Also lists linked-but-never-fetched targets separately
    (not dead — pending/unreachable, but worth seeing).

    Returns {"dead": [{"url", "error", "inlinks"}],
             "unfetched": ["url", ...]}. No network requests."""
    graph = LinkGraph(main_db)
    frontier = sqlite3.connect(frontier_db)
    frontier.execute("PRAGMA busy_timeout = 5000")
    try:
        visited = {row[0] for row in frontier.execute("SELECT url FROM visited")}
        targets = graph.conn.execute(
            "SELECT DISTINCT to_url FROM link_edges ORDER BY to_url").fetchall()
        dead, unfetched = [], []
        for (to_url,) in targets:
            error, _ts = _last_error_for_target(frontier, to_url)
            if error and error.startswith(_TERMINAL_4XX_PREFIX) \
                    and not error.startswith(_RETRYABLE_4XX):
                inlinks = [r[0] for r in graph.conn.execute(
                    "SELECT from_url FROM link_edges WHERE to_url = ? "
                    "ORDER BY from_url LIMIT ?",
                    (to_url, _MAX_INLINKS_PER_TARGET + 1)).fetchall()]
                dead.append({
                    "url": to_url,
                    "error": error,
                    "inlinks": inlinks[:_MAX_INLINKS_PER_TARGET],
                    "inlink_count": len(inlinks),
                })
            elif not error and to_url not in visited:
                unfetched.append(to_url)
        return {"dead": dead, "unfetched": unfetched}
    finally:
        frontier.close()
        graph.close()


def orphan_pages(storage, graph: LinkGraph, exclude_seeds: bool = True) -> list[dict]:
    """Indexed web pages with zero inbound edges.

    `exclude_seeds=True` (default) drops depth-0 crawl entries: a seed URL
    that nobody links to yet is a crawl-start fact, not an orphan problem.
    A page is an orphan when its NORMALIZED URL receives no edge — the same
    normalization choke point the graph writes through (BUG-02)."""
    import json
    from ..crawler.url_utils import normalize_url

    with storage.lock:
        rows = storage.conn.execute(
            "SELECT doc_id, metadata FROM documents "
            "WHERE doc_type = 'web' ORDER BY doc_id").fetchall()
    candidates = []
    for doc_id, metadata_json in rows:
        try:
            metadata = json.loads(metadata_json) if metadata_json else {}
        except (ValueError, TypeError):
            metadata = {}
        url = metadata.get("url") or metadata.get("canonical_url")
        if not url:
            continue
        if exclude_seeds and metadata.get("depth") == 0:
            continue
        try:
            normalized = normalize_url(url)
        except ValueError:
            normalized = url
        candidates.append((doc_id, url, normalized))
    if not candidates:
        return []
    with graph.lock:
        linked = {r[0] for r in graph.conn.execute("SELECT to_url FROM link_edges")}
    return [{"doc_id": doc_id, "url": url}
            for doc_id, url, normalized in candidates
            if normalized not in linked]

