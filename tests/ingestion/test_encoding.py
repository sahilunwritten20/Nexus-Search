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


class TestShortWesternEncoding(unittest.TestCase):
    """P0-4: SHORT cp1252 files were misdetected by charset-normalizer even
    on the pinned 3.5.1 ('Café' -> utf_16_be, 'São Paulo' -> big5, i.e.
    mojibake). Detection order must be BOM -> strict UTF-8 -> detector, with
    a cp1252 preference when the detector's guess is an odd/non-Western
    misfire on a short sample. CJK must NOT regress."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _roundtrip(self, text, encoding):
        f = self.dir / f"{encoding.replace('_', '-')}.txt"
        f.write_bytes(text.encode(encoding))
        return read_text_file(f)

    def test_short_cp1252_cafe(self):
        self.assertEqual(self._roundtrip("Café", "cp1252"), "Café")

    def test_short_cp1252_sao_paulo(self):
        self.assertEqual(self._roundtrip("São Paulo", "cp1252"), "São Paulo")

    def test_short_cp1252_zurich_strasse(self):
        self.assertEqual(self._roundtrip("Zürich Straße Müller", "cp1252"),
                         "Zürich Straße Müller")

    def test_short_cp1252_sentence(self):
        text = "Café Mug på Ångström — naïve façade"
        self.assertEqual(self._roundtrip(text, "cp1252"), text)

    def test_longer_shift_jis_sample(self):
        text = "東京都渋谷区で機械学習の研究会が開催されました。" * 8
        self.assertEqual(self._roundtrip(text, "shift_jis"), text)

    def test_longer_gbk_sample(self):
        text = "北京市举办人工智能与机器学习技术研讨会。" * 8
        self.assertEqual(self._roundtrip(text, "gbk"), text)

    def test_short_cjk_survives_cp1252_preference(self):
        """The cp1252 preference must not eat real short CJK files: a
        CJK-family guess is kept (or better), never flipped to cp1252.
        Note the honest limit, pinned by the pre-fix run: for very short
        samples even the RIGHT family can be statistically ambiguous
        (GBK bytes can decode to Hangul under cp949 with the same CJK
        density), so the guarantee here is 'stays CJK text', not 'exact
        roundtrip' — the longer-sample tests above carry the exact bar."""
        def cjk_share(s):
            cjk = sum(1 for ch in s if "\u3040" <= ch <= "\u30ff"
                      or "\u3400" <= ch <= "\u9fff"
                      or "\uac00" <= ch <= "\ud7a3"
                      or "\uf900" <= ch <= "\ufaff")
            return cjk / max(len(s), 1)

        for encoding, text in (
            ("shift_jis", "こんにちは世界"),
            ("gbk", "你好世界"),
            ("big5", "這是一個測試"),
        ):
            with self.subTest(encoding=encoding):
                out = self._roundtrip(text, encoding)
                self.assertGreaterEqual(
                    cjk_share(out), 0.5,
                    f"{encoding} sample decoded to Latin mojibake: {out!r}")
        # Shift-JIS short samples happen to be detected exactly — keep them
        self.assertIn("こんにちは世界",
                      self._roundtrip("こんにちは世界", "shift_jis"))


class TestLegacySingleByteScripts(unittest.TestCase):
    """WP12-B6 (audit R6): cp1251/koi8_r/cp1253/cp1254/cp1250 text is
    usually VALID cp1252 bytes too, so the cp1252 preference mojibaked
    five writing systems. Candidates are scored by script coherence
    (strict decode, one alphabet, family-specific letters for the Latin
    families)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _roundtrip(self, text, encoding):
        f = self.dir / f"{encoding.replace('_', '-')}.txt"
        f.write_bytes(text.encode(encoding))
        return read_text_file(f)

    def test_russian_cp1251_roundtrip(self):
        for text in ("Привет мир, как дела?", "Привет мир! " * 40):
            with self.subTest(len=len(text)):
                self.assertEqual(self._roundtrip(text, "cp1251"), text)

    def test_russian_koi8_r_roundtrip(self):
        for text in ("Привет мир, как дела?", "Привет мир! " * 40):
            with self.subTest(len=len(text)):
                self.assertEqual(self._roundtrip(text, "koi8_r"), text)

    def test_greek_cp1253_roundtrip(self):
        for text in ("Καλημέρα κόσμε;", "Καλημέρα κόσμε! " * 40):
            with self.subTest(len=len(text)):
                self.assertEqual(self._roundtrip(text, "cp1253"), text)

    def test_turkish_cp1254_roundtrip(self):
        for text in ("Günaydın dünya?", "Günaydın dünya! " * 40):
            with self.subTest(len=len(text)):
                self.assertEqual(self._roundtrip(text, "cp1254"), text)

    def test_polish_cp1250_roundtrip(self):
        for text in ("Żółć gęślą, jaźń?", "Żółć gęślą jaźń! " * 40):
            with self.subTest(len=len(text)):
                self.assertEqual(self._roundtrip(text, "cp1250"), text)

    def test_cp1252_latin_symbols_not_flipped(self):
        """The Latin-family rule must not steal genuine cp1252 text: a
        superscript byte (m³) or a French œ (byte 0x9C) has no INTERIOR
        script-specific letter under the legacy codecs — cp1252 keeps them."""
        for text in ("Der Tank fasst 50 m³ Wasser.", "un cœur simple, sœur aînée"):
            with self.subTest(text=text):
                self.assertEqual(self._roundtrip(text, "cp1252"), text)


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
