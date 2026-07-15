from app.services.analysis.layout import detect_layout
from tests.fixtures.leipziger_liste_samples import (
    SAMPLE_MULTI_PAGE_TEXTS,
    SAMPLE_RAW_TEXT_PROSE,
    SAMPLE_RAW_TEXT_WITH_FOOTNOTES,
    SAMPLE_RAW_TEXT_WITH_TABLE,
)


def test_detect_layout_finds_footnotes():
    info = detect_layout(SAMPLE_RAW_TEXT_WITH_FOOTNOTES)
    assert info.has_footnotes is True
    assert len(info.footnote_lines) == 3
    assert any(line.startswith("*") for line in info.footnote_lines)
    assert any(line.startswith("1)") for line in info.footnote_lines)
    assert any(line.startswith("Anm.:") for line in info.footnote_lines)


def test_detect_layout_no_footnotes_in_prose():
    info = detect_layout(SAMPLE_RAW_TEXT_PROSE)
    assert info.has_footnotes is False
    assert info.footnote_lines == []


def test_detect_layout_no_footnotes_in_table():
    info = detect_layout(SAMPLE_RAW_TEXT_WITH_TABLE)
    assert info.has_footnotes is False


def test_detect_layout_uses_page_texts_when_available():
    joined = "\n\n".join(SAMPLE_MULTI_PAGE_TEXTS)
    info = detect_layout(joined, page_texts=SAMPLE_MULTI_PAGE_TEXTS)
    assert info.page_count_estimate == 2


def test_detect_layout_estimates_pages_without_page_texts():
    joined = "\n\n".join(SAMPLE_MULTI_PAGE_TEXTS)
    info = detect_layout(joined)
    assert info.page_count_estimate == 2


def test_detect_layout_empty_text():
    info = detect_layout("")
    assert info.page_count_estimate == 0
    assert info.has_footnotes is False
