"""Simple crawl scheduling support."""

import time


class CrawlScheduler:

    def __init__(
        self,
        interval_seconds: float = 3600,
    ):
        if interval_seconds <= 0:
            raise ValueError(
                "interval_seconds must be greater than 0"
            )

        self.interval_seconds = interval_seconds
        self.next_run = time.time()

    def due(self) -> bool:
        return time.time() >= self.next_run

    def schedule_next(self):
        self.next_run = (
            time.time()
            + self.interval_seconds
        )

    def seconds_until_next(self) -> float:
        return max(
            0.0,
            self.next_run - time.time(),
        )