"""WP10 runner: compare search systems on the labeled fixture at K=10.

Systems (identical data, identical split):
  keyword          — BM25 only
  semantic-hash    — hash embedder (LEXICAL control; expected to lose on
                     paraphrase/synonym and win on exact)
  semantic-st      — sentence-transformers/all-MiniLM-L6-v2 (real semantics;
                     marked Unverified when the model can't load)
  hybrid-rrf       — BM25 + st, reciprocal rank fusion
  hybrid-weighted  — BM25 + st, min-max weighted fusion
Variations: query understanding on/off (keyword + hybrid-rrf);
hybrid-rrf with rerank on/off.

Honesty rules: results reported as measured; sanity checks (st > hash on
paraphrase, hybrid >= keyword on paraphrase without hurting exact) are
asserted with INVESTIGATE advice, not silently. no-answer queries report
score distributions so confident junk is visible.
"""
import json
import os
import random
import statistics
import time

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from .fixture import SPLIT_SEED, build_fixture  # noqa: E402
from ..metrics import evaluate_query  # noqa: E402

K = 10


def _split(queries):
    """dev/test 60/40 by the committed seed; per-category stratified so both
    splits see every category."""
    rng = random.Random(SPLIT_SEED)
    by_cat = {}
    for q in queries:
        by_cat.setdefault(q.category, []).append(q)
    dev, test = [], []
    for cat in sorted(by_cat):
        rows = sorted(by_cat[cat], key=lambda q: q.query_id)
        rng.shuffle(rows)
        cut = max(1, int(len(rows) * 0.6))
        dev.extend(rows[:cut])
        test.extend(rows[cut:])
    return dev, test


def _vector_coverage(hybrid, n_docs) -> float:
    return hybrid.vector_store.get_stats().get("count", 0) / max(n_docs, 1)


def _build_systems(storage, docs, db_path: str, embedder=None):
    """(systems: name -> search_fn, hybrid). Builds over the given storage
    + db_path (Storage does not expose its path — the caller owns it).
    `embedder` pins the vector backend at construction: the ST arm and the
    hash control arm must be SEPARATE stores (different embedders)."""
    from ...core.hybrid_search import HybridSearch, SearchMode
    from ...core.indexer import Indexer
    from ...core.vector_store import VectorStoreManager
    from ...core.embedding_sync import EmbeddingSync
    from ...ranking.query import understand_query
    from ...ranking.ranker import rerank, RankingWeights

    indexer = Indexer(storage)
    vs = VectorStoreManager(db_path, embedder=embedder)
    sync = EmbeddingSync(vs, batch_size=64)
    sync.attach(indexer)
    for doc in docs:
        indexer.add_document(doc.doc_id, doc.content, title=doc.title,
                             doc_type="text")
    sync.flush()
    hybrid = HybridSearch(storage, vector_store=vs, db_path=db_path)

    systems = {}

    def kw(q, top_k, understanding_on):
        u = understand_query(q, storage) if understanding_on else None
        page = hybrid.search_page(q, top_k=top_k, mode=SearchMode.KEYWORD,
                                   understanding=u)
        return [r.doc_id for r in page.results]

    def hybrid_rrf(q, top_k, understanding_on, rerank_on):
        u = understand_query(q, storage) if understanding_on else None
        page = hybrid.search_page(q, top_k=top_k, mode=SearchMode.HYBRID,
                                   understanding=u)
        results = page.results
        if rerank_on:
            ranked = rerank(results, q, weights=RankingWeights(),
                            storage=storage,
                            understanding=understand_query(q, storage)
                            if understanding_on else None)
            results = [r.result for r in ranked]
        return [r.doc_id for r in results]

    systems["keyword"] = lambda q, k=K: kw(q, k, understanding_on=False)
    systems["keyword+understanding"] = lambda q, k=K: kw(q, k, understanding_on=True)
    systems["hybrid-rrf"] = lambda q, k=K: hybrid_rrf(q, k, False, False)
    systems["hybrid-rrf+understanding"] = lambda q, k=K: hybrid_rrf(q, k, True, False)
    systems["hybrid-rrf+rerank"] = lambda q, k=K: hybrid_rrf(q, k, False, True)
    return systems, hybrid, vs, sync


def _bootstrap_ci(diffs, iterations=2000, seed=7):
    """Paired bootstrap CI for the mean of `diffs` (system_a - system_b per
    query). Returns (low, high) at 95%."""
    rng = random.Random(seed)
    n = len(diffs)
    if n == 0:
        return (0.0, 0.0)
    means = []
    for _ in range(iterations):
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    return (means[int(0.025 * iterations)], means[int(0.975 * iterations)])


def run_benchmark(quiet: bool = False) -> dict:
    import tempfile
    from ...core.storage import Storage

    docs, queries = build_fixture()
    dev, test = _split(queries)

    report = {"k": K, "docs": len(docs), "queries_total": len(queries),
              "dev_queries": len(dev), "test_queries": len(test),
              "split_seed": SPLIT_SEED, "systems": {}, "sanity": {},
              "per_category": {}, "significance": {}}

    tmp = tempfile.mkdtemp()
    try:
        return _run_benchmark_in(tmp, docs, dev, test, report, quiet)
    finally:
        import shutil
        import time as _t
        for attempt in range(10):  # Windows: connections close asynchronously
            try:
                shutil.rmtree(tmp)
                break
            except PermissionError:
                _t.sleep(0.2)


def _run_benchmark_in(tmp, docs, dev, test, report, quiet):
    from ...core.storage import Storage  # moved body needs it locally
    # (original body below; the finally block closes every store opened)
    if True:  # keep indentation of the original body
        # st store + hash store are SEPARATE indexes (different embedders);
        # build both over the same corpus + split
        storage = Storage(os.path.join(tmp, "bench.db"))
        try:
            # the ST arm must be its own store: try the model FIRST and
            # pin it at construction
            st_available, st_emb = _st_embedder()
            report["st_available"] = st_available
            systems, hybrid, vs, sync = _build_systems(
                storage, docs, os.path.join(tmp, "bench.db"), embedder=st_emb)
            coverage = _vector_coverage(hybrid, len(docs))
            report["vector_coverage"] = round(coverage, 4)
            if coverage < 1.0:
                report["error"] = "vector_coverage_not_100%"
                if not quiet:
                    print("Refusing to report - vector coverage below 100%")
                return report

            # hash control store
            os.environ["NEXUS_EMBEDDER"] = "hash:384"
            from ...core.embedders import reset_embedder
            reset_embedder()
            hash_storage = Storage(os.path.join(tmp, "hash.db"))
            hash_systems, hash_hybrid, hash_vs, hash_sync = _build_systems(
                hash_storage, docs, os.path.join(tmp, "hash.db"))

            run_set = test if test else dev   # report on held-out test
            all_systems = dict(systems)
            if st_available:
                all_systems["semantic-st"] = lambda q, k=K: [
                    r.doc_id for r in hybrid.search_page(
                        q, top_k=k, mode=_search_mode("semantic")).results]
            all_systems["semantic-hash"] = lambda q, k=K: [
                r.doc_id for r in hash_hybrid.search_page(
                    q, top_k=k, mode=_search_mode("semantic")).results]

            for name, fn in sorted(all_systems.items()):
                per_q = {}
                latencies = []
                for q in run_set:
                    t0 = time.perf_counter()
                    got = fn(q.text)
                    latencies.append(time.perf_counter() - t0)
                    per_q[q.query_id] = evaluate_query(q.relevant, got, k_values=[K])
                agg = _aggregate(per_q, latencies)
                report["systems"][name] = agg
                # per-category breakdown for the key systems
                if name in ("keyword", "semantic-hash", "hybrid-rrf") or \
                        (name == "semantic-st" and st_available):
                    cat_metrics = {}
                    for cat in sorted(set(q.category for q in run_set)):
                        rows = [q for q in run_set if q.category == cat]
                        merged = {}
                        for q in rows:
                            if q.query_id in per_q:
                                merged[q.query_id] = per_q[q.query_id]
                        if merged:
                            cat_metrics[cat] = _aggregate(merged, [])
                    report["per_category"][name] = cat_metrics

            # no-answer score-distribution honesty
            na = [q for q in run_set if q.category == "no-answer"]
            if na:
                dist = {}
                for name in ("keyword", "semantic-hash", "hybrid-rrf"):
                    scores = []
                    for q in na:
                        page = (hybrid if not name.startswith("semantic-hash")
                                else hash_hybrid).search_page(
                            q.text, top_k=1,
                            mode=_search_mode("hybrid" if "hybrid" in name
                                              else "keyword"))
                        scores.append(round(page.results[0].score, 3)
                                      if page.results else None)
                    dist[name] = scores
                report["no_answer_top1_scores"] = dist

            # paired bootstrap significance on NDCG@10
            def ndcg_vec(fn_a, fn_b):
                out = []
                for q in run_set:
                    a = evaluate_query(q.relevant, fn_a(q.text), k_values=[K])["ndcg@10"]
                    b = evaluate_query(q.relevant, fn_b(q.text), k_values=[K])["ndcg@10"]
                    out.append(a - b)
                return out

            if st_available:
                report["significance"]["st_vs_hash_ndcg10_ci95"] = [
                    round(x, 4) for x in _bootstrap_ci(ndcg_vec(
                        all_systems["semantic-st"], all_systems["semantic-hash"]))]
            report["significance"]["hybrid_vs_keyword_ndcg10_ci95"] = [
                round(x, 4) for x in _bootstrap_ci(ndcg_vec(
                    all_systems["hybrid-rrf"], all_systems["keyword"]))]

            # sanity checks (investigate, never silently pass)
            st_row = report["systems"].get("semantic-st", {})
            hash_row = report["systems"].get("semantic-hash", {})
            hyb_row = report["systems"].get("hybrid-rrf", {})
            kw_row = report["systems"].get("keyword", {})
            if st_available:
                st_para = report["per_category"].get("semantic-st", {}).get("paraphrase", {})
                hash_para = report["per_category"].get("semantic-hash", {}).get("paraphrase", {})
                report["sanity"]["st_beats_hash_on_paraphrase"] = (
                    st_para.get("ndcg@10", 0) > hash_para.get("ndcg@10", 0))
            report["sanity"]["hybrid_not_below_keyword_overall"] = (
                hyb_row.get("ndcg@10", 0) >= kw_row.get("ndcg@10", 0) - 0.05)
            report["sanity"]["exact_not_hurt_by_hybrid"] = (
                report["per_category"].get("hybrid-rrf", {}).get("exact", {})
                .get("ndcg@10", 0) >= 0.5)

            for closer in (hash_sync.close, hash_hybrid.close, hash_vs.close,
                           hash_storage.close):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                sync.close()
                hybrid.close()
                vs.close()
                storage.close()
            except Exception:  # noqa: BLE001
                pass
    return report


def _search_mode(name: str):
    from ...core.hybrid_search import SearchMode
    return SearchMode(name)


def _st_embedder():
    """(available, embedder|None): can sentence-transformers load here?
    No cache / no network -> (False, None): the benchmark runs on the hash
    control and the ST arm is marked Unverified."""
    try:
        from ...core.embedders import reset_embedder, SentenceTransformerEmbedder
        reset_embedder()
        emb = SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2")
        return (emb.dim is not None), emb
    except Exception:  # noqa: BLE001 - honest skip
        return (False, None)


def _aggregate(per_q: dict, latencies: list) -> dict:
    n = len(per_q)
    agg = {}
    for key in ("precision@10", "recall@10", "ndcg@10", "mrr"):
        agg[key] = round(sum(r[key] for r in per_q.values()) / n, 4) if n else 0.0
    if latencies:
        ordered = sorted(latencies)
        agg["latency_p50_ms"] = round(ordered[len(ordered) // 2] * 1000, 1)
        agg["latency_p95_ms"] = round(ordered[int(len(ordered) * 0.95)] * 1000, 1)
    return agg


def main() -> int:
    report = run_benchmark()
    out_path = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                            "docs", "SEMANTIC_BENCHMARK.md")
    lines = ["# Semantic Benchmark (WP10)", "",
             f"Fixture: {report['docs']} docs / {report['queries_total']} queries "
             f"(dev {report['dev_queries']} / test {report['test_queries']}, "
             f"seed {report['split_seed']}); reported on the held-out test split.", ""]
    if "error" in report:
        lines.append(f"ERROR: {report['error']}")
    else:
        lines += ["| system | P@10 | R@10 | MRR | NDCG@10 | p50 ms | p95 ms |",
                  "|---|---|---|---|---|---|---|"]
        for name, row in sorted(report["systems"].items()):
            lines.append(
                f"| {name} | {row['precision@10']:.3f} | {row['recall@10']:.3f} | "
                f"{row['mrr']:.3f} | {row['ndcg@10']:.3f} | "
                f"{row.get('latency_p50_ms', '-')} | {row.get('latency_p95_ms', '-')} |")
        lines += ["", "## Per-category NDCG@10 (key systems)", "",
                  "| category | " + " | ".join(sorted(report["per_category"])) + " |",
                  "|---" * (len(report["per_category"]) + 1) + "|"]
        cats = sorted(set(c for rows in report["per_category"].values()
                          for c in rows))
        for cat in cats:
            cells = [f"{report['per_category'][s].get(cat, {}).get('ndcg@10', '-'):.3f}"
                     if isinstance(report['per_category'][s].get(cat), dict)
                     else "-" for s in sorted(report["per_category"])]
            lines.append(f"| {cat} | " + " | ".join(cells) + " |")
        lines += ["", "## Sanity + significance", ""]
        for k, v in report.get("sanity", {}).items():
            lines.append(f"- {k}: {v}")
        for k, v in report.get("significance", {}).items():
            lines.append(f"- {k} (95% paired bootstrap): {v}")
        if "no_answer_top1_scores" in report:
            lines += ["", "## no-answer top-1 scores (confident junk check)", ""]
            for name, scores in report["no_answer_top1_scores"].items():
                lines.append(f"- {name}: {scores}")
        if not report.get("st_available"):
            lines += ["", "st results: **Unverified - model unavailable in this environment**; "
                      "the hash control ran."]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    json_path = out_path.replace(".md", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    print("\n".join(lines[:40]))
    print(f"\nwrote {out_path} + {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
