"""WP13 (independent re-review, blocking B6 follow-up): encoding regressions.

The WP12-B6 script-coherence fix (commit 6097aad) mojibaked files that
WP11 decoded correctly:

  - Korean euc_kr/cp949 WITH spaces, GBK with spaces/digits -> a legacy
    single-byte codec won the new "_legacy_script_codec > cjk + 0.10"
    comparison because the two shares used DIFFERENT denominators
    (_cjk_share divided by ALL chars incl. spaces; the legacy share by
    LETTERS ONLY) — a spaced Korean sentence scored ~0.8 vs a flat 1.0
    for the wrong codec.
  - Danish/French/Dutch cp1252 with 2+ of one colliding letter
    (ø->ų, û->ū, ë->ė) -> cp1257: the Baltic specific-letter set included
    letters whose cp1257 byte is a REAL cp1252 letter (the collision
    exclusion applied to the Central-European and Turkish sets only).
  - Russian koi8_r paragraph -> cp1257 via the same colliding letters
    (koi8_r lowercase а/л/ш/ы land on į/ė/ų/ū).

These tests pin the WP13 contract:
  1. the full table (reviewer samples + every writing system x codec x
     length x spaces/digits/punctuation variants) round-trips EXACTLY;
  2. DIFFERENTIAL: every (text, codec) row the WP11 head (3511b86)
     decoded correctly (fixture: wp13_wp11_baseline.json) still decodes
     correctly — no regressions allowed;
  3. DETECTOR-INDEPENDENCE: with charset_normalizer.from_bytes stubbed
     to fake top guesses (cp1252, cp949, gb18030, mac_latin2, utf_16_be,
     big5) the outputs stay correct for every row whose codec family is
     represented in the fake list — the decision must be carried by
     script coherence and shared denominators, not by one detector
     version's ranking. Japanese rows use the same six tops plus cp932 /
     euc_jis_2004 in the list: without a Japanese codec in the candidate
     list the correct answer does not exist (all wrong CJK decodes are
     byte-valid) — that limit is documented, not guessed around;
  4. cp1257 is accepted only as the detector's own top guess or on
     NON-COLLIDING evidence (Ø/ø at bytes 0xA8/0xB8, where cp1252 has
     the symbols ¨/¸) — never via letters that collide with cp1252.

The WP12-B6 tests in test_encoding.py (short cp1252, Russian/Greek/
Turkish/Polish round-trips, Latin-symbol guards) must stay green too.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from nexus_search.ingestion.connectors.files import _detect_encoding, read_text_file

from .wp13_encoding_samples import entry_key, table_entries

FIXTURE_PATH = Path(__file__).with_name("wp13_wp11_baseline.json")

# The fake top guesses (reviewer list: observed misfire classes).
FAKE_TOPS = ("cp1252", "cp949", "gb18030", "mac_latin2", "utf_16_be", "big5")
# Japanese codec-family members to append for Japanese rows (see docstring).
JP_EXTRAS = ("cp932", "euc_jis_2004")

LATVIAN_CP1257 = (
    "Latvijas Universitāte rīko starptautisku konferenci par "
    "mākslīgo intelektu un datu zinātni. Dalībnieki apspriedīs "
    "jaunākās tehnoloģijas."
)
NORDIC_CP1257 = (
    "Søren Kierkegård skrev om tro og tvivl i København, "
    "hvor æbletræer blomstrer om foråret."
)
DANISH_CP1252 = (
    "Det danske sprog har bogstaverne æ, ø og å. "
    "Blåbærgrød og rødgrød med fløde."
)


def _fake_matches(top, extras=()):
    """A fake charset_normalizer result list: `top` first, then the rest."""
    rest = [t for t in FAKE_TOPS if t != top]
    order = [top] + rest + list(extras)
    return [SimpleNamespace(encoding=e) for e in order]


class TestWp13EncodingTable(unittest.TestCase):
    """Every row of the table must round-trip EXACTLY: encode the text
    with its codec, detect the bytes, decode — byte-equal to the source."""

    def test_every_row_round_trips_exactly(self):
        for lang, codec, variant, text in table_entries():
            with self.subTest(lang=lang, codec=codec, variant=variant):
                raw = text.encode(codec)
                out = raw.decode(_detect_encoding(raw), errors="replace")
                self.assertEqual(out, text)

    def test_connector_path_round_trips(self):
        """End-to-end through the real reader (read_text_file), not just
        the detection function."""
        self.dir = Path(tempfile.mkdtemp())
        try:
            cases = [
                ("korean_spaced.txt", "한국어 텍스트입니다 테스트 문장", "euc_kr"),
                ("gbk_digits.txt",
                 "2024年 我们 发布了 3 个 新 版本 , 欢迎 下载 使用 .", "gbk"),
                ("danish.txt",
                 "Det danske sprog har bogstaverne æ, ø og å. "
                 "Blåbærgrød og rødgrød med fløde.", "cp1252"),
                ("french.txt",
                 "Il est sûr que ce fruit est mûr et que la sûreté est assurée.",
                 "cp1252"),
                ("dutch.txt",
                 "Zoë en Chloë reisden naar België voor een geëerd concert.",
                 "cp1252"),
                ("russian.txt",
                 "В 2024 году наша команда выпустила три новых версии "
                 "поисковой системы. Мы улучшили скорость индексации.",
                 "koi8_r"),
            ]
            for fname, text, codec in cases:
                with self.subTest(file=fname):
                    f = self.dir / fname
                    f.write_bytes(text.encode(codec))
                    self.assertEqual(read_text_file(f), text)
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)


class TestWp13DifferentialVsWp11(unittest.TestCase):
    """No-regression contract vs the WP11 head (3511b86): every table row
    that WP11 decoded correctly (recorded in the committed fixture by
    scripts/dev/wp13/gen_wp13_fixture.py) must still decode correctly."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        texts = {entry_key(l, c, v): t for l, c, v, t in table_entries()}
        cls.rows = [(row, texts[entry_key(row["lang"], row["codec"], row["variant"])])
                    for row in cls.fixture["passed"]]

    def test_wp11_correct_rows_still_round_trip(self):
        self.assertGreater(len(self.rows), 40,
                           "fixture must carry the full WP11 pass set")
        for row, text in self.rows:
            key = entry_key(row["lang"], row["codec"], row["variant"])
            with self.subTest(row=key):
                raw = text.encode(row["codec"])
                out = raw.decode(_detect_encoding(raw), errors="replace")
                self.assertEqual(out, text)


class TestWp13DetectorIndependence(unittest.TestCase):
    """Stub charset_normalizer.from_bytes with fake top guesses; the
    correct decode must not depend on which guess leads the list."""

    def _assert_roundtrip_under_stub(self, text, codec, top, extras=()):
        raw = text.encode(codec)
        with mock.patch("charset_normalizer.from_bytes",
                        return_value=_fake_matches(top, extras)):
            out = raw.decode(_detect_encoding(raw), errors="replace")
        self.assertEqual(out, text)

    def test_all_rows_survive_every_fake_top_guess(self):
        for lang, codec, variant, text in table_entries():
            extras = JP_EXTRAS if lang == "japanese" else ()
            for top in FAKE_TOPS:
                with self.subTest(lang=lang, codec=codec, variant=variant,
                                  fake_top=top):
                    self._assert_roundtrip_under_stub(text, codec, top, extras)

    def test_fake_list_with_only_wrong_guesses_is_honest(self):
        """When the candidate list contains NO codec that can decode the
        bytes (a Western list for CJK bytes), detection must fall back
        honestly — never claim a script-coherent legacy codec for them."""
        raw = "한국어 텍스트입니다 테스트 문장".encode("euc_kr")
        western_only = [SimpleNamespace(encoding=e)
                        for e in ("cp1252", "mac_latin2", "cp1257", "cp1250")]
        with mock.patch("charset_normalizer.from_bytes",
                        return_value=western_only):
            enc = _detect_encoding(raw)
        out = raw.decode(enc, errors="replace")
        # Honest limit: with no CJK codec offered, the bytes cannot decode
        # correctly — but they must not be stolen by a script-coherent
        # legacy decode either (the B6 regression shape).
        self.assertNotIn(enc, ("koi8_r", "cp1250", "cp1251", "cp1253",
                               "cp1254", "cp1257"))


class TestWp13Cp1257Rules(unittest.TestCase):
    """cp1257's letters are almost all cp1252-colliding, so letter
    evidence must never auto-select it; it is accepted only as the
    detector's own top guess, or via the non-colliding Ø/ø evidence
    (bytes 0xA8/0xB8 = ¨/¸ in cp1252)."""

    def test_cp1257_accepted_as_detector_top_guess(self):
        for text in (LATVIAN_CP1257, NORDIC_CP1257):
            with self.subTest(text=text[:24]):
                raw = text.encode("cp1257")
                with mock.patch("charset_normalizer.from_bytes",
                                return_value=_fake_matches("cp1257")):
                    self.assertEqual(_detect_encoding(raw), "cp1257")

    def test_cp1257_nordic_evidence_survives_a_cp1252_top_guess(self):
        """Ø/ø (bytes 0xA8/0xB8) are real cp1257 evidence: with cp1252
        leading the list the baltic candidate must still win via the
        script-coherence scan (detector rank is only tie-break 1)."""
        raw = NORDIC_CP1257.encode("cp1257")
        stub = [SimpleNamespace(encoding=e)
                for e in ("cp1252", "cp1257", "cp1250")]
        with mock.patch("charset_normalizer.from_bytes", return_value=stub):
            self.assertEqual(_detect_encoding(raw), "cp1257")

    def test_cp1252_nordic_text_never_flips_to_cp1257(self):
        """The B6 regression pin: Danish cp1252 bytes (ø=0xF8 -> ų, ë=0xEB
        -> ė, û=0xFB -> ū under cp1257) must keep their cp1252 read even
        when cp1257 sits right behind cp1252 in the guess list."""
        for text in (DANISH_CP1252,
                     "Il est sûr que ce fruit est mûr et que la sûreté est assurée.",
                     "Zoë en Chloë reisden naar België voor een geëerd concert."):
            with self.subTest(text=text[:24]):
                raw = text.encode("cp1252")
                stub = [SimpleNamespace(encoding=e)
                        for e in ("cp1252", "cp1257", "cp1250", "cp1254")]
                with mock.patch("charset_normalizer.from_bytes",
                                return_value=stub):
                    self.assertEqual(_detect_encoding(raw), "cp1252")

    def test_russian_paragraph_never_flips_to_cp1257(self):
        """koi8_r lowercase а/л/ш/ы land on the old colliding baltic set
        (į/ė/ų/ū) — the reviewer's Russian->cp1257 shape. Under a cp1257-
        leading stub the Cyrillic script coherence must win instead."""
        text = ("В 2024 году наша команда выпустила три новых версии "
                "поисковой системы. Мы улучшили скорость индексации, "
                "добавили поддержку гибридного поиска и исправили "
                "множество ошибок. Пользователи отмечают, что результаты "
                "стали точнее, а время ответа уменьшилось почти вдвое.")
        raw = text.encode("koi8_r")
        stub = [SimpleNamespace(encoding=e)
                for e in ("cp1257", "cp1252", "cp1250")]
        with mock.patch("charset_normalizer.from_bytes", return_value=stub):
            self.assertEqual(_detect_encoding(raw), "koi8_r")


if __name__ == "__main__":
    unittest.main()
