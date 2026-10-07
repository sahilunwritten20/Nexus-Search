"""WP12 R6 — single-byte legacy encodings decode as cp1252 (mojibake)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
from nexus_search.ingestion.connectors.files import _detect_encoding  # noqa: E402

CASES = [
    # (label, encoding, short text, long text)
    ("russian cp1251", "cp1251", "Привет мир, как дела?", "Привет мир! " * 40),
    ("russian koi8_r", "koi8_r", "Привет мир, как дела?", "Привет мир! " * 40),
    ("greek cp1253", "cp1253", "Καλημέρα κόσμε;", "Καλημέρα κόσμε! " * 40),
    ("turkish cp1254", "cp1254", "Günaydın dünya?", "Günaydın dünya! " * 40),
    ("polish cp1250", "cp1250", "Żółć gęślą, jaźń?", "Żółć gęślą jaźń! " * 40),
]

ok = 0
for label, enc, short, long in CASES:
    s_raw, l_raw = short.encode(enc), long.encode(enc)
    s_got = _detect_encoding(s_raw)
    l_got = _detect_encoding(l_raw)
    s_ok = s_raw.decode(s_got, errors="replace") == short
    l_ok = l_raw.decode(l_got, errors="replace") == long
    ok += s_ok + l_ok
    print(f"{label:16} short({len(s_raw):4d} B): guessed={s_got:8} roundtrip={'OK' if s_ok else 'MOJIBAKE'}   "
          f"long({len(l_raw):5d} B): guessed={l_got:8} roundtrip={'OK' if l_ok else 'MOJIBAKE'}")
print(f"\n{ok}/10 round-trips exact")
