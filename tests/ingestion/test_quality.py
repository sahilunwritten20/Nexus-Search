from nexus_search.ingestion.quality import content_quality_score


def test_empty_text():
    assert content_quality_score("") == 0.0


def test_short_text():
    score = content_quality_score("hello world")
    assert 0.0 < score < 1.0


def test_good_quality_text():
    text = (
        "Nexus Search is a search engine that indexes documents "
        "and retrieves relevant information using ranking algorithms. "
        "The system supports multiple document formats and web pages."
    )

    score = content_quality_score(text)

    assert score > 0.5
    assert score <= 1.0


def test_score_never_exceeds_one():
    text = "This is a long sentence. " * 100

    assert content_quality_score(text) <= 1.0


def test_whitespace_text():
    assert content_quality_score("   ") == 0.0