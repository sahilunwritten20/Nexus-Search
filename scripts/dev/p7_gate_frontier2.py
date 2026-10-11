"""Frontier against WELL-EVIDENCED answerables (top hit is a fixture-relevant
doc): the false-refusal that actually harms. Also shows the raw rate."""
import os
import sys
import tempfile

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from nexus_search.rag import calibration as cal


def main():
    tmp = tempfile.mkdtemp(prefix="rag-frontier2-")
    storage = hybrid = vs = sync = None
    try:
        storage, hybrid, vs, sync, retriever, queries = cal._build_corpus(
            os.path.join(tmp, "frontier2.db"))
        # rows + relevance of the top hit
        info = {}
        rows = []
        for q in queries:
            out = retriever.retrieve(q.text, k=10)
            top = out.results[0] if out.results else None
            relevant = set(q.relevant or {})
            rows.append(cal.SignalRow(
                query_id=q.query_id, text=q.text, answerable=bool(q.relevant),
                top_bm25=top.bm25_score if top else None,
                top_vector=top.vector_score if top else None,
                top_chunk_id=top.chunk_id if top else None,
                n_results=len(out.results), category=q.category))
            info[q.query_id] = (bool(q.relevant),
                                bool(top and top.chunk_id in relevant))
        for i, text in enumerate(cal.EXTRA_UNANSWERABLE):
            out = retriever.retrieve(text, k=10)
            top = out.results[0] if out.results else None
            qid = f"no-answer-extra-{i}"
            rows.append(cal.SignalRow(
                query_id=qid, text=text, answerable=False,
                top_bm25=top.bm25_score if top else None,
                top_vector=top.vector_score if top else None,
                top_chunk_id=top.chunk_id if top else None,
                n_results=len(out.results), category="no-answer-extra"))
            info[qid] = (False, False)

        dev, test = cal._split(rows, 20260101)

        def rates(subset, bf, vf):
            junk = [r for r in subset if not r.answerable]
            well = [r for r in subset if r.answerable and info[r.query_id][1]]
            raw_ans = [r for r in subset if r.answerable]
            jr = sum(cal._gate_refuses(r, bf, vf) for r in junk)
            wr = sum(cal._gate_refuses(r, bf, vf) for r in well)
            rr = sum(cal._gate_refuses(r, bf, vf) for r in raw_ans)
            return (jr / len(junk) if junk else 0,
                    wr / len(well) if well else 0,
                    rr / len(raw_ans) if raw_ans else 0,
                    len(junk), len(well), len(raw_ans))

        print(f"dev: junk={len([r for r in dev if not r.answerable])} "
              f"well_evidenced={len([r for r in dev if r.answerable and info[r.query_id][1]])} "
              f"answerable={len([r for r in dev if r.answerable])}")
        print(f"test: junk={len([r for r in test if not r.answerable])} "
              f"well_evidenced={len([r for r in test if r.answerable and info[r.query_id][1]])} "
              f"answerable={len([r for r in test if r.answerable])}")
        print("\n-- Pareto on (junk_refusal, well_evidenced_false_refusal) dev --")
        points = []
        for bf in cal._BM25_GRID:
            for vf in cal._VECTOR_GRID:
                jr, wr, rr, *_ = rates(dev, bf, vf)
                points.append((round(jr, 4), round(wr, 4), round(rr, 4), bf, vf))
        pareto = []
        for p in sorted(points, key=lambda x: (-x[0], x[1])):
            if not any(q[0] >= p[0] and q[1] <= p[1] and (q[0], q[1]) != (p[0], p[1])
                       for q in points):
                pareto.append(p)
        print("junk_ref  well_false  raw_false  bm25_floor  vec_floor")
        for p in sorted(pareto, key=lambda x: (-x[0], x[1]))[:20]:
            print(f"{p[0]:<10}{p[1]:<12}{p[2]:<11}{p[3]:<12}{p[4]}")
        print("\n-- specific candidates on dev AND test --")
        for bf, vf in [(17.0, 0.40), (11.0, 0.40), (6.5, 0.40), (5.0, 0.40),
                       (17.0, 0.35), (4.5, 0.40)]:
            d = rates(dev, bf, vf)
            t = rates(test, bf, vf)
            print(f"floors=({bf},{vf}) dev: junk={d[0]:.3f} well_false={d[1]:.3f} "
                  f"raw_false={d[2]:.3f} | test: junk={t[0]:.3f} "
                  f"well_false={t[1]:.3f} raw_false={t[2]:.3f}")
    finally:
        if sync: sync.close()
        if hybrid: hybrid.close()
        if vs: vs.close()
        if storage: storage.close()
        cal._rmtree_retry(tmp)


if __name__ == "__main__":
    main()
