"""Tiny HTML-to-text helper shared by adapters that get HTML description fields back
from an API (Greenhouse's `content`, Amazon's `description`/`basic_qualifications`).

Not a full HTML parser - just enough to give the pipeline's keyword scan and Nova Lite
calls clean text instead of markup, without pulling in a parsing dependency.
"""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_BLOCK_BREAK_RE = re.compile(r"</(p|div|li|br|h[1-6])\s*/?>", re.IGNORECASE)


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = _BLOCK_BREAK_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()
