"""HTTP fetcher with retries, ETag and Last-Modified support."""

import time
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass
class FetchResult:
    url: str
    status_code: Optional[int]
    html: Optional[str]
    error: Optional[str]
    elapsed: float
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False


class Fetcher:
    def __init__(
        self,
        user_agent: str = "NexusSearchBot/0.1 (+https://example.com/bot)",
        timeout: float = 10.0,
        max_retries: int = 2,
    ):
        self.timeout = timeout
        self.max_retries = max_retries

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": user_agent
        })

    def fetch(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> FetchResult:

        last_error = None
        elapsed = 0.0

        headers = {}

        if etag:
            headers["If-None-Match"] = etag

        if last_modified:
            headers["If-Modified-Since"] = last_modified

        for attempt in range(self.max_retries + 1):

            start = time.monotonic()

            try:
                resp = self.session.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                    headers=headers,
                )

                elapsed = time.monotonic() - start

                response_etag = resp.headers.get("ETag")
                response_last_modified = resp.headers.get("Last-Modified")

                if resp.status_code == 304:
                    return FetchResult(
                        url=url,
                        status_code=304,
                        html=None,
                        error=None,
                        elapsed=elapsed,
                        etag=response_etag or etag,
                        last_modified=response_last_modified or last_modified,
                        not_modified=True,
                    )

                content_type = resp.headers.get(
                    "content-type",
                    ""
                )

                if "text/html" not in content_type.lower():

                    return FetchResult(
                        url=url,
                        status_code=resp.status_code,
                        html=None,
                        error=(
                            f"non-HTML content-type: "
                            f"{content_type}"
                        ),
                        elapsed=elapsed,
                        etag=response_etag,
                        last_modified=response_last_modified,
                    )

                return FetchResult(
                    url=url,
                    status_code=resp.status_code,
                    html=resp.text,
                    error=None,
                    elapsed=elapsed,
                    etag=response_etag,
                    last_modified=response_last_modified,
                )

            except requests.RequestException as exc:

                last_error = str(exc)

                elapsed = time.monotonic() - start

                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)

        return FetchResult(
            url=url,
            status_code=None,
            html=None,
            error=last_error,
            elapsed=elapsed,
        )

    def fetch_text(self, url: str) -> str:
        """Fetch a simple text resource such as robots.txt."""

        try:

            resp = self.session.get(
                url,
                timeout=self.timeout,
            )

            return (
                resp.text
                if resp.status_code == 200
                else ""
            )

        except requests.RequestException:
            return ""

    def close(self):
        self.session.close()