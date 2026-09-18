"""Minimal sitemap XML reader used to seed the crawler."""
from xml.etree import ElementTree


def parse_sitemap(xml_text: str) -> list[str]:
    if not xml_text.strip():
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    urls = []
    for loc in root.findall('.//{*}loc'):
        if loc.text and loc.text.strip():
            urls.append(loc.text.strip())
    return list(dict.fromkeys(urls))
