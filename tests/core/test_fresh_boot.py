"""Fresh-process boot semantics (WP11 / P0-1).

The reload-based tests in test_api.py reuse an already-populated module
namespace, so they cannot catch module-level NameError ordering bugs. These
tests boot the app in a REAL subprocess (a fresh interpreter, no reload,
no shared namespace) and assert the read-auth contract end to end.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Runs INSIDE the fresh interpreter: import the app module cold (the crash
# under test happens at import time), then probe /search both ways.
_PROBE = """
import os
import nexus_search.core.api as api
from fastapi.testclient import TestClient

client = TestClient(api.app)
no_key = client.get("/search", params={"q": "x"}).status_code
with_key = client.get("/search", params={"q": "x"},
                      headers={"X-API-Key": os.environ["NEXUS_API_KEY"]}).status_code
print("BOOT_RESULT", no_key, with_key)
"""

_SUBPROCESS_TIMEOUT = 120  # cold interpreter + fastapi imports, generous


def _boot_fresh(env_extra: dict) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "NEXUS_EMBEDDER": "hash:384",
        "NEXUS_DB": os.path.join(_BOOT_FRESH_DIR, "fresh.db"),
        **env_extra,
    }
    return subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env, cwd=str(REPO_ROOT), capture_output=True, text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )


import tempfile
_BOOT_FRESH_DIR = tempfile.mkdtemp(prefix="nexus_fresh_boot_")


class TestFreshProcessReadAuth(unittest.TestCase):
    """P0-1: NEXUS_REQUIRE_AUTH_FOR_READS=1 must boot and gate reads in a
    fresh process. Pre-fix this crashed at import with NameError because
    `_READ_AUTH` referenced `require_api_key` before its definition."""

    def test_read_auth_boots_and_gates_in_fresh_process(self):
        result = _boot_fresh({
            "NEXUS_ENV": "production",
            "NEXUS_API_KEY": "fresh-boot-key",
            "NEXUS_REQUIRE_AUTH_FOR_READS": "1",
        })
        self.assertEqual(
            result.returncode, 0,
            f"fresh-process import failed:\n{result.stderr[-2000:]}")
        marker = [ln for ln in result.stdout.splitlines()
                  if ln.startswith("BOOT_RESULT")]
        self.assertEqual(len(marker), 1, f"unexpected stdout: {result.stdout!r}")
        no_key, with_key = (int(v) for v in marker[0].split()[1:3])
        self.assertEqual(no_key, 401, "reads must be gated without the key")
        self.assertEqual(with_key, 200, "reads must pass with the valid key")

    def test_reads_stay_open_in_fresh_process_without_flag(self):
        # the flag unset must keep reads open (default contract) and, more
        # to the point, must not regress the plain production boot path
        result = _boot_fresh({
            "NEXUS_ENV": "production",
            "NEXUS_API_KEY": "fresh-boot-key",
        })
        self.assertEqual(
            result.returncode, 0,
            f"fresh-process import failed:\n{result.stderr[-2000:]}")
        marker = [ln for ln in result.stdout.splitlines()
                  if ln.startswith("BOOT_RESULT")]
        self.assertEqual(len(marker), 1, f"unexpected stdout: {result.stdout!r}")
        no_key, with_key = (int(v) for v in marker[0].split()[1:3])
        self.assertEqual(no_key, 200)
        self.assertEqual(with_key, 200)


if __name__ == "__main__":
    unittest.main()
