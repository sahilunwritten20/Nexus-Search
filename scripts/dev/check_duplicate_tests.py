"""CI lint 2: no duplicate test-function names within a test module.

A duplicate `def test_x` silently shadows the earlier one — the suite
under-reports (BUG-13 was exactly this). AST-based, fast, no execution.
Run: python scripts/dev/check_duplicate_tests.py
"""
import ast
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
TESTS = os.path.join(ROOT, "tests")


def main() -> int:
    duplicates = []
    for dirpath, _dirs, files in os.walk(TESTS):
        for name in files:
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            path = os.path.join(dirpath, name)
            tree = ast.parse(open(path, encoding="utf-8").read())
            seen = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name.startswith("test_"):
                    if node.name in seen:
                        duplicates.append(f"{os.path.relpath(path, ROOT)}:{node.lineno}"
                                          f" duplicate test_{node.name}")
                    seen.add(node.name)
    if duplicates:
        print("DUPLICATE TEST NAMES (later def shadows the earlier one):")
        for d in duplicates:
            print(f"  {d}")
        return 1
    print("no duplicate test function names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
