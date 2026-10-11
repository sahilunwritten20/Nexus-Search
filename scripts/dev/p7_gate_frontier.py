"""Pareto frontier of (unanswerable refusal, answerable false-refusal) for
the AND-rule over the floor grid — evidence for the restated target."""
import os
import sys
import tempfile

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from nexus_search.rag import calibration as cal


def main():
    tmp = tempfile.mkdtemp(prefix="rag-frontier-")
    storage = hybrid = vs = sync = None
    try:
        storage, hybrid, vs, sync, retriever, queries = cal._build_corpus(
            os.path.join(tmp, "frontier.db"))
        rows = cal.collect_signals(retriever, queries)
        dev, test = cal._split(rows, cal.SPLIT_SEED_CALIB if hasattr(cal, "SPLIT_SEED_CALIB") else 20260101)
        points = []
        for bf in cal._BM25_GRID:
            for vf in cal._VECTOR_GRID:
                d = cal.evaluate_floors(dev, bf, vf)
                points.append((round(d["unanswerable_refusal_rate"], 4),
                               round(d["answerable_false_refusal_rate"], 4),
                               bf, vf, d["unanswerable_total"], d["answerable_total"]))
        # Pareto: max u_refusal, min false_refusal
        pareto = []
        for p in sorted(points, key=lambda x: (-x[0], x[1])):
            if not any(q[0] >= p[0] and q[1] <= p[1] and (q[0], q[1]) != (p[0], p[1])
                       for q in points):
                pareto.append(p)
        print("dev sizes: unanswerable=%d answerable=%d" % (pareto[0][4], pareto[0][5]))
        print("u_refuse  false_ref  bm25_floor  vec_floor")
        for p in sorted(pareto, key=lambda x: (-x[0], x[1]))[:25]:
            print(f"{p[0]:<10}{p[1]:<11}{p[2]:<12}{p[3]}")
    finally:
        if sync: sync.close()
        if hybrid: hybrid.close()
        if vs: vs.close()
        if storage: storage.close()
        cal._rmtree_retry(tmp)


if __name__ == "__main__":
    main()
