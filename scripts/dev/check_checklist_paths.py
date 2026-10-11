"""CI lint: every test file named in docs/PHASES_1-6_CHECKLIST.md must exist
(WP14-5: the checklist named files that never existed — test_ab.py,
test_query_parser.py, test_hybrid*.py and a nonexistent "robots" file).

Two token forms are checked:
- full paths: `tests/.../test_x.py` (or with `*` globs) — must exist / match
- bare names:  `test_x.py` — must match at least one file under tests/

Run: python scripts/dev/check_checklist_paths.py
"""
import glob
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
CHECKLIST = os.path.join(ROOT, "docs", "PHASES_1-6_CHECKLIST.md")

FULL_PATH = re.compile(r"tests/[A-Za-z0-9_/.]+\.py")
BARE_NAME = re.compile(r"\btest_[a-z0-9_]+\.py\b")


def all_test_files():
    out = set()
    for dirpath, _dirs, files in os.walk(os.path.join(ROOT, "tests")):
        for name in files:
            if name.endswith(".py") and name.startswith("test"):
                out.add(name)
    return out


def main() -> int:
    text = open(CHECKLIST, encoding="utf-8").read()
    full = sorted(set(FULL_PATH.findall(text)))
    known = all_test_files()
    # bare names that are not part of an already-checked full path
    consumed = {p.rsplit("/", 1)[-1] for p in full}
    bare = sorted(set(BARE_NAME.findall(text)) - consumed)
    if not full and not bare:
        print("no test paths found in the checklist — regex broken?")
        return 1
    missing = []
    for token in full:
        if "*" in token:
            if not glob.glob(os.path.join(ROOT, *token.split("/"))):
                missing.append(f"{token} (glob matches nothing)")
        elif not os.path.isfile(os.path.join(ROOT, *token.split("/"))):
            missing.append(token)
    for name in bare:
        if name not in known:
            missing.append(f"{name} (no such file anywhere under tests/)")
    if missing:
        print("checklist names test files that do not exist:")
        for m in missing:
            print(f"  {m}")
        return 1
    print(f"checklist test files all exist: {len(full)} full paths, "
          f"{len(bare)} bare names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
