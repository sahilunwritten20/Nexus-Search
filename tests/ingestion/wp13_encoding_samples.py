"""WP13 encoding test corpus (data-only module, no nexus_search imports).

Shared by three consumers:
- tests/ingestion/test_wp13_encoding.py (the WP13 table/differential/stub tests)
- scripts/dev/wp13/gen_wp13_fixture.py (differential baseline: runs the WP11
  head's _detect_encoding over this exact table and records which rows
  round-tripped, so WP13 can never regress them)

Structure: one entry per language; each entry carries the codecs the
language's files are actually written in (the reviewer's 13-codec list:
cp1252, euc_kr, cp949, gbk, gb18030, big5, shift_jis, euc_jp, cp1251,
koi8_r, cp1253, cp1254, cp1250) and named text variants covering
with/without spaces, with digits, and with punctuation at three lengths
(short / ~250 B / ~2 KB). The reviewer's exact blocking samples are the
short rows for Korean, GBK, Danish, French and Dutch.
"""

KOREAN_MEDIUM = (
    "한국어 검색 엔진은 형태소 분석과 색인 구조 최적화가 중요합니다. "
    "최근 시스템은 2024년 기준으로 분산 처리와 실시간 색인을 지원하며, "
    "사용자 경험 개선을 위해 순위 알고리즘도 발전했습니다. "
    "오늘날 많은 개발자가 오픈소스 도구를 활용하여 빠르고 정확한 "
    "검색 서비스를 구축하고 있습니다."
)
SIMPLIFIED_MEDIUM = (
    "现代搜索引擎需要处理海量数据, 索引结构与查询优化是核心技术。"
    "2024年我们发布了 3 个新版本, 支持分布式索引和实时检索。"
    "用户希望搜索结果更加准确, 响应时间更短, 同时开发者也关注系统的"
    "可扩展性与稳定性, 以便应对不断增长的数据量与访问压力。"
)
TRADITIONAL_MEDIUM = (
    "傳統中文搜尋系統必須處理龐大的資料量, 2024年我們推出了 3 個新版本, "
    "支援分散式索引與即時檢索。使用者希望結果更準確, 回應更快速, "
    "同時系統也必須保持穩定與安全, 才能滿足企業級應用的需求, 並且降低維運成本。"
)
JAPANESE_MEDIUM = (
    "日本語の検索エンジンでは形態素解析が重要な役割を果たします。"
    "2024年、新しいバージョンを 3 個リリースしました。"
    "ユーザー体験の向上のため、ランキングアルゴリズムも改善され、"
    "多くの開発者に評価されています。"
)
RUSSIAN_MEDIUM = (
    "В 2024 году наша команда выпустила три новых версии поисковой системы. "
    "Мы улучшили скорость индексации, добавили поддержку гибридного поиска "
    "и исправили множество ошибок. Пользователи отмечают, что результаты "
    "стали точнее, а время ответа уменьшилось почти вдвое."
)
RUSSIAN_MIXED = (
    "Мы используем Python и koi8 для разработки. Отличные инструменты "
    "помогают быстро писать код. Эта библиотека очень популярна среди "
    "программистов, которые ценят скорость и качество. Можно сказать, "
    "что это отличный выбор для нашей команды разработчиков."
)
GREEK_MEDIUM = (
    "Η αναζήτηση κειμένου βασίζεται σε δείκτες και αλγορίθμους κατάταξης. "
    "Το 2024 κυκλοφόρησαν 3 νέες εκδόσεις που βελτίωσαν την ακρίβεια "
    "των αποτελεσμάτων και μείωσαν τον χρόνο απόκρισης για τους χρήστες "
    "σε μεγάλες συλλογές εγγράφων και αρχείων."
)
TURKISH_MEDIUM = (
    "Türkçe metin arama motorları için büyük bir öneme sahiptir. "
    "2024 yılında 3 yeni sürüm yayınladık ve kullanıcılar memnun kaldı. "
    "Arama sonuçlarının kalitesi, dizin yapısı ve sıralama algoritmalarıyla "
    "doğrudan ilişkilidir, bu yüzden sürekli iyileştirme gerekir."
)
POLISH_MEDIUM = (
    "Wyszukiwarka pełnotekstowa wymaga optymalizacji struktury indeksu. "
    "W 2024 roku wydałiśmy 3 nowe wersje systemu, które znacznie "
    "przyspieszyły wyszukiwanie. Użytkownicy mogą teraz szybciej znaleźć "
    "potrzebne informacje, także w bardzo dużych zbiorach dokumentów."
)
DANISH_MEDIUM = (
    "Det danske sprog har bogstaverne æ, ø og å. Blåbærgrød og rødgrød "
    "med fløde er klassiske retter, som mange familier laver til store "
    "fester. I 2024 arrangerede 3 foreninger en madfestival i København, "
    "hvor over 2.000 gæster smagte på traditionelle danske specialiteter."
)
FRENCH_MEDIUM = (
    "La sûreté des systèmes de recherche est assurée par des tests "
    "rigoureux. En 2024, nous avons publié 3 nouvelles versions, et les "
    "utilisateurs sont sûrs que la qualité n'a pas changé. Le coût élevé "
    "des infrastructures reste maîtrisé grâce à une architecture efficace."
)
DUTCH_MEDIUM = (
    "Zoë en Chloë reisden naar België voor een geëerd concert. In 2024 "
    "gaven zij 3 voorstellingen in Antwerpen, waar het publiek geboeid "
    "luisterde naar hun muziek, en de kritiek was enthousiast over hun "
    "nieuwe album en de samenwerking met andere artiesten."
)

# (language, codecs, [(variant, text), ...]) — long rows repeat the medium
# text to land in the ~2 KB band (bytes, in the source codec).
LANGUAGES = [
    {
        "name": "korean",
        "codecs": ["euc_kr", "cp949"],
        "variants": [
            ("short_spaced", "한국어 텍스트입니다 테스트 문장"),
            ("short_spaced2",
             "안녕하세요 저는 개발자입니다 검색 엔진을 만들고 있습니다"),
            ("short_nospace", "한국어텍스트입니다테스트문장입니다검색엔진"),
            ("short_digits", "2024년 우리는 3개의 새로운 버전을 출시했습니다"),
            ("medium", KOREAN_MEDIUM),
            ("long", KOREAN_MEDIUM * 6),
        ],
    },
    {
        "name": "simplified_chinese",
        "codecs": ["gbk", "gb18030"],
        "variants": [
            ("short_spaced_latin",
             "Python 是一种 编程语言 , 简单 易学 , 广泛 使用 ."),
            ("short_digits",
             "2024年 我们 发布了 3 个 新 版本 , 欢迎 下载 使用 ."),
            ("short_nospace", "现代搜索引擎需要处理海量数据并快速返回准确结果"),
            ("medium", SIMPLIFIED_MEDIUM),
            ("long", SIMPLIFIED_MEDIUM * 4),
        ],
    },
    {
        "name": "traditional_chinese",
        "codecs": ["big5"],
        "variants": [
            ("short", "這是一個測試文件,關於搜尋引擎的開發。"),
            ("short_digits", "2024年 我們 推出了 3 個 新 版本 , 支援 分散式 索引 。"),
            ("medium", TRADITIONAL_MEDIUM),
            ("long", TRADITIONAL_MEDIUM * 4),
        ],
    },
    {
        "name": "japanese",
        "codecs": ["shift_jis", "euc_jp"],
        "variants": [
            ("short", "東京都渋谷区で機械学習の研究会が開催されました。"),
            ("short2", "奈良県の古い寺院を訪れて、歴史と文化について学びました。"),
            ("short_digits", "2024年、新しいバージョンを 3 個リリースしました。"),
            ("medium", JAPANESE_MEDIUM),
            ("long", JAPANESE_MEDIUM * 6),
        ],
    },
    {
        "name": "russian",
        "codecs": ["koi8_r", "cp1251"],
        "variants": [
            ("short", "Привет мир, как дела?"),
            ("short_digits", "В 2024 году мы выпустили 3 новые версии."),
            ("short_yo", "Поэтому всё это очень хорошо, ёлка трёхэтажная, эхо эйфории."),
            ("medium", RUSSIAN_MEDIUM),
            ("medium_mixed", RUSSIAN_MIXED),
            ("long", RUSSIAN_MEDIUM * 6),
        ],
    },
    {
        "name": "greek",
        "codecs": ["cp1253"],
        "variants": [
            ("short", "Καλημέρα κόσμε;"),
            ("short_digits", "Το 2024 είναι μια καλή χρονιά για 3 νέα έργα."),
            ("medium", GREEK_MEDIUM),
            ("long", GREEK_MEDIUM * 6),
        ],
    },
    {
        "name": "turkish",
        "codecs": ["cp1254"],
        "variants": [
            ("short", "Günaydın dünya?"),
            ("short_digits", "2024 yılında 3 yeni sürüm yayınladık, çok güzel!"),
            ("medium", TURKISH_MEDIUM),
            ("long", TURKISH_MEDIUM * 6),
        ],
    },
    {
        "name": "polish",
        "codecs": ["cp1250"],
        "variants": [
            ("short", "Żółć gęślą, jaźń?"),
            ("short_digits", "W 2024 roku wydałiśmy 3 nowe wersje systemu."),
            ("short_interior_z", "możliwości zawsze trzeba określić, także żądać"),
            ("medium", POLISH_MEDIUM),
            ("long", POLISH_MEDIUM * 6),
        ],
    },
    {
        "name": "danish",
        "codecs": ["cp1252"],
        "variants": [
            ("short",
             "Det danske sprog har bogstaverne æ, ø og å. "
             "Blåbærgrød og rødgrød med fløde."),
            ("short2",
             "Høyt oppe på fjellet bor en gammel mann som elsker "
             "røkt laks og brunost."),
            ("short_digits",
             "I 2024 deltog 3 danske hold i mesterskabet, og de vandt 2 priser."),
            ("medium", DANISH_MEDIUM),
            ("long", DANISH_MEDIUM * 3),
        ],
    },
    {
        "name": "french",
        "codecs": ["cp1252"],
        "variants": [
            ("short", "Il est sûr que ce fruit est mûr et que la sûreté est assurée."),
            ("short_digits", "En 2024, 3 nouvelles versions sont sûres d'être mûres."),
            ("medium", FRENCH_MEDIUM),
            ("long", FRENCH_MEDIUM * 3),
        ],
    },
    {
        "name": "dutch",
        "codecs": ["cp1252"],
        "variants": [
            ("short", "Zoë en Chloë reisden naar België voor een geëerd concert."),
            ("short_nospace", "ZoëenChloëreisdennaarBelgiëvoreengeëerdconcert."),
            ("short_digits", "In 2024 gaven 3 zangers een geëerd concert in België."),
            ("medium", DUTCH_MEDIUM),
            ("long", DUTCH_MEDIUM * 3),
        ],
    },
]


def table_entries():
    """Yield (lang, codec, variant, text) for every row of the table."""
    for lang in LANGUAGES:
        for codec in lang["codecs"]:
            for variant, text in lang["variants"]:
                yield lang["name"], codec, variant, text


def entry_key(lang, codec, variant):
    return f"{lang}|{codec}|{variant}"
