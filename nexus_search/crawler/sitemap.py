"""Sitemap XML readers used to seed the crawler.

SECURITY: sitemaps are remote, attacker-influenced XML. Parsing goes through
defusedxml.ElementTree, which refuses DTDs/entity declarations (billion-
laughs amplification) and extremely deep nesting with a clean exception;
this module converts that into the same "" / [] "can't parse" outcome every
other malformed payload gets ([](https://pypi.org/project/defusedxml/))."""
import zlib
from typing import Optional

# defusedxml raises DefusedXmlException (base of EntitiesForbidden etc.);
# stdlib's Element/ParseError are plain value types, safe to re-export here.
from xml.etree.ElementTree import Element, ParseError
import defusedxml.ElementTree as safe_et
from defusedxml.common import DefusedXmlException


class _SafeElementTree:
    """Drop-in for the bits of xml.etree.ElementTree this module uses."""

    Element = Element
    ParseError = ParseError

    @staticmethod
    def fromstring(xml_text: str):
        return safe_et.fromstring(xml_text)


ElementTree = _SafeElementTree

_MAX_SITEMAP_BYTES = 16 * 1024 * 1024  # decompression-bomb ceiling


def maybe_gzip(data: bytes, content_type: str = "") -> str:
    """Sitemap bytes → XML text. Handles *.xml.gz payloads (magic bytes or
    gzip content-type) with a hard decompressed-size cap; plain XML passes
    through as UTF-8 with replacement."""
    if not data:
        return ""
    if data[:2] == b"\x1f\x8b" or "gzip" in content_type.lower():
        try:
            stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
            # capped decompression: unconsumed_tail non-empty == over the cap
            out = stream.decompress(data, _MAX_SITEMAP_BYTES)
            if stream.unconsumed_tail:
                return ""
        except (zlib.error, ValueError, EOFError):
            return ""
        data = out
    return data.decode("utf-8", errors="replace")


def _root(xml_text: str) -> Optional[ElementTree.Element]:
    if not xml_text or not xml_text.strip():
        return None
    try:
        return ElementTree.fromstring(xml_text)
    except (ElementTree.ParseError, DefusedXmlException):
        # malformed XML and forbidden DTD/entity payloads land the same way:
        # this sitemap is unusable, not fatal
        return None


def _locs(xml_text: str, path: str) -> list[str]:
    root = _root(xml_text)
    if root is None:
        return []
    found = [l.text.strip() for l in root.findall(path) if l.text and l.text.strip()]
    return list(dict.fromkeys(found))


def parse_sitemap(xml_text: str) -> list[str]:
    """Page URLs from a <urlset> sitemap."""
    return _locs(xml_text, "{*}url/{*}loc")


def parse_sitemap_index(xml_text: str) -> list[str]:
    """Child sitemap URLs from a <sitemapindex>."""
    return _locs(xml_text, "{*}sitemap/{*}loc")


def parse_sitemap_extended(xml_text: str) -> list[dict]:
    """A <urlset> including news:news and video:video extension metadata.

    Returns [{"loc", "news"?: {...}, "video"?: [...]}]. The bare <loc> list
    is unchanged in semantic — extensions enrich, they don't filter. Missing
    or malformed extension blocks simply don't appear (parse never raises)."""
    root = _root(xml_text)
    if root is None:
        return []
    out: list[dict] = []
    for url_el in root.findall("{*}url"):
        loc_el = url_el.find("{*}loc")
        if loc_el is None or not loc_el.text or not loc_el.text.strip():
            continue
        entry: dict = {"loc": loc_el.text.strip()}
        news_el = url_el.find(
            "{http://www.google.com/schemas/sitemap-news/0.9}news")
        if news_el is not None:
            news: dict = {}
            title_el = news_el.find(
                "{http://www.google.com/schemas/sitemap-news/0.9}title")
            pub_el = news_el.find(
                "{http://www.google.com/schemas/sitemap-news/0.9}publication_date")
            pub_name_el = news_el.find(
                "{http://www.google.com/schemas/sitemap-news/0.9}publication/"
                "{http://www.google.com/schemas/sitemap-news/0.9}name")
            if title_el is not None and title_el.text:
                news["title"] = title_el.text
            if pub_el is not None and pub_el.text:
                news["publication_date"] = pub_el.text
            if pub_name_el is not None and pub_name_el.text:
                news["publication"] = pub_name_el.text
            if news:
                entry["news"] = news
        videos = []
        for vid_el in url_el.findall(
                "{http://www.google.com/schemas/sitemap-video/1.1}video"):
            video: dict = {}
            t_el = vid_el.find(
                "{http://www.google.com/schemas/sitemap-video/1.1}title")
            thumb_el = vid_el.find(
                "{http://www.google.com/schemas/sitemap-video/1.1}thumbnail_loc")
            dur_el = vid_el.find(
                "{http://www.google.com/schemas/sitemap-video/1.1}duration")
            if t_el is not None and t_el.text:
                video["title"] = t_el.text
            if thumb_el is not None and thumb_el.text:
                video["thumbnail"] = thumb_el.text
            if dur_el is not None and dur_el.text:
                video["duration"] = dur_el.text
            if video:
                videos.append(video)
        if videos:
            entry["video"] = videos
        out.append(entry)
    return list({e["loc"]: e for e in out}.values())  # dedupe by loc, keep first