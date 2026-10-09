"""Files connector for Nexus Search.

Supports: TXT, Markdown, RST, CSV, JSON, HTML, PDF, DOCX, XLSX, PPTX.
Unreadable files are logged (never silently dropped) and skipped.
Oversized files are refused BEFORE read (NEXUS_MAX_INGEST_BYTES, default 64MB)
so a monster CSV can't OOM the process mid-parse.
"""
import csv
import json
import logging
import os
import unicodedata
from pathlib import Path
from typing import Callable, Iterator, Optional

from ..types import IngestDoc

DEFAULT_MAX_INGEST_BYTES = 64 * 1024 * 1024  # 64 MiB
# WP12-B8 (audit R8): python-docx materializes ~5x the declared XML in RSS
# (measured: 20 MiB payload -> 100 MiB tracemalloc peak, 223 s parse), so the
# old 512 MiB payload bound admitted ~2.5 GB RSS from a <1 MiB file. 64 MiB
# total / 32 MiB per entry keeps the worst case ~320 MiB; honest OOXML never
# approaches this (a 500-page document.xml is a few MiB).
DEFAULT_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024  # 64 MiB payload per zip
DEFAULT_MAX_ENTRY_BYTES = 32 * 1024 * 1024  # 32 MiB per zip entry
DEFAULT_MAX_PDF_PAGES = 10_000

logger = logging.getLogger("nexus_search.ingestion.files")

DEFAULT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".csv", ".json", ".html", ".htm",
    ".pdf", ".docx", ".xlsx", ".pptx",
}
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}


def _detect_encoding(raw: bytes) -> str:
    """Best-effort encoding detection for text payloads.

    Order (P0-4): BOM -> strict UTF-8 -> detector. charset-normalizer's
    statistical guess on SHORT samples is unreliable even on the pinned
    3.5.1 (measured: cp1252 "Café" -> utf_16_be, "São Paulo" -> big5 ->
    mojibake), so an odd/non-Western guess on a short sample is only
    trusted when it actually EXPLAINS the bytes better than cp1252:
    - Western single-byte guesses (cp125x/iso8859/latin/mac-latin): the
      existing cp1252 preference applies (shares the high-byte range),
      but a legacy SCRIPT codec with a coherent alphabet wins first
      (WP12-B6), a CJK candidate from the detector's own list beats a
      Western misfire (WP13-B6), and cp1257 is accepted only as the
      detector's own top guess (WP13-B6: every Baltic letter collides
      with a real cp1252 letter).
    - CJK-family guesses: ranked among ALL explaining odd-family
      candidates by (cjk share, common-character coherence) over the
      SAME denominator — cross-codec decodes (Korean bytes as GBK) tie
      on share but yield rare characters (WP13-B6).
    - BOM-less utf_16/utf_32 guesses: kept only when NUL interleave or
      (>=32 bytes of) CJK output says the sample really is UTF-16. The
      residual trade — a tiny genuine BOM-less UTF-16 CJK file may flip
      to cp1252 — is accepted and documented; BOM'd UTF-16 is caught
      earlier and never reaches here.

    Very short samples (<64 bytes) are statistically weak whatever we
    choose; the chosen encoding is logged at INFO so nothing is guessed
    silently. Falls back to UTF-8 with replacement when detection finds
    nothing sane — detection failure must degrade, not crash."""
    if not raw:
        return "utf-8"
    # BOMs first — detection libraries underweight them, and they're certain.
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"),
                     (b"\xef\xbb\xbf", "utf-8")):
        if raw.startswith(bom):
            return enc
    # Strict UTF-8: a clean decode is stronger evidence than any
    # statistical guess (and covers ASCII).
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes
        sample = raw[:65536]
        matches = from_bytes(sample)  # sample-bound, not the whole file
        odd_short = len(sample) < 8192
        best_enc = None
        for rank, m in enumerate(matches):
            enc = m.encoding
            enc_l = enc.lower()
            if rank == 0:
                best_enc = enc
            # Single-byte legacy Western charsets: cp1252 is a latin-1
            # superset that shares the high-byte range with the encodings
            # the detector confuses (cp1250, mac_latin2, ...). Prefer it
            # when the sample is also strictly valid cp1252 — "São" must
            # not come back as "Săo". WP12-B6: BEFORE settling for cp1252,
            # check whether a legacy SCRIPT codec (cp1251/koi8_r/cp1253/
            # cp1254/cp1250 family) explains the bytes with one coherent
            # alphabet — those bytes are valid cp1252 too, and the cp1252
            # read was mojibake for five writing systems (audit R6).
            # WP13-B6: a CJK candidate the detector itself proposed can
            # rescue a Western misfire, and cp1257 — whose letters ALL
            # collide with real cp1252 letters — is only accepted when
            # it is the detector's own top guess, never via letter
            # evidence (audit B6: ø/û/ë text flipped to "Hųyt"/"sūr"/
            # "Zoė" through the colliding letters).
            if enc_l.startswith(_WESTERN_GUESS_PREFIXES):
                legacy = _legacy_script_codec(sample, matches)
                rescue = _best_cjk_candidate(sample, matches, cjk_only=True)
                # When BOTH a legacy script decode and a CJK candidate
                # explain the bytes, they compete on the SAME denominator
                # (WP13-B6): the legacy decode must win by the +0.10
                # margin or on script-frequency coherence — CJK bytes
                # decode as coherent Cyrillic/CE soup under the
                # symbol-dense single-byte tables.
                if _legacy_beats_cjk(legacy, rescue):
                    logger.info("legacy script codec %s chosen for a "
                                "sample (script-coherence, audit R6)",
                                legacy[0])
                    return legacy[0]
                if rescue:
                    logger.info("CJK codec %s beats Western-family guess "
                                "%s (WP13-B6)", rescue[0], enc)
                    return rescue[0]
                if rank == 0 and enc_l == "cp1257":
                    logger.info("cp1257 accepted as the detector's own "
                                "top guess (WP13-B6)")
                    return "cp1257"
                if _cp1252_clean(sample):
                    return "cp1252"
                if rank == 0:
                    return enc  # Western top guess, but bytes aren't cp1252
                continue
            # Odd/non-Western guesses on SHORT samples are the misfire zone
            # (P0-4): trust a guess only when it explains the bytes. The
            # candidate scan ranks ALL explaining odd-family guesses in the
            # detector's own list (never introducing codecs it didn't claim)
            # by cjk share, then common-character coherence — the correct
            # CJK decode wins; a UTF-16/big5 misfire that also decodes the
            # bytes loses on coherence (WP13-B6).
            if odd_short and enc_l.startswith(_ODD_GUESS_PREFIXES):
                cand = _best_cjk_candidate(sample, matches)
                if cand:
                    legacy = _legacy_script_codec(sample, matches)
                    if _legacy_beats_cjk(legacy, cand):
                        logger.info("legacy script codec %s beats %s "
                                    "(share %.2f coh %.2f vs cjk %.2f "
                                    "coh %.2f, audit R6/B6)",
                                    legacy[0], cand[0], legacy[1],
                                    legacy[2], cand[1], cand[2])
                        return legacy[0]
                    return cand[0]
                continue
            if rank == 0:
                # Top guess outside the misfire/Western families. A
                # single-byte junk guess (cp874 on koi8_r text,
                # measured) must still lose to a script-coherent legacy
                # codec; a CJK candidate in the detector's own list
                # rescues a junk top guess on CJK bytes; and cp1252-
                # clean bytes never settle for a junk Western guess
                # (measured: cp775 topped a French file, WP13-B6).
                legacy = _legacy_script_codec(sample, matches)
                rescue = _best_cjk_candidate(sample, matches, cjk_only=True)
                if _legacy_beats_cjk(legacy, rescue):
                    logger.info("legacy script codec %s chosen for a "
                                "sample (script-coherence, audit R6)",
                                legacy[0])
                    return legacy[0]
                if rescue:
                    logger.info("CJK codec %s rescues top guess %s "
                                "(WP13-B6)", rescue[0], enc)
                    return rescue[0]
                if _cp1252_clean(sample):
                    return "cp1252"
                return enc  # top guess outside the misfire families: as-is
        # No candidate explained a short odd sample -> the cp1252 fallback
        # is the best remaining bet when the bytes allow it. WP12-B6: same
        # script-codec check as above — e.g. a koi8_r file whose detector
        # guesses were all junk still deserves a Cyrillic read.
        legacy = _legacy_script_codec(sample, matches)
        if legacy:
            logger.info("legacy script codec %s chosen for a sample "
                        "(script-coherence, audit R6)", legacy[0])
            return legacy[0]
        if _cp1252_clean(sample):
            return "cp1252"
        return best_enc or "utf-8"
    except Exception:
        logger.debug("encoding detection failed; defaulting to utf-8",
                     exc_info=True)
    return "utf-8"


def _detect_encoding_logged(raw: bytes) -> str:
    """_detect_encoding with the WP13 honesty log: very short samples are
    statistically weak whichever codec wins, so the choice is logged at
    INFO instead of guessed silently (audit B6)."""
    enc = _detect_encoding(raw)
    if 0 < len(raw) < 64:
        logger.info("encoding %s chosen for a very short sample "
                    "(%d bytes); short-sample detection is statistically "
                    "weak — verify if the source encoding is known", enc, len(raw))
    return enc


# cp1252 has five undefined bytes; a strict decode fails on any of them.
_CP1252_UNDEFINED = {0x81, 0x8D, 0x8F, 0x90, 0x9D}


def _cp1252_clean(raw: bytes) -> bool:
    """True when raw decodes strictly as cp1252 (no undefined control bytes)."""
    return not any(b in _CP1252_UNDEFINED for b in raw)


# Single-byte Western families the cp1252 preference applies to.
_WESTERN_GUESS_PREFIXES = (
    "cp125", "iso8859", "iso-8859", "latin",
    "mac_latin", "mac_roman", "macintosh",
)

# The guess families observed to misfire on short samples (charset-
# normalizer 3.4.x: mac_latin2/utf_16_be/big5; 3.5.1: utf_16_be/big5).
# "euc_jis" joined in WP13-B6: real euc_jp files top-guess big5 and
# euc_jis_2004 (measured) and the correct superset must be rankable.
_ODD_GUESS_PREFIXES = (
    "utf_16", "utf16", "utf_32", "utf32",          # BOM-less multi-byte
    "big5", "cp932", "cp950", "shift_jis", "sjis",  # CJK double-byte
    "gb2312", "gbk", "gb18030", "gb_", "euc_jp", "euc_jis", "euc_kr",
    "cp949", "johab", "iso2022_jp", "iso2022_kr", "hz",
)
# The odd families that are genuinely multi-byte CJK (utf_16/32 excluded:
# they carry the separate NUL-interleave evidence rule).
_CJK_GUESS_PREFIXES = tuple(
    p for p in _ODD_GUESS_PREFIXES
    if not p.startswith(("utf_16", "utf16", "utf_32", "utf32")))


def _evidence_chars(text: str) -> list:
    """Characters that carry script information: non-space, non-digit,
    non-punctuation. WP13-B6: BOTH the cjk share and the legacy script
    share use this denominator — the WP12 comparison divided _cjk_share
    by ALL characters (spaces, digits, punctuation, Latin) while the
    legacy share divided by letters only, so a spaced Korean sentence
    scored ~0.8 against a flat 1.0 for a mojibake decode and the +0.10
    margin was meaningless. Digits are excluded: they carry no script
    information in any language and would poison the Latin 0.9 /
    Cyrillic 0.85 floors for digit-heavy text."""
    return [c for c in text
            if not c.isspace() and not c.isdigit()
            and not unicodedata.category(c).startswith("P")]


def _cjk_share(text: str) -> float:
    """Fraction of EVIDENCE chars (see _evidence_chars) in CJK ranges
    (Han, kana, Hangul, compatibility ideographs)."""
    ev = _evidence_chars(text)
    if not ev:
        return 0.0
    cjk = sum(1 for ch in ev
              if "\u3040" <= ch <= "\u30ff" or "\u3400" <= ch <= "\u4dbf"
              or "\u4e00" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7a3"
              or "\uf900" <= ch <= "\ufaff")
    return cjk / len(ev)


# Common-character sets for CJK coherence (WP13-B6). Cross-codec decodes
# tie the correct codec on cjk share (Korean bytes decode as GBK at share
# 1.0) but yield RARE characters; real text is written in frequent ones.
# Measured separation across the WP13 table: correct decode 0.20-0.81,
# cross-codec decode 0.00-0.54, correct always highest (see
# scripts/dev/wp13/ and AUDIT_REMEDIATION WP13 for the matrix).
_HANGUL_COMMON = frozenset(
    "이다하그는것수있는서고될때우리은이아주라지게더사아니내기"
    "경중새개이들명외건발동방요로씨간직함해처는데번혀후들자다"
    "만그를한의가도에인나위해필요없이같은달라서른스물이른참된"
    "큰작은많은적은좋은나쁜새로운오래된다른같이함께매우아주너무좀"
    "그러나그리고그래서하지만그래서또한즉만약어쩌면아마당연히"
    "검색엔진텍스트문장테스트시스템버전출시개발만들고있습니다"
)
_HAN_COMMON = frozenset(
    "的一是不了人我在有他这中大来上国和地也子时道说而要于就下得"
    "可你年生自会那后能对着事其里所去行过家十用发天如然作方成者"
    "多日都三小军二同经法与面起定还果结间位心之信本形量加主象因"
    "等此各时代新要下以之外表老化点想政相实回关决美把无开手总"
    "长意难知声文力口当任条理比或数情形者起色主此式变正没到出"
    "們個時說學國來對會後裡過發問經動開間長東車樂電買賣醫院書"
    "錢鐵馬龍鳳嶺風飛雲海島嶼灣線路條網際路進區歷史化藝術教育"
    "經濟政治社環境題研究展建設家政府企業管理技術產品市場服務"
    "質量標準規範內容搜尋檢索引擎資料系統使用結果準確快速"
)


def _cjk_coherence(text: str) -> float:
    """Fraction of CJK characters that are COMMON for their block
    (frequent Hangul syllables / Han characters / any kana — kana are a
    small closed set). The correct decode of a CJK file scores 0.2-0.8;
    a cross-codec decode scores ~0.0-0.2 (rare random characters)."""
    cjk = [c for c in text
           if "\u3040" <= c <= "\u30ff" or "\u3400" <= c <= "\u4dbf"
           or "\u4e00" <= c <= "\u9fff" or "\uac00" <= c <= "\ud7a3"
           or "\uf900" <= c <= "\ufaff"]
    if not cjk:
        return 0.0
    common = sum(1 for c in cjk if c in _HANGUL_COMMON or c in _HAN_COMMON
                 or "\u3040" <= c <= "\u30ff")
    return common / len(cjk)


def _odd_guess_explains(sample: bytes, enc_l: str) -> bool:
    """Does a CJK/UTF-16-family guess actually explain this short sample
    better than cp1252 would? Measured margins: real CJK decodes to
    ~100% CJK chars; Western misfires land at ~12%. WP13-B6: for the
    BOM-less utf_16/32 cjk path the share bar alone is not enough —
    Latin text read as utf_16_be lands ~0.7-0.9 cjk share because ASCII
    letter pairs decode into Han/Ext-A ranges; the frequency coherence
    of the decode separates them (genuine CJK 0.2+, these misfires
    ~0.0)."""
    try:
        decoded = sample.decode(enc_l)
    except (UnicodeDecodeError, LookupError):
        return False  # guess can't even read the bytes -> misfire
    if enc_l.startswith(("utf_16", "utf16", "utf_32", "utf32")):
        # Genuine BOM-less UTF-16 of Latin text is ~50% NUL bytes; CJK
        # content has none but needs enough bytes for the guess to mean
        # anything (tiny samples are exactly the "Café" misfire zone).
        nul_share = sum(1 for b in sample if b == 0) / max(len(sample), 1)
        if nul_share >= 1 / 16:
            return True
        return (len(sample) >= 32 and _cjk_share(decoded) >= 0.3
                and _cjk_coherence(decoded) >= 0.2)
    return _cjk_share(decoded) >= 0.3


def _best_cjk_candidate(sample: bytes, matches, cjk_only: bool = False):
    """Best odd-family candidate that actually explains the sample:
    (encoding, cjk_share, cjk_coherence) ranked by share, then
    common-character coherence, then the detector's own ranking. Only
    codecs the detector PROPOSED are considered — the P0-4 rule that
    exotic codepages are never introduced. WP13-B6: the old code
    returned the FIRST explaining guess; with a stubbed/wrong detector
    that let a big5 top-guess steal a Korean file even though cp949
    (also proposed) explained it better. cjk_only restricts the scan
    to the multi-byte CJK families (used for rescues from Western/junk
    guesses; the UTF-16 NUL rule is a separate evidence class
    evaluated only on the odd branch)."""
    best = None
    prefixes = _CJK_GUESS_PREFIXES if cjk_only else _ODD_GUESS_PREFIXES
    for rank, m in enumerate(matches):
        enc = m.encoding
        enc_l = enc.lower()
        if not enc_l.startswith(prefixes):
            continue
        if not _odd_guess_explains(sample, enc_l):
            continue
        try:
            decoded = sample.decode(enc_l)
        except (UnicodeDecodeError, LookupError):
            continue
        share = _cjk_share(decoded)
        coherence = _cjk_coherence(decoded)
        key = (share, coherence, -rank)
        if best is None or key > best[0]:
            best = (key, enc, share, coherence)
    return (best[1], best[2], best[3]) if best else None


def _legacy_beats_cjk(legacy, cand) -> bool:
    """Competition between a script-coherent legacy decode and an
    explaining CJK candidate, on the SAME denominator (WP13-B6). The
    legacy decode must either explain clearly more of the bytes
    (+0.10, the WP12 margin — kept because CJK bytes coincidentally
    decode as coherent Cyrillic/CE under the symbol-dense single-byte
    tables) or, on a near-tie in share, show more script-frequency
    coherence: real Greek text beats a johab misfire that decodes the
    same bytes to rare random Hangul (measured: 0.615 vs ~0.0)."""
    if not legacy:
        return False
    if not cand:
        return True
    if legacy[1] > cand[1] + 0.10:
        return True
    return legacy[1] >= cand[1] - 0.10 and legacy[2] > cand[2]


# --- WP12-B6 / WP13-B6: single-byte legacy script detection --------------
# charset-normalizer cannot separate cp1251/koi8_r/cp1253/cp1254/cp1250
# from cp1252 on short samples (their bytes are usually valid cp1252 too),
# so the cp1252 preference mojibaked five writing systems (audit R6:
# 0/10 round-trips). Candidates are scored by SCRIPT COHERENCE instead:
# a strict decode whose cased letters sit overwhelmingly in ONE alphabet
# (Cyrillic/Greek), or a Latin family whose decode contains INTERIOR
# family-specific letters. Tie-breaks: detector rank first, then share of
# the script's most common letters (separates cp1251 from koi8_r — KOI8
# stores Cyrillic with the case bit inverted — and cp1253 from cp1251),
# then cp125x family order.
_LEGACY_SCRIPTS = {
    "cp1251": "cyrillic", "koi8_r": "cyrillic", "iso8859-5": "cyrillic",
    "cp1253": "greek", "iso8859-7": "greek",
    "cp1254": "turkish", "iso8859-9": "turkish",
    "cp1250": "ce", "iso8859-2": "ce",
    "cp1257": "baltic",
}
# Only letters whose cp125x byte is NOT a letter in the competing
# interpretations: for cp1252 that means a symbol/punctuation byte
# (interior occurrences prove the legacy codec); for the OTHER legacy
# families the byte must not be one of THEIR evidence letters either.
# WP13-B6 removed the Baltic collision letters (ūėįųŗŪĖĮŲ -> ø/û/ë/á/ø/º/
# Û/Ë/Á/Ø under cp1252): two of them flipped every Danish/French/Dutch
# file with 2+ ø/û/ë to cp1257 ("Hųyt", "sūr", "Zoė"). What survives:
#  - turkish ıİşğŞĞ: documented trade vs Icelandic ý/þ (bytes 0xFD/0xFE;
#    _TR_UNIQUE needs only ONE interior hit for ı/İ)
#  - ce ąłżĄŁŻ: bytes 0xB9/0xB3/0xBF = ¹³¿ in cp1252 (¹³¿ must keep
#    their cp1252 read), NOT colliding with any other legacy evidence
#    letter. NOTE: cp1257's æ sits on cp1250's ż byte (0xBF) — a
#    cp1257 Nordic file and a cp1250 Polish file can each trigger the
#    other's evidence; detector rank decides that residual tie.
#  - baltic Øø ONLY: bytes 0xA8/0xB8 = ¨/¸ in cp1252 (symbols), ˇ/¸ in
#    cp1250, box-drawing in koi8_r — colliding with nothing textual.
#    Æ/æ (0xAF/0xBF = ¯/¿ in cp1252) were EXCLUDED: they collide with
#    cp1250's Ż/ż. cp1251's ё (0xB8) reads as ø, and koi8_r's lowercase
#    а/л/ш/ы read as į/ė/ų/ū — both are protected by the Cyrillic
#    script winning over any Latin family below, not by the evidence
#    set. Everything else in cp1257 (ā č ē ģ ī ķ ļ ņ š ū ž ...) collides
#    with real cp1252 letters (â ç è ì î ï ò ù û ß...) and can never be
#    letter evidence — genuine Baltic files rely on the detector's own
#    top guess instead (accepted in the main loop).
_LATIN_SPECIFIC = {
    "turkish": "ıİşğŞĞ",
    "ce": "ąłżĄŁŻ",
    "baltic": "Øø",
}
# most common letters (share separates true text from cross-codec soup).
# Kept deliberately SMALL (8-11 letters): the floor below must stay above
# what a RANDOM cross-codec decode scores (~|set|/33 for Cyrillic).
_CYR_COMMON = set("оеаинтрс")
_GR_COMMON = set("αετοινρκσλ")
# Measured floors (WP13-B6, case-folded): real Russian 0.48-0.89, real
# Greek 0.615; CJK bytes read as cp1251/iso8859-5/cp1253 score 0.07-0.32
# (those tables have almost no symbol bytes, so the symbol-reject cannot
# catch them — this floor does). 0.40 splits both sides with margin.
_SCRIPT_COHERENCE_FLOOR = 0.40
# 'ı'/'İ' are unambiguous in the single-byte space: cp1252 0xFD is 'ý'
# (Icelandic, not interior-typical), and no other candidate family
# defines it — a single interior hit is decisive for these.
_TR_UNIQUE = set("ıİ")
# Latin-family evidence density (WP13-B6): the specific letters must be
# a meaningful fraction of the decode's high-byte letters. Real
# Polish/Turkish/Baltic text measures 0.33-0.75 (ą/ł/ż, ı/ş/ğ and Ø/ø
# are among the most frequent letters of their languages); CJK bytes
# read as cp1250/iso8859-2/cp1254 hit those byte positions at the random
# rate (measured 0.07-0.16). 0.25 splits both sides with margin.
_LATIN_HIT_RATIO_MIN = 0.25
# WP13-B6: a legacy decode with more than 5% symbols among non-space
# characters is always wrong for real Cyrillic/Greek/Latin text (real
# decodes measure 0-2%); CJK bytes read as single-byte legacy codecs
# measure 5-46% (koi8_r/cp1251 map the CJK lead bytes to box-drawing).
# The count>=2 companion keeps a single degree sign in a very short
# sample from rejecting a genuine decode.
_SYMBOL_SHARE_MAX = 0.05


def _symbol_share(text: str) -> float:
    non_space = [c for c in text if not c.isspace()]
    if not non_space:
        return 0.0
    return (sum(1 for c in non_space
                if unicodedata.category(c).startswith("S")) / len(non_space))


def _script_of(ch: str) -> str:
    o = ord(ch)
    if 0x0400 <= o <= 0x04FF:
        return "cyrillic"
    if 0x0370 <= o <= 0x03FF:
        return "greek"
    if ch.isalpha():
        return "latin"
    return ""


def _legacy_script_codec(sample: bytes, matches):
    """Best script-coherent legacy codec for a sample: (codec, share,
    coherence) with share = the winning decode's script share over the
    SHARED denominator (non-space, non-digit, non-punctuation chars —
    see _evidence_chars), or None when nothing explains the bytes
    better than the existing cp1252 preference. Callers compare
    `share` against competing interpretations (e.g. a CJK misfire's
    cjk share) — same denominator both sides, or the margin is
    meaningless (audit B6).

    WP13-B6 rules on top of the WP12 scoring:
    - symbol-reject: >5% symbols (and >=2 of them) rejects the decode
      (box-drawing like ╠╧╬ is always wrong for real text);
    - non-Latin scripts (Cyrillic/Greek) beat Latin families whenever
      both are coherent — whole-alphabet coherence is far stronger
      evidence than a few interior Latin letters, and the Latin
      candidates' evidence letters collide with Cyrillic text bytes
      (koi8_r ы -> cp1250 Ł, cp1251 ё -> cp1257 ø, measured).

    Residual limits (documented, per audit B6): a Latin-family sample
    whose ONLY script-specific letters sit at word boundaries (e.g. a
    lone leading 'Ł') can stay cp1252 — the interior rule exists so
    genuine cp1252 text ('m³', French 'cœur', Danish 'æ', Spanish 'ñ')
    is never stolen. Icelandic 'ý'/'þ' can read as Turkish 'ı'/'ş' —
    the two families share those bytes; documented trade. Genuine
    cp1257 Baltic files whose letters all collide with cp1252 rely on
    the detector's own top guess (see _detect_encoding), not on letter
    evidence."""
    if not sample or not any(b >= 0x80 for b in sample):
        return None
    coherent: dict[str, tuple] = {}
    for codec, script in _LEGACY_SCRIPTS.items():
        try:
            text = sample.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        if any(0x80 <= ord(c) <= 0x9F for c in text):
            continue  # C1 controls: wrong codec for these bytes
        non_space = [c for c in text if not c.isspace()]
        symbols = sum(1 for c in non_space
                      if unicodedata.category(c).startswith("S"))
        if symbols >= 2 and symbols / max(len(non_space), 1) > _SYMBOL_SHARE_MAX:
            continue  # box-drawing / stray symbol soup: wrong codec
        evidence = _evidence_chars(text)
        if not evidence:
            continue
        letters = [c for c in text if c.isalpha()]
        if not letters:
            continue
        if script in ("cyrillic", "greek"):
            in_script = [c for c in evidence if _script_of(c) == script]
            if len(in_script) < 3:
                continue
            share = len(in_script) / len(evidence)
            if share < 0.85:
                continue
            common = (_CYR_COMMON if script == "cyrillic" else _GR_COMMON)
            # case-fold: ALL-CAPS Cyrillic headlines must not lose the
            # common-letter coherence check
            coherence = (sum(1 for c in in_script if c.lower() in common)
                         / len(in_script))
            if coherence < _SCRIPT_COHERENCE_FLOOR:
                continue  # random cross-codec letters, not real script text
            coherent[codec] = (share, coherence, len(in_script))
        else:  # Latin family
            specific = _LATIN_SPECIFIC[script]
            hits = [c for c in letters if c in specific]
            if not hits:
                continue
            interior = any(
                i >= 1 and text[i - 1].isalpha() and i + 1 < len(text)
                and text[i + 1].isalpha()
                for i, c in enumerate(text) if c in specific)
            if not interior:
                continue  # 'm³'-style boundary symbols must not flip us
            high_letters = [c for c in letters if ord(c) >= 0x80]
            if (len(hits) / max(len(high_letters), 1)) < _LATIN_HIT_RATIO_MIN:
                continue  # specific bytes hit at the random rate: mojibake
            if script == "turkish" and not (len(hits) >= 2 or (set(hits) & _TR_UNIQUE)):
                continue  # one shared-zone letter (French œ→ś) proves little
            # ce/baltic need no minimum hit count: their evidence bytes
            # are cp1252 SYMBOLS (¹ ³ ¿ ¨ ¸) — one interior occurrence is
            # as decisive as the Turkish ıİ rule (WP13-B6; a digits-variant
            # Polish file with a single ą used to fall back to cp1252)
            latin_share = sum(1 for c in evidence
                              if _script_of(c) == "latin") / len(evidence)
            if latin_share < 0.9:
                continue
            coherent[codec] = (latin_share, 0.5 + 0.01 * len(hits), len(hits))
    if not coherent:
        return None
    # A coherent non-Latin alphabet (85%+ of evidence chars in Cyrillic/
    # Greek, ~0% symbols) is stronger evidence than interior Latin
    # letters; it wins over any Latin-family candidate (WP13-B6).
    pool = ([c for c in coherent if _LEGACY_SCRIPTS[c] in ("cyrillic", "greek")]
            or list(coherent))
    # Tie-break 1: the detector's own ranking, in match order.
    for m in matches:
        if m.encoding in pool:
            return (m.encoding, coherent[m.encoding][0], coherent[m.encoding][1])
    # Tie-break 2: script-common-letter coherence, then coverage, then
    # cp125x family order (real-world files are overwhelmingly cp125x).
    order = sorted(((codec, coherent[codec]) for codec in pool),
                   key=lambda kv: (-kv[1][1], -kv[1][2],
                                   kv[0].startswith("cp125") and 0 or 1, kv[0]))
    return order[0][0], order[0][1][0], order[0][1][1]


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    return raw.decode(_detect_encoding_logged(raw), errors="replace")


def read_markdown_file(path: Path) -> str:
    """Markdown reader (WP6): everything stays searchable, but the MARKUP
    doesn't leak into the index as noise:
    - YAML front matter (leading --- block) is dropped
    - fenced code blocks keep their text, drop the ``` fences + language tags
    - heading markers (#) are dropped, heading text kept
    - links [text](url) keep the text; images ![alt](url) keep the alt
    - emphasis/strong/strikethrough markers are unwrapped
    - blockquote markers and list bullets are stripped
    """
    text = read_text_file(path)

    # front matter: a leading --- ... --- block
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:].lstrip("\n")

    out_lines = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue  # fence markers and language tags are not content
        if in_fence:
            out_lines.append(line)  # code text verbatim
            continue
        # heading markers
        if stripped.startswith("#"):
            line = line.lstrip("# \t")
        # blockquote markers
        if stripped.startswith(">"):
            line = line.lstrip("> ")
        # images before links (they share the bracket syntax)
        import re as _re
        line = _re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", line)
        # links: keep the anchor text, drop the URL
        line = _re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        # emphasis / strong / strikethrough
        line = _re.sub(r"(\*\*\*|___|\*\*|__|~~|_|\*)(?=\S)(.*?\S)\1", r"\2", line)
        # list bullets
        line = _re.sub(r"^(\s*)[-*+]\s+", r"\1", line)
        out_lines.append(line.rstrip())
    return "\n".join(out_lines).strip("\n")


def read_csv_file(path: Path) -> str:
    """Stream a CSV row-by-row — no full-file materialization. Detection
    samples the first 64KB only; the body is decoded incrementally, so a
    40MB CSV costs a bounded window instead of three full-size copies."""
    import io
    with path.open("rb") as probe:
        sample = probe.read(65536)
    encoding = _detect_encoding_logged(sample)
    with path.open("r", encoding=encoding, errors="replace") as f:
        reader = csv.DictReader(f)
        out_rows: list[str] = []
        for row in reader:
            line = " | ".join(f"{k}: {v}" for k, v in row.items() if k and v)
            if line:
                out_rows.append(line)
    return "\n".join(out_rows)


def read_json_file(path: Path) -> str:
    out: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                out.append(str(k))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif x is not None:
            out.append(str(x))

    walk(json.loads(read_text_file(path)))
    return "\n".join(out)


def read_html_file(path: Path) -> str:
    from .web import parse_html

    return parse_html(read_text_file(path), path.as_uri()).content


def read_pdf_file(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    # P1-12a: "huge pages" guard — a pathological page count means
    # pathological parse time; refuse before extracting anything.
    max_pages = _max_pdf_pages()
    if len(reader.pages) > max_pages:
        raise ValueError(f"{len(reader.pages)} pages exceeds "
                         f"NEXUS_MAX_PDF_PAGES ({max_pages})")
    pages = (page.extract_text() for page in reader.pages)
    text = "\n".join(t for t in pages if t)
    if not text.strip():
        ocr = _ocr_pdf(path)
        if ocr:
            return ocr
    return text


def _ocr_pdf(path: Path) -> str:
    """OCR fallback for image-only (scanned) PDFs, opt-in via NEXUS_OCR=1.

    Needs pytesseract + a `tesseract` binary on PATH (and pdf2image+poppler).
    If unavailable we return "" and LOG it, not silently skip: a doc that
    yielded no text was already logged by read_file as unreadable/disabled."""
    import shutil as _shutil
    if os.environ.get("NEXUS_OCR") != "1":
        return ""
    if _shutil.which("tesseract") is None:
        logger.warning("NEXUS_OCR=1 but no tesseract binary on PATH; skipping OCR for %s", path)
        return ""
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        logger.warning("NEXUS_OCR=1 but pytesseract/pdf2image not installed; skipping OCR for %s", path)
        return ""
    try:
        images = convert_from_path(str(path))
        return "\n".join(pytesseract.image_to_string(img) for img in images)
    except Exception as exc:
        logger.warning("OCR failed for %s: %s", path, exc)
        return ""


def _check_zip_payload(path: Path) -> None:
    """P1-12a: refuse zip containers whose DECLARED decompressed payload
    exceeds NEXUS_MAX_DECOMPRESSED_BYTES (total) or NEXUS_MAX_ENTRY_BYTES
    (per entry, WP12-B8), BEFORE any reader materializes it.
    NEXUS_MAX_INGEST_BYTES caps the file on disk, not the payload — a ~200
    KiB 'docx' whose XML entries declare gigabytes (the zip-bomb class)
    would otherwise be fully inflated by the OOXML reader. The check reads
    only the zip central directory (declared sizes), no decompression. A
    header that lies SMALL truncates harmlessly inside Python's zipfile;
    a header that lies LARGE just means the guard fires early — both safe
    directions. The per-entry cap catches the nested-bomb shape (one huge
    entry padded under the total by small files)."""
    import zipfile
    bound = _max_decompressed_bytes()
    entry_bound = _max_entry_bytes()
    total = 0
    with zipfile.ZipFile(str(path)) as zf:
        for info in zf.infolist():
            if info.file_size > entry_bound:
                raise ValueError(
                    f"declared decompressed entry {info.filename} "
                    f"({info.file_size} bytes) exceeds "
                    f"NEXUS_MAX_ENTRY_BYTES ({entry_bound})")
            total += info.file_size
            if total > bound:
                raise ValueError(
                    f"declared decompressed payload {total} bytes exceeds "
                    f"NEXUS_MAX_DECOMPRESSED_BYTES ({bound})")


def read_docx_file(path: Path) -> str:
    from docx import Document

    _check_zip_payload(path)
    document = Document(str(path))
    text = [p.text for p in document.paragraphs if p.text]
    for table in document.tables:  # tables were previously dropped
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                text.append(" | ".join(cells))
    return "\n".join(text)


def read_xlsx_file(path: Path) -> str:
    from openpyxl import load_workbook

    _check_zip_payload(path)
    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        text = []
        for sheet in workbook.worksheets:
            text.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(v) for v in row if v is not None]
                if values:
                    text.append(" | ".join(values))
        return "\n".join(text)
    finally:
        workbook.close()


def read_pptx_file(path: Path) -> str:
    from pptx import Presentation

    _check_zip_payload(path)
    text = []
    for n, slide in enumerate(Presentation(str(path)).slides, start=1):
        text.append(f"Slide: {n}")
        for shape in slide.shapes:
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    text.append(" | ".join(c.text for c in row.cells if c.text))
            elif getattr(shape, "text", ""):
                text.append(shape.text)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text
            if notes:
                text.append(notes)
    return "\n".join(text)


_READERS: dict[str, Callable[[Path], str]] = {
    ".txt": read_text_file, ".md": read_markdown_file, ".markdown": read_markdown_file,
    ".rst": read_text_file, ".csv": read_csv_file, ".json": read_json_file,
    ".html": read_html_file, ".htm": read_html_file,
    ".pdf": read_pdf_file, ".docx": read_docx_file,
    ".xlsx": read_xlsx_file, ".pptx": read_pptx_file,
}

# content-type -> reader: magic bytes beat lying extensions (WP6)
_CONTENT_READERS = {
    "application/pdf": read_pdf_file,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": read_docx_file,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": read_xlsx_file,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": read_pptx_file,
}


def read_file(path: Path) -> str:
    """Read a file; '' (and a logged warning) on failure or over the size
    limit. Routing: magic bytes first (a PDF named .txt parses as PDF),
    extension second — `mime.py` holds the detection policy."""
    limit = _max_ingest_bytes()
    try:
        size = path.stat().st_size  # stat BEFORE opening: size guard costs nothing
    except OSError:
        return ""
    if size > limit:
        logger.warning("Refusing to read %s: %d bytes exceeds NEXUS_MAX_INGEST_BYTES (%d)",
                       path, size, limit)
        return ""
    try:
        from ..mime import sniff_content_type
        sniffed = sniff_content_type(path)
        if sniffed == "application/octet-stream":
            logger.warning("Refusing to read %s: binary content", path)
            return ""
        reader = _CONTENT_READERS.get(sniffed)
        if reader is not None:
            return reader(path)
        reader = _READERS.get(path.suffix.lower())
        if reader is None:
            return ""
        return reader(path)
    except Exception as exc:  # corrupt file, missing optional dependency, ...
        logger.warning("Could not read %s: %s: %s", path, type(exc).__name__, exc)
        return ""


def _max_ingest_bytes() -> int:
    raw = os.environ.get("NEXUS_MAX_INGEST_BYTES")
    if not raw:
        return DEFAULT_MAX_INGEST_BYTES
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid NEXUS_MAX_INGEST_BYTES %r; using default %d",
                       raw, DEFAULT_MAX_INGEST_BYTES)
        return DEFAULT_MAX_INGEST_BYTES


def _max_decompressed_bytes() -> int:
    raw = os.environ.get("NEXUS_MAX_DECOMPRESSED_BYTES")
    if not raw:
        return DEFAULT_MAX_DECOMPRESSED_BYTES
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid NEXUS_MAX_DECOMPRESSED_BYTES %r; using default %d",
                       raw, DEFAULT_MAX_DECOMPRESSED_BYTES)
        return DEFAULT_MAX_DECOMPRESSED_BYTES


def _max_entry_bytes() -> int:
    raw = os.environ.get("NEXUS_MAX_ENTRY_BYTES")
    if not raw:
        return DEFAULT_MAX_ENTRY_BYTES
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid NEXUS_MAX_ENTRY_BYTES %r; using default %d",
                       raw, DEFAULT_MAX_ENTRY_BYTES)
        return DEFAULT_MAX_ENTRY_BYTES


def _max_pdf_pages() -> int:
    raw = os.environ.get("NEXUS_MAX_PDF_PAGES")
    if not raw:
        return DEFAULT_MAX_PDF_PAGES
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("Invalid NEXUS_MAX_PDF_PAGES %r; using default %d",
                       raw, DEFAULT_MAX_PDF_PAGES)
        return DEFAULT_MAX_PDF_PAGES


def iter_files(root: str, extensions: Optional[set[str]] = None) -> Iterator[IngestDoc]:
    extensions = {e.lower() for e in (extensions or DEFAULT_EXTENSIONS)}
    root_path = Path(root)

    for path in sorted(root_path.rglob("*")):
        rel = path.relative_to(root_path)
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(part in IGNORED_DIRS for part in rel.parts):
            continue
        content = read_file(path)
        if not content.strip():
            logger.warning("No text extracted from %s (empty, scanned, or unreadable)", rel)
            continue
        yield IngestDoc(
            doc_id=f"file:{rel}",
            title=path.stem,
            content=content,
            doc_type="file",
            metadata={"path": str(rel), "extension": path.suffix.lower(), "filename": path.name},
        )