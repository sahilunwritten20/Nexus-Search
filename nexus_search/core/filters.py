"""Metadata filters shared by every retriever (BM25 now, vectors in Phase 4)."""


def matches_filters(doc, filters: dict[str, str]) -> bool:
    """True if `doc` (anything with .doc_type and .metadata) satisfies the
    parsed `type:` / `lang:` filters."""
    if "doc_type" in filters and doc.doc_type.lower() != filters["doc_type"]:
        return False
    if "language" in filters and str(doc.metadata.get("language", "")).lower() != filters["language"]:
        return False
    return True