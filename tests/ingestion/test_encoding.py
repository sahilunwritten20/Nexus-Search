"""Tests for Phase 2 build-out #11 — encoding detection on text/CSV/JSON/HTML."""
import shutil
import tempfile
import unittest
from pathlib import Path

from nexus_search.ingestion.connectors.files import read_csv_file, read_file, read_text_file


class TestEncodingDetection(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_utf8_unchanged(self):
        f = self.dir / "a.txt"
        f.write_text("héllo wörld, café", encoding="utf-8")
        self.assertEqual(read_text_file(f), "héllo wörld, café")

    def test_latin1_detected(self):
        f = self.dir / "a.txt"
        f.write_bytes("héllo wörld, café - naïve".encode("latin-1"))
        out = read_text_file(f)
        self.assertIn("héllo", out)
        self.assertNotIn("�", out)

    def test_utf16_bom_detected(self):
        f = self.dir / "a.txt"
        f.write_bytes("hello world of tests".encode("utf-16"))
        self.assertIn("hello world", read_text_file(f))

    def test_csv_latin1(self):
        f = self.dir / "a.csv"
        f.write_bytes("name,city\nZoë,São Paulo\n".encode("latin-1"))
        out = read_csv_file(f)
        self.assertIn("Zoë", out)
        self.assertIn("São Paulo", out)

    def test_json_latin1(self):
        f = self.dir / "a.json"
        f.write_bytes('{"name": "José", "city": "Montréal"}'.encode("latin-1"))
        out = read_file(f)
        self.assertIn("José", out)

    def test_empty_file(self):
        f = self.dir / "e.txt"
        f.write_bytes(b"")
        self.assertEqual(read_text_file(f), "")

    def test_invalid_bytes_replace_never_raise(self):
        f = self.dir / "bad.txt"
        f.write_bytes(b"\xff\xfe\x41\x42" * 10)
        self.assertIsInstance(read_text_file(f), str)


class TestStreamingIngestion(unittest.TestCase):
    """Large CSVs stream row-by-row; content is identical to a full read."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_large_csv_roundtrip_complete_and_ordered(self):
        f = self.dir / "big.csv"
        rows = [f"id{i},value {i}" for i in range(5000)]  # ~1MB of rows
        f.write_text("id,val\n" + "\n".join(rows), encoding="utf-8")
        out = read_csv_file(f).splitlines()
        self.assertEqual(len(out), 5000)
        self.assertEqual(out[0], "id: id0 | val: value 0")
        self.assertEqual(out[-1], "id: id4999 | val: value 4999")

    def test_streaming_csv_latin1_still_detected(self):
        f = self.dir / "latin.csv"
        f.write_bytes("name,city\nZoë,São\n".encode("latin-1"))
        out = read_csv_file(f)
        self.assertIn("São", out)


if __name__ == "__main__":
    unittest.main()
