"""Tests to ensure all Python files are syntactically valid on Python 3.12."""

import ast
import os
import pytest


def collect_python_files(root_dirs):
    """Collect all .py files from the given root directories."""
    files = []
    for root_dir in root_dirs:
        for dp, dn, fn in os.walk(root_dir):
            for f in fn:
                if f.endswith('.py'):
                    files.append(os.path.join(dp, f))
    return files


@pytest.mark.parametrize("filepath", collect_python_files(["nexus_search", "tests", "scripts"]))
def test_py312_syntax(filepath):
    """Test that each Python file parses correctly with Python 3.12 grammar."""
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()
    # Use feature_version=(3, 12) to ensure 3.12 compatibility
    ast.parse(source, filename=filepath, feature_version=(3, 12))