"""WP13 differential baseline generator (audit B6 regression evidence).

Runs the WP11 head's `_detect_encoding` (git worktree at 3511b86) over the
exact WP13 encoding table (tests/ingestion/wp13_encoding_samples.py) and
records which (lang, codec, variant) rows round-tripped EXACTLY. The
committed fixture (tests/ingestion/wp13_wp11_baseline.json) is the
no-regression contract: every row WP11 decoded correctly, WP13 must decode
correctly too (WP13 additionally fixes rows WP11 got wrong).

Usage (from the WP13 repo root, with the WP11 worktree checked out):
    python scripts/dev/wp13/gen_wp13_fixture.py <wp11_worktree> [-o out.json]
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def load_samples(repo_root: Path):
    spec = importlib.util.spec_from_file_location(
        "wp13_encoding_samples", repo_root / "tests" / "ingestion" / "wp13_encoding_samples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wp11_worktree", help="path to a git worktree at 3511b86 (WP11 head)")
    ap.add_argument("-o", "--out",
                   default=str(REPO / "tests" / "ingestion" / "wp13_wp11_baseline.json"))
    args = ap.parse_args()

    wt = Path(args.wp11_worktree).resolve()
    if not (wt / "nexus_search" / "ingestion" / "connectors" / "files.py").exists():
        print(f"not a nexus-search worktree: {wt}", file=sys.stderr)
        return 1

    samples = load_samples(REPO)

    # Import the WP11 files module from the worktree, under a private name so
    # a WP13 checkout of the same package can never shadow it.
    sys.path.insert(0, str(wt))
    wp11_files = importlib.import_module("nexus_search.ingestion.connectors.files")
    detect = getattr(wp11_files, "_detect_encoding")
    import charset_normalizer
    print(f"WP11 head _detect_encoding from {wt}")
    print(f"charset-normalizer {charset_normalizer.__version__}")

    passed, failed = [], []
    for lang, codec, variant, text in samples.table_entries():
        raw = text.encode(codec)
        chosen = detect(raw)
        decoded = raw.decode(chosen, errors="replace")
        row = {"lang": lang, "codec": codec, "variant": variant,
               "bytes": len(raw), "wp11_encoding": chosen}
        (passed if decoded == text else failed).append(row)

    fixture = {
        "generator": "scripts/dev/wp13/gen_wp13_fixture.py",
        "wp11_head": "3511b86",
        "charset_normalizer": charset_normalizer.__version__,
        "contract": ("every row listed under 'passed' MUST round-trip exactly in "
                    "later work packages; rows under 'failed' are the headroom "
                    "WP13 is allowed (and expected) to fix"),
        "passed": passed,
        "failed": failed,
    }
    out = Path(args.out)
    out.write_text(json.dumps(fixture, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print(f"\nWP11 baseline: {len(passed)} passed, {len(failed)} failed "
          f"of {len(passed) + len(failed)} table rows")
    for row in failed:
        print(f"  WP11 FAILED: {row['lang']}|{row['codec']}|{row['variant']} "
              f"({row['bytes']}B) -> {row['wp11_encoding']}")
    print(f"fixture written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
