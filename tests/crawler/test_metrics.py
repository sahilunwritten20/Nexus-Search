from nexus_search.crawler.metrics import CrawlerMetrics


def test_initial_metrics():

    metrics = CrawlerMetrics()

    data = metrics.snapshot()

    assert data["crawled"] == 0
    assert data["skipped"] == 0
    assert data["errors"] == 0
    assert data["not_modified"] == 0


def test_record_crawled():

    metrics = CrawlerMetrics()

    metrics.record_crawled(
        bytes_downloaded=100
    )

    data = metrics.snapshot()

    assert data["crawled"] == 1

    assert (
        data["bytes_downloaded"]
        == 100
    )


def test_record_error():

    metrics = CrawlerMetrics()

    metrics.record_error()

    assert (
        metrics.snapshot()["errors"]
        == 1
    )


def test_record_skipped():

    metrics = CrawlerMetrics()

    metrics.record_skipped()

    assert (
        metrics.snapshot()["skipped"]
        == 1
    )


def test_record_not_modified():

    metrics = CrawlerMetrics()

    metrics.record_not_modified()

    assert (
        metrics.snapshot()["not_modified"]
        == 1
    )