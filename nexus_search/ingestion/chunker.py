"""Document text chunking for Nexus Search."""


def chunk_text(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 5,
) -> list[str]:
    """Split text into overlapping chunks."""

    if not text:
        return []

    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")

    if overlap < 0:
        raise ValueError("overlap cannot be negative")

    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    # If the whole text fits into one chunk,
    # return it without splitting.
    if len(text) <= chunk_size:
        return [text]

    chunks = []

    start = 0

    while start < len(text):
        end = start + chunk_size

        chunk = text[start:end]

        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        start = end - overlap

    return chunks