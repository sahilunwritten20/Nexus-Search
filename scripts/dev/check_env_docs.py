"""CI lint 1: every env var the code reads must be documented in .env.example
(or be an explicitly allowlisted internal/test-only name).

Fails with a list of undocumented NEXUS_* variables. Run: python scripts/dev/check_env_docs.py
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SRC = os.path.join(ROOT, "nexus_search")

# internal/test-only knobs that intentionally do not belong in .env.example
ALLOWLIST = {
    "NEXUS_RUN_MODEL_TESTS",    # test-only gate (documented in .env.example comment already)
    "NEXUS_GRAPH_BENCH_SMOKE",  # benchmark harness mode (test-only)
    "NEXUS_RUN_GRAPH_BENCH",    # benchmark harness gate (test-only)
    "NEXUS_OCR",                # documented in .env.example (kept here in case of drift)
    "NEXUS_STOPWORDS",          # tokenizer knob, documented in tokenizer docstring
}

READ_PATTERN = re.compile(
    r"""os\.environ(?:\.get)?\(\s*["'](NEXUS_[A-Z0-9_]+)["']|"""
    r"""os\.getenv\(\s*["'](NEXUS_[A-Z0-9_]+)["']|"""
    r"""os\.environ\[[\"'](NEXUS_[A-Z0-9_]+)[\"']\]""")

DEFAULT_PATTERN = re.compile(r"""NEXUS_[A-Z0-9_]+""")


def vars_in_code():
    found = set()
    for dirpath, _dirs, files in os.walk(SRC):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            text = open(path, encoding="utf-8").read()
            for match in READ_PATTERN.finditer(text):
                found.add(next(g for g in match.groups() if g))
            # env-var-name indirection: ("NEXUS_X", "field") style tables
            for match in re.finditer(r"""["'](NEXUS_[A-Z0-9_]+)["']""", text):
                found.add(match.group(1))
    return found


def vars_in_docs():
    doc = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    return set(re.findall(r"NEXUS_[A-Z0-9_]+", doc))


def main() -> int:
    code_vars = vars_in_code()
    doc_vars = vars_in_docs()
    missing = sorted(v for v in code_vars - doc_vars - ALLOWLIST
                     if not v.startswith("NEXUS_RUN_"))
    if missing:
        print("UNDOCUMENTED env vars (read in code, absent from .env.example):")
        for v in missing:
            print(f"  {v}")
        return 1
    print(f"env docs consistent: {len(code_vars & doc_vars)} documented vars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
