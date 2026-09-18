"""Simple crawler monitoring metrics."""

import threading
import time


class CrawlerMetrics:

    def __init__(self):

        self.lock = threading.Lock()

        self.started_at = time.time()

        self.crawled = 0
        self.skipped = 0
        self.errors = 0
        self.not_modified = 0
        self.bytes_downloaded = 0

    def record_crawled(
        self,
        bytes_downloaded: int = 0,
    ):

        with self.lock:

            self.crawled += 1
            self.bytes_downloaded += (
                bytes_downloaded
            )

    def record_skipped(self):

        with self.lock:
            self.skipped += 1

    def record_error(self):

        with self.lock:
            self.errors += 1

    def record_not_modified(self):

        with self.lock:
            self.not_modified += 1

    def snapshot(self) -> dict:

        with self.lock:

            elapsed = max(
                0.001,
                time.time() - self.started_at,
            )

            return {
                "crawled": self.crawled,
                "skipped": self.skipped,
                "errors": self.errors,
                "not_modified": self.not_modified,
                "bytes_downloaded": self.bytes_downloaded,
                "elapsed_seconds": round(
                    elapsed,
                    3,
                ),
                "pages_per_second": round(
                    self.crawled / elapsed,
                    3,
                ),
            }