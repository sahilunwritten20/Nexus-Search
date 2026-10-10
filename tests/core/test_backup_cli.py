"""WP14-7 — core/backup.py CLI (main) was 0%-covered (module 41%).
The backup tool is ops-critical: a broken CLI discovered during an incident
is the worst possible time. Pins: success path, missing-db exit code,
--with-frontier with and without the frontier file."""
import os
import tempfile
import unittest

from nexus_search.core import backup


class TestBackupCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp14_backup_")
        self.out = os.path.join(self.tmp, "backups")
        self.db = os.path.join(self.tmp, "nexus_search.db")
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE documents (doc_id TEXT PRIMARY KEY, content TEXT)")
        conn.execute("INSERT INTO documents VALUES ('d1', 'hello backup')")
        conn.commit()
        conn.close()

    def test_main_backs_up_and_reports(self):
        rc = backup.main(["--db", self.db, "--out", self.out])
        self.assertEqual(rc, 0)
        files = [f for f in os.listdir(self.out) if f.endswith(".db")]
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith("nexus_search-"))

    def test_main_missing_db_returns_1(self):
        rc = backup.main(["--db", os.path.join(self.tmp, "nope.db"),
                          "--out", self.out])
        self.assertEqual(rc, 1)

    def test_main_with_frontier_file(self):
        frontier = os.path.splitext(self.db)[0] + "_frontier.db"
        import sqlite3
        conn = sqlite3.connect(frontier)
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
        conn.close()
        rc = backup.main(["--db", self.db, "--out", self.out,
                          "--with-frontier"])
        self.assertEqual(rc, 0)
        files = sorted(f for f in os.listdir(self.out) if f.endswith(".db"))
        self.assertEqual(len(files), 2)
        self.assertTrue(any(f.startswith("nexus_search_frontier-") for f in files))

    def test_main_with_frontier_absent_skips(self):
        rc = backup.main(["--db", self.db, "--out", self.out,
                          "--with-frontier"])
        self.assertEqual(rc, 0)  # skipped, not failed
        files = [f for f in os.listdir(self.out) if f.endswith(".db")]
        self.assertEqual(len(files), 1)  # only the main DB


if __name__ == "__main__":
    unittest.main()
