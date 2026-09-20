"""Sitemap XML readers used to seed the crawler."""
from typing import Optional
from xml.etree import ElementTree


def _root(xml_text: str) -> Optional[ElementTree.Element]:
    if not xml_text or not xml_text.strip():
        return None
    try:
        return ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
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