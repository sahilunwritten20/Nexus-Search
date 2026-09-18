"""MIME type detection for Nexus Search."""

import mimetypes


def detect_mime_type(path: str) -> str:
    """Return the MIME type for a file path."""

    mime_type, _ = mimetypes.guess_type(path)

    return mime_type or "application/octet-stream"