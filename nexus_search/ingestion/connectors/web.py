"""Web connector: turns HTML into an indexable document.

This is the *canonical* HTML-extraction implementation — the crawler
(Phase 3) imports `extract_page` from here for its own link-following
needs rather than keeping a second copy. Two code paths parsing HTML two
slightly different ways is exactly the kind of drift that causes subtle
bugs later, so there's deliberately only one.
"""
from copy import copy
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..types import IngestDoc

_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript")
_CONTENT_TAGS = ("article", "main")


@dataclass
class ExtractedPage:
    url: str
    title: str
    text: str
    links: list[str]
    meta_description: Optional[str]
    language: Optional[str]
    canonical_url: Optional[str]


def _main_text(soup: BeautifulSoup) -> str:
    working = copy(soup)
    for tag in working.find_all(_STRIP_TAGS):
        tag.decompose()
    for tag_name in _CONTENT_TAGS:
        content = working.find(tag_name)
        if content is not None:
            text = content.get_text(separator=" ", strip=True)
            if len(text) > 50:
                return _collapse_whitespace(text)
    body = working.find("body") or working
    return _collapse_whitespace(body.get_text(separator=" ", strip=True))


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def extract_page(html: str, url: str) -> ExtractedPage:
    """Full extraction, including outgoing links — what the crawler needs
    to both index a page and keep discovering new ones.
    """
    soup = BeautifulSoup(html, "lxml")

    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else url

    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = (meta_desc_tag.get("content", "").strip() if meta_desc_tag else "") or None

    html_tag = soup.find("html")
    language = html_tag.get("lang") if html_tag else None

    canonical_tag = soup.find("link", attrs={"rel": lambda v: v and "canonical" in v})
    canonical_url = urljoin(url, canonical_tag.get("href").strip()) if canonical_tag and canonical_tag.get("href") else None

    text = _main_text(soup)

    links: list[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(url, href)
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)

    return ExtractedPage(url=url, title=title, text=text, links=links,
                          meta_description=meta_description, language=language, canonical_url=canonical_url)


def parse_html(html: str, url: str) -> IngestDoc:
    """What standalone ingestion needs: just the indexable document, no
    link-graph info (that's the crawler's concern, not the indexer's).
    """
    page = extract_page(html, url)
    return IngestDoc(
        doc_id=f"web:{url}",
        title=page.title,
        content=page.text,
        doc_type="web",
        metadata={"url": url, "canonical_url": page.canonical_url or url, "meta_description": page.meta_description, "language": page.language},
    )
