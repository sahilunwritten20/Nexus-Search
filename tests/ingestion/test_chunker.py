from nexus_search.ingestion.chunker import chunk_text


def test_empty_text():
    assert chunk_text("") == []


def test_small_text():
    assert chunk_text("hello", chunk_size=10) == ["hello"]


def test_text_is_split_into_chunks():
    result = chunk_text(
        "abcdefghij",
        chunk_size=4,
        overlap=1,
    )

    assert result == [
        "abcd",
        "defg",
        "ghij",
    ]


def test_overlap_is_applied():
    result = chunk_text(
        "abcdefgh",
        chunk_size=5,
        overlap=2,
    )

    assert result == [
        "abcde",
        "defgh",
    ]


def test_invalid_chunk_size():
    try:
        chunk_text("hello", chunk_size=0)
        assert False
    except ValueError:
        assert True


def test_invalid_overlap():
    try:
        chunk_text("hello", chunk_size=5, overlap=5)
        assert False
    except ValueError:
        assert True