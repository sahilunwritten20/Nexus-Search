"""WP13 audit-B6 evidence: the reviewer's exact blocking samples decoded
by the WP11 head (3511b86), the WP12 head (b63bdb5) and the WP13 tree.

Usage (all three checkouts available):
    python scripts/dev/wp13/b6_before_after.py <wp11_worktree> <wp12_worktree>
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

SAMPLES = [
    ("korean euc_kr, spaced", "한국어 텍스트입니다 테스트 문장", "euc_kr"),
    ("korean cp949, spaced", "한국어 텍스트입니다 테스트 문장", "cp949"),
    ("korean euc_kr, spaced 2", "안녕하세요 저는 개발자입니다 검색 엔진을 만들고 있습니다", "euc_kr"),
    ("korean cp949, spaced 2", "안녕하세요 저는 개발자입니다 검색 엔진을 만들고 있습니다", "cp949"),
    ("GBK spaces+Latin", "Python 是一种 编程语言 , 简单 易学 , 广泛 使用 .", "gbk"),
    ("gb18030 spaces+Latin", "Python 是一种 编程语言 , 简单 易学 , 广泛 使用 .", "gb18030"),
    ("GBK digits", "2024年 我们 发布了 3 个 新 版本 , 欢迎 下载 使用 .", "gbk"),
    ("gb18030 digits", "2024年 我们 发布了 3 个 新 版本 , 欢迎 下载 使用 .", "gb18030"),
    ("danish cp1252 #1", "Det danske sprog har bogstaverne æ, ø og å. Blåbærgrød og rødgrød med fløde.", "cp1252"),
    ("danish cp1252 #2", "Høyt oppe på fjellet bor en gammel mann som elsker røkt laks og brunost.", "cp1252"),
    ("french cp1252 (û)", "Il est sûr que ce fruit est mûr et que la sûreté est assurée.", "cp1252"),
    ("dutch cp1252 (ë)", "Zoë en Chloë reisden naar België voor een geëerd concert.", "cp1252"),
    ("russian koi8_r ~250B",
     "В 2024 году наша команда выпустила три новых версии поисковой "
     "системы. Мы улучшили скорость индексации, добавили поддержку "
     "гибридного поиска и исправили множество ошибок. Пользователи "
     "отмечают, что результаты стали точнее, а время ответа "
     "уменьшилось почти вдвое.", "koi8_r"),
    ("russian koi8_r mixed Latin",
     "Мы используем Python и koi8 для разработки. Отличные инструменты "
     "помогают быстро писать код. Эта библиотека очень популярна "
     "среди программистов, которые ценят скорость и качество.", "koi8_r"),
]


def load_detection(worktree: Path):
    sys.path.insert(0, str(worktree))
    try:
        mod = importlib.import_module("nexus_search.ingestion.connectors.files")
        return mod._detect_encoding
    finally:
        sys.path.remove(str(worktree))


def main() -> int:
    wp11 = load_detection(Path(sys.argv[1]).resolve())
    # WP12 and WP13 import the same module path; load WP12 under a
    # private name via importlib machinery so it does not shadow.
    import importlib
    wp12_wt = Path(sys.argv[2]).resolve()
    sys.path.insert(0, str(wp12_wt))
    for name in [m for m in list(sys.modules)
                 if m == "nexus_search" or m.startswith("nexus_search.")]:
        del sys.modules[name]
    wp12_mod = importlib.import_module("nexus_search.ingestion.connectors.files")
    wp12 = wp12_mod._detect_encoding
    sys.path.remove(str(wp12_wt))
    for name in [m for m in list(sys.modules)
                 if m == "nexus_search" or m.startswith("nexus_search.")]:
        del sys.modules[name]

    sys.path.insert(0, str(REPO))
    wp13_mod = importlib.import_module("nexus_search.ingestion.connectors.files")
    wp13 = wp13_mod._detect_encoding

    print(f"{'sample':32s} {'codec':9s} | WP11            | WP12            | WP13")
    print("-" * 100)
    wp12_fail = wp13_fail = 0
    for label, text, codec in SAMPLES:
        raw = text.encode(codec)
        r11, r12, r13 = wp11(raw), wp12(raw), wp13(raw)
        ok11 = raw.decode(r11, errors="replace") == text
        ok12 = raw.decode(r12, errors="replace") == text
        ok13 = raw.decode(r13, errors="replace") == text
        wp12_fail += not ok12
        wp13_fail += not ok13
        mark = lambda ok, enc: ("OK " if ok else "MOJIBAKE->") + enc
        print(f"{label:32s} {codec:9s} | {mark(ok11, r11):15s} | {mark(ok12, r12):15s} | {mark(ok13, r13)}")
    print(f"\nWP12 head: {wp12_fail}/{len(SAMPLES)} rows mojibake; "
          f"WP13 head: {wp13_fail}/{len(SAMPLES)} rows mojibake")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
