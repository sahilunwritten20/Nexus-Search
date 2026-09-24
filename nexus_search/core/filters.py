"""Metadata filters shared by every retriever (BM25 now, vectors in Phase 4)."""


def matches_filters(doc, filters: dict[str, str]) -> bool:
    """True if `doc` (anything with .doc_type and .metadata) satisfies the
    parsed `type:` / `lang:` filters."""
    if "doc_type" in filters and doc.doc_type.lower() != filters["doc_type"]:
        return False
    if "language" in filters and str(doc.metadata.get("language", "")).lower() != filters["language"]:
        return False
    return True


# Facet names map 1:1 onto the query filter vocabulary from query_parser.py:
# "type" -> doc.doc_type, "lang" -> doc.metadata["language"]. No new syntax.
FACET_FIELDS = {"doc_type", "language"}


def facet_counts(docs, fields: list[str] | None = None) -> dict[str, dict[str, int]]:
    """Facet counts for a set of already-retrieved documents.

    `docs` is anything iterable of objects with .doc_type / .metadata (the
    same shape matches_filters consumes). Unknown facet fields are skipped
    silently rather than erroring — facets are presentation data, not a
    contract a typo should hard-fail. Currently supported: doc_type, language.
    """
    fields = fields or ["doc_type", "language"]
    out: dict[str, dict[str, int]] = {}
    for field in fields:
        if field not in FACET_FIELDS:
            continue
        counts: dict[str, int] = {}
        for doc in docs:
            value = doc.doc_type if field == "doc_type" else str(doc.metadata.get("language", "") or "")
            if value:
                counts[value] = counts.get(value, 0) + 1
        out[field] = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    return out