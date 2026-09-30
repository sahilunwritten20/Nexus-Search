"""Regenerate tests/golden/keyword_baseline.json (WP0 baseline).

Run from the repo root:
    python scripts/dev/regen_golden.py

The golden file is the pre-remediation keyword-mode output. tests/core/
test_golden_keyword.py compares live output to it; any intentional ranking
change must regenerate this file in the same commit that changes behavior.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tests.golden.golden_corpus import golden_output  # noqa: E402


def main() -> int:
    out_path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "tests", "golden", "keyword_baseline.json")
    with tempfile.TemporaryDirectory() as tmp:
        data = golden_output(os.path.join(tmp, "golden.db"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    n = sum(len(q["results"]) for q in data["queries"])
    print(f"wrote {out_path}: {len(data['queries'])} queries, {n} results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
