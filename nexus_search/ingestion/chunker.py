"""Document text chunking for Nexus Search."""


def chunk_text(
    text: str,
    chunk_size: int = 1000,
    overlap: int = 5,
    snap_to_space: bool = False,
) -> list[str]:
    """Split text into overlapping chunks.
    snap_to_space=True ends each chunk at the last space instead of mid-word."""
    if not text:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")
    if overlap < 0:
        raise ValueError("overlap cannot be negative")
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if snap_to_space and end < len(text):
            cut = text.rfind(" ", start + chunk_size // 2, end)
            if cut != -1:
                end = cut
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)  # always make progress
    return chunks