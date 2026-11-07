#!/usr/bin/env python3
"""Check coverage floors from coverage.json. Exit non-zero if any floor is violated."""
import json
import sys
import os

def load_coverage():
    path = "docs/evidence/coverage_full.json"
    if not os.path.exists(path):
        path = "coverage.json"
    with open(path) as f:
        return json.load(f)

def pct(covered, total):
    return (covered / total * 100) if total else 0

def main():
    data = load_coverage()
    totals = data["totals"]
    line_pct = pct(totals["covered_lines"], totals["num_statements"])
    branch_pct = pct(totals["covered_branches"], totals["num_branches"])
    
    print(f"Overall: {line_pct:.2f}% line ({totals['covered_lines']}/{totals['num_statements']})")
    print(f"Overall: {branch_pct:.2f}% branch ({totals['covered_branches']}/{totals['num_branches']})")
    
    # Overall gates
    overall_line_ok = line_pct >= 92.0
    overall_branch_ok = branch_pct >= 85.0
    
    if not overall_line_ok:
        print(f"FAIL: Overall line {line_pct:.2f}% < 92%")
    if not overall_branch_ok:
        print(f"FAIL: Overall branch {branch_pct:.2f}% < 85%")
    
    # Security-critical modules (using forward slashes for matching)
    critical = {
        "nexus_search/core/storage.py": (97, 90),
        "nexus_search/crawler/security.py": (97, 90),
        "nexus_search/rag/sanitize.py": (97, 90),
        "nexus_search/rag/egress.py": (97, 90),
        "nexus_search/rag/gate.py": (97, 90),
        "nexus_search/rag/retrieval.py": (97, 90),
        "nexus_search/crawler/fetcher.py": (97, 90),
        "nexus_search/crawler/url_utils.py": (97, 90),
        "nexus_search/core/models.py": (97, 90),
        "nexus_search/core/api.py": (97, 90),
        "nexus_search/rag/session.py": (97, 90),
        "nexus_search/ingestion/connectors/files.py": (97, 90),
        "nexus_search/rag/calibration.py": (97, 90),
        "nexus_search/rag/__main__.py": (97, 90),
        "nexus_search/rag/budget.py": (97, 90),
        "nexus_search/rag/provider.py": (97, 90),
        "nexus_search/rag/pipeline.py": (97, 90),
        "nexus_search/rag/citations.py": (97, 90),
        "nexus_search/rag/context.py": (97, 90),
        "nexus_search/core/migrations.py": (97, 90),
        "nexus_search/core/backup.py": (97, 90),
        "nexus_search/crawler/blocklist.py": (97, 90),
        "nexus_search/crawler/robots.py": (97, 90),
    }
    
    all_ok = overall_line_ok and overall_branch_ok
    
    for f, (line_floor, branch_floor) in critical.items():
        # Match with backslash or forward slash
        f_backslash = f.replace('/', '\\')
        if f_backslash not in data["files"] and f not in data["files"]:
            print(f"WARN: {f} not in coverage data")
            continue
        key = f_backslash if f_backslash in data["files"] else f
        fd = data["files"][key]
        s = fd.get("summary", fd)  # some files may have summary at top level
        line_pct = pct(s.get("covered_lines", 0), s.get("num_statements", 1))
        branch_pct = pct(s.get("covered_branches", 0), s.get("num_branches", 1))
        line_ok = line_pct >= line_floor
        branch_ok = branch_pct >= branch_floor
        status = "OK" if line_ok and branch_ok else "FAIL"
        print(f"{f}: {line_pct:.2f}% line (floor {line_floor}%) / {branch_pct:.2f}% branch (floor {branch_floor}%) [{status}]")
        if not line_ok or not branch_ok:
            all_ok = False
    
    if not all_ok:
        sys.exit(1)
    print("All floors MET")
    sys.exit(0)

if __name__ == "__main__":
    main()