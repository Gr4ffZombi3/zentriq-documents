from app.services.analysis.tables import detect_tables
from tests.fixtures.leipziger_liste_samples import (
    SAMPLE_RAW_TEXT_PROSE,
    SAMPLE_RAW_TEXT_WITH_FOOTNOTES,
    SAMPLE_RAW_TEXT_WITH_TABLE,
)


def test_detect_tables_finds_table_block():
    info = detect_tables(SAMPLE_RAW_TEXT_WITH_TABLE)
    assert info.table_block_count == 1
    assert info.table_row_count == 4  # Kopfzeile + 3 Kundenzeilen, alle 3-spaltig


def test_detect_tables_no_table_in_prose():
    info = detect_tables(SAMPLE_RAW_TEXT_PROSE)
    assert info.table_block_count == 0
    assert info.table_row_count == 0


def test_detect_tables_no_table_in_footnote_text():
    info = detect_tables(SAMPLE_RAW_TEXT_WITH_FOOTNOTES)
    assert info.table_block_count == 0


def test_detect_tables_single_matching_row_is_not_a_block():
    # Nur 1 Zeile mit >=3 Spalten reicht nicht fuer einen erkannten Tabellen-Block
    # (min_rows_per_block=2 per Default).
    text = "Nur eine Zeile      mit   Spalten"
    info = detect_tables(text)
    assert info.table_block_count == 0
    assert info.table_row_count == 0


def test_detect_tables_empty_text():
    info = detect_tables("")
    assert info.table_block_count == 0
    assert info.table_row_count == 0
