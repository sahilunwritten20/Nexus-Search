"""WP14-6a — the FastAPI app version comes from nexus_search.__version__
(single source of truth; was a drifted hard-coded "0.4.0" while the release
tag was already v0.6.0)."""
import os
import unittest

os.environ.setdefault("NEXUS_ENV", "dev")
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

import nexus_search
from nexus_search.core import api


class TestVersion(unittest.TestCase):
    def test_version_is_set(self):
        self.assertTrue(nexus_search.__version__)
        self.assertEqual(nexus_search.__version__, "0.6.0")

    def test_api_uses_package_version(self):
        self.assertEqual(api.app.version, nexus_search.__version__)


if __name__ == "__main__":
    unittest.main()
