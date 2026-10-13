"""Shared test helper: reset nexus_search.core.api to a bootable state.

Review finding ORDER-1: the RAG/WP6 API test teardowns reload
``nexus_search.core.api`` to leave a clean module behind, but the reload
reads the process environment. After tearDown restores a key-less
environment the module's fail-closed boot check RAISES, so these tests
only pass when some OTHER module leaked NEXUS_ENV/NEXUS_API_KEY into the
process first (e.g. tests/core/test_api.py's module-level setdefault) —
an order dependence that also lets the reload open the repo's default
``nexus_search.db`` when NEXUS_DB is unset (cross-process DB races, stray
DB artifacts in the worktree).

``reset_api()`` reloads the module under a self-contained dev environment
pointed at a throwaway DB, then restores the environment exactly — the
reset never depends on ambient state and never touches shared files.
"""
import importlib
import os
import tempfile

_RESET_ENV = ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB")


def reset_api():
    """Reload nexus_search.core.api boot-safely; returns the module."""
    saved = {k: os.environ.get(k) for k in _RESET_ENV}
    try:
        os.environ["NEXUS_ENV"] = "dev"
        os.environ.pop("NEXUS_API_KEY", None)
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(prefix="api-reset-"),
                                              "reset.db")
        # import AFTER the env is set: a first-ever import executes the
        # module's boot check, which must see the boot-safe env above
        from nexus_search.core import api
        return importlib.reload(api)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
