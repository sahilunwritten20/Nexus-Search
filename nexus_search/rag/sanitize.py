"""P7-5 — output sanitization: model output is UNTRUSTED (PHASE7_PLAN §2b).

The answer ships as PLAIN TEXT: HTML tags are stripped (script/style
blocks removed whole), and markdown images/links — the exfiltration /
tracking-pixel channel (`![x](http://attacker/...)`, `[x](http://...)`)
— are removed from model output entirely. Model output is never eval'd,
templated or f-string'd anywhere in this package (source-scan test).

`StreamSanitizer` applies the same rules across token boundaries, so a
payload split mid-token (`<scr` + `ipt>`, `![x](ht` + `tp://...)`) is
caught too (P7-7 uses it on every streamed chunk).

Renderer requirement for any future UI: render answer text as PLAIN
TEXT; do NOT auto-render markdown images/links from it. The API's
plain-text default is what makes that rule enforceable.
"""
import re
from typing import Optional, Protocol

# Whole-block removal first (so payloads inside script/style never leak
# as text), then any remaining HTML tags, then markdown link/image forms.
_SCRIPT_BLOCK_RE = re.compile(r"<\s*(script|style|iframe|object|embed|svg)\b.*"
                              r"<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_SCRIPT_OPEN_RE = re.compile(r"<\s*/?\s*(script|style|iframe|object|embed|svg)"
                             r"\b[^>]*>?", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^<>]{0,300}>")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[(?!!)([^\]\[]{1,200})\]\(([^)\s]{0,500})\)")
_URL_IN_PARENS_RE = re.compile(r"\((https?://[^)\s]{0,500})\)")


def sanitize_text(text: str) -> tuple[str, int]:
    """Plain-text answer text. Returns (clean_text, removal_count).
    removal_count counts how many dangerous constructs were dropped —
    recorded in metadata, the constructs themselves are never echoed."""
    if not text:
        return text, 0
    removed = 0
    text, n = _SCRIPT_BLOCK_RE.subn("", text)
    removed += n
    text, n = _SCRIPT_OPEN_RE.subn("", text)
    removed += n
    text, n = _HTML_TAG_RE.subn("", text)
    removed += n
    text, n = _MD_IMAGE_RE.subn("", text)
    removed += n
    # markdown links: keep the label text, drop the URL (the label may be
    # a legitimate citation like [1]; the parenthesized URL is the channel)
    text, n = _MD_LINK_RE.subn(lambda m: m.group(1), text)
    removed += n
    text, n = _URL_IN_PARENS_RE.subn("[link removed]", text)
    removed += n
    # strip any residual control chars that could smuggle formatting
    text = text.replace("\x00", "")
    return text, removed


# ---------------------------------------------------------------------------
# Prompt-leak scrubbing (the system prompt and fencing nonce are NEVER
# allowed in a response field — enforced here, not trusted to the model)
# ---------------------------------------------------------------------------

_FRAME_MARKER_RE = re.compile(r"={3,}\s*NXSRAG[-_]?[0-9a-fA-F]*[^=\n]{0,60}={3,}")


def _whitespace_flexible(pattern_text: str) -> re.Pattern:
    """Regex matching `pattern_text` with any whitespace run collapsed."""
    parts = [re.escape(p) for p in re.split(r"\s+", pattern_text) if p]
    return re.compile(r"\s+".join(parts), re.IGNORECASE)


def scrub_leaks(text: str, system_prompt: str,
                nonce: str = "") -> tuple[str, int]:
    """Remove echoed prompt frame markers, the fencing nonce, and any
    verbatim (whitespace-flexible) system-prompt sentence from an answer.
    HONEST LIMIT: lexical containment — a PARAPHRASED prompt leak is not
    catchable here (the model is told not to; a real leak lands in the
    P7-9 prompt-leak metric). Returns (clean_text, removal_count)."""
    if not text:
        return text, 0
    removed = 0
    text, n = _FRAME_MARKER_RE.subn("[prompt frame removed]", text)
    removed += n
    if nonce:
        text, n = re.subn(re.escape(nonce), "[removed]", text)
        removed += n
    if system_prompt:
        # whole prompt first, then each long sentence (partial echoes)
        candidates = [system_prompt]
        candidates += [s.strip() for s in re.split(r"(?<=[.!?])\s+",
                                                   system_prompt)
                       if len(s.strip()) >= 40]
        for candidate in candidates:
            text, n = _whitespace_flexible(candidate).subn(
                "[system prompt removed]", text)
            removed += n
    return text, removed


class OutputModerator(Protocol):
    """Optional output-moderation hook (interface only; the no-op default
    ships below). A real implementation can flag/blank answers that pass
    every lexical check but still look harmful — wiring one in is an
    operator decision, never silent."""

    def moderate(self, answer_text: str) -> Optional[str]:
        """Return the (possibly rewritten) answer, or None to pass through
        unchanged."""


class NoopModerator:
    def moderate(self, answer_text: str) -> Optional[str]:
        return None


class StreamSanitizer:
    """Stateful sanitizer for token streams: dangerous constructs are
    removed even when split across token boundaries.

    Design (correctness first): every push either (a) holds the buffer
    from the earliest UNFINISHED dangerous opener onward — including a
    trailing single `<`, `!`, `(` or `]` that may pair with the NEXT
    token (so `!` + `[` across token boundaries is caught) — or (b) when
    nothing is pending, consumes and sanitizes the ENTIRE buffer, so a
    completed construct is never cut mid-payload for the sanitizer to
    miss. The raw buffer therefore never holds safe text, and clean/raw
    can never misalign or duplicate."""

    def __init__(self):
        self._buffer = ""
        self.removed_count = 0

    # the openers that may pair with the NEXT token (single chars) or
    # with their closer (multi-char forms handled below)
    _TRAILING_OPENERS = ("!", "(", "<", "]")

    def push(self, token: str) -> str:
        """Feed one token; returns the text now safe to emit."""
        self._buffer += token
        hold_from = self._partial_from(self._buffer)
        if hold_from is None:
            consumed, self._buffer = self._buffer, ""
        else:
            consumed = self._buffer[:hold_from]
            self._buffer = self._buffer[hold_from:]
        clean, removed = sanitize_text(consumed)
        self.removed_count += removed
        return clean

    def flush(self) -> str:
        """Emit whatever a completed stream leaves in the buffer."""
        clean, removed = sanitize_text(self._buffer)
        self.removed_count += removed
        self._buffer = ""
        return clean

    # an open script-style block holds until its closing tag arrives —
    # otherwise the block BODY would stream out as inert-but-visible text
    _SCRIPT_OPEN_HOLD_RE = re.compile(
        r"<\s*(script|style|iframe|object|embed|svg)\b[^>]*>", re.IGNORECASE)

    @classmethod
    def _partial_from(cls, buffer: str):
        """Absolute index of the earliest unfinished dangerous construct
        in the buffer, or None when everything is safe to consume."""
        candidates = []
        m = cls._SCRIPT_OPEN_HOLD_RE.search(buffer)
        if m is not None:
            close_re = re.compile(
                r"<\s*/\s*" + re.escape(m.group(1)) + r"\s*>", re.IGNORECASE)
            if not close_re.search(buffer[m.end():]):
                candidates.append(m.start())
        lt = buffer.rfind("<")
        if lt != -1 and ">" not in buffer[lt:]:
            candidates.append(lt)
        for opener in ("![", "]("):
            i = buffer.rfind(opener)
            if i != -1 and ")" not in buffer[i:]:
                candidates.append(i)
        p = buffer.rfind("(")
        if p != -1 and ")" not in buffer[p:] and ("/" in buffer[p:] or ":" in buffer[p:]):
            candidates.append(p)
        for ch in cls._TRAILING_OPENERS:
            if buffer.endswith(ch):
                candidates.append(len(buffer) - 1)
        return min(candidates) if candidates else None


def sanitize_stream(tokens) -> tuple[list[str], int]:
    """Convenience: sanitize an iterable of tokens; returns
    (clean_tokens, removal_count)."""
    sanitizer = StreamSanitizer()
    out = [sanitizer.push(t) for t in tokens]
    out.append(sanitizer.flush())
    return [t for t in out if t], sanitizer.removed_count

