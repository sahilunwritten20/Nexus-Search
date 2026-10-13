"""Session baseline for the whole test suite (review ORDER-1).

``nexus_search.core.api`` runs its fail-closed boot check at IMPORT time.
Every API test module sets its own env and RELOADS the module, but the
FIRST import in a fresh process executed under whatever ambient
environment happened to exist — so single-file runs failed unless some
OTHER module had already leaked NEXUS_ENV=dev into the process
(tests/core/test_api.py's module-level setdefault). conftest.py runs
before any test module: give the session an explicit, deterministic
baseline instead of an accidental one.

Boot-policy tests set their own env and reload, so they are unaffected.
setdefault (not overwrite): an operator who deliberately exports
NEXUS_ENV=production keeps their choice.

NEXUS_DB baseline: without it, every test boot that doesn't set its own
DB opens/creates ``nexus_search.db`` IN THE REPO ROOT (CI artifact
pollution + cross-run/cross-process races — observed live during this
review). A per-session throwaway default keeps those boots off shared
files; tests that set their own NEXUS_DB (most) are unaffected.
"""
import os
import tempfile

os.environ.setdefault("NEXUS_ENV", "dev")
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")
if not os.environ.get("NEXUS_DB") or \
        os.environ["NEXUS_DB"] == "nexus_search.db":
    os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(prefix="nxs-test-"),
                                          "session.db")
