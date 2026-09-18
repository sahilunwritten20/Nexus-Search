"""Content quality scoring for Nexus Search."""


def content_quality_score(text: str) -> float:
    """Return a simple content quality score between 0 and 1."""

    if not text or not text.strip():
        return 0.0

    text = text.strip()

    score = 0.0

    # Has reasonable length
    if len(text) >= 100:
        score += 0.4
    elif len(text) >= 50:
        score += 0.2

    # Contains multiple words
    words = text.split()

    if len(words) >= 20:
        score += 0.3
    elif len(words) >= 10:
        score += 0.15

    # Contains sentence-like structure
    if "." in text or "!" in text or "?" in text:
        score += 0.2

    # Contains useful alphanumeric content
    if any(char.isalnum() for char in text):
        score += 0.1

    return min(score, 1.0)