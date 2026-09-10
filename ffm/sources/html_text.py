"""Minimal HTML-to-text, so page markup doesn't dominate the extraction prompt.

Deliberately stdlib-only: one more scraping dependency to keep current is not
worth it for what amounts to tag stripping. This is not a general-purpose
converter — it exists to turn a staff-picks page into readable prose.
"""

from __future__ import annotations

from html.parser import HTMLParser

#: Content of these elements is never text worth reading.
_SKIP = {"script", "style", "noscript", "svg", "head", "meta", "link"}

#: Elements that imply a line break, so table rows and list items stay separate.
_BLOCK = {
    "p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "table", "thead", "tbody", "header", "footer",
}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
        elif tag in _BLOCK:
            self.parts.append("\n")
        # Table cells read better separated than concatenated.
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text + " ")


def html_to_text(html: str) -> str:
    """Flatten HTML to readable text, collapsing runs of blank lines."""
    parser = _Extractor()
    parser.feed(html)
    raw = "".join(parser.parts)

    lines = [ln.strip(" |").strip() for ln in raw.splitlines()]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()
