"""Heuristische Layout-Erkennung auf dem OCR-Rohtext (Regex/stdlib). Bewusst KEINE echte
Computer-Vision-Layoutanalyse - im Techstack existiert keine entsprechende Bibliothek und
eine neue schwere Abhaengigkeit dafuer waere fuer diesen Meilenstein unverhaeltnismaessig.
Diese Einschraenkung ist im Abschlussbericht dokumentiert."""

import re
from dataclasses import dataclass, field

FOOTNOTE_PATTERN = re.compile(
    r"^\s*(?:[*†‡]|\d+\)|Anm\.:|Hinweis:|Fußnote)", re.IGNORECASE
)


@dataclass
class LayoutInfo:
    page_count_estimate: int
    has_footnotes: bool
    footnote_lines: list[str] = field(default_factory=list)


def detect_layout(raw_text: str, page_texts: list[str] | None = None) -> LayoutInfo:
    """`page_texts` (aus extract_text(), falls verfuegbar) liefert die exakte Seitenzahl;
    ohne sie wird ueber die "\\n\\n"-Trennung (siehe pipeline.py) grob geschaetzt."""
    if page_texts is not None:
        page_count_estimate = len(page_texts)
    elif raw_text:
        page_count_estimate = raw_text.count("\n\n") + 1
    else:
        page_count_estimate = 0

    footnote_lines = [line.strip() for line in raw_text.splitlines() if FOOTNOTE_PATTERN.match(line)]

    return LayoutInfo(
        page_count_estimate=page_count_estimate,
        has_footnotes=bool(footnote_lines),
        footnote_lines=footnote_lines,
    )
