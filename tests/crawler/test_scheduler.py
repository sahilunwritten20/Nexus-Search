from nexus_search.crawler.scheduler import CrawlScheduler


def test_scheduler_is_due_initially():

    scheduler = CrawlScheduler(
        interval_seconds=60
    )

    assert scheduler.due() is True


def test_scheduler_schedules_next_run():

    scheduler = CrawlScheduler(
        interval_seconds=60
    )

    scheduler.schedule_next()

    assert scheduler.due() is False


def test_seconds_until_next_is_positive():

    scheduler = CrawlScheduler(
        interval_seconds=60
    )

    scheduler.schedule_next()

    assert (
        scheduler.seconds_until_next()
        > 0
    )


def test_invalid_interval():

    try:

        CrawlScheduler(
            interval_seconds=0
        )

        assert False

    except ValueError:

        assert True