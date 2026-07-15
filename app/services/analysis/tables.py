"""Heuristische Tabellenerkennung auf dem OCR-Rohtext: Zeilen mit einer konsistenten Anzahl
durch mehrere Leerzeichen/Tabs getrennter Spalten werden zu Bloecken gruppiert; ein Block
gilt als erkannte Tabelle ab einer Mindestzeilenzahl. Bewusst heuristisch (Regex, kein
Table-Detection-Modell) - siehe layout.py fuer die gleiche Einschraenkung."""

import re
from dataclasses import dataclass

COLUMN_SPLIT_PATTERN = re.compile(r"\s{2,}|\t+")


def _column_count(line: str) -> int:
    columns = [c for c in COLUMN_SPLIT_PATTERN.split(line.strip()) if c]
    return len(columns)


@dataclass
class TableInfo:
    table_row_count: int
    table_block_count: int


def detect_tables(raw_text: str, min_columns: int = 3, min_rows_per_block: int = 2) -> TableInfo:
    lines = raw_text.splitlines()
    column_counts = [_column_count(line) for line in lines]

    blocks: list[int] = []
    current_rows = 0
    current_columns: int | None = None

    for count in column_counts:
        matches_current = count >= min_columns and (
            current_columns is None or abs(count - current_columns) <= 1
        )
        if matches_current:
            current_columns = current_columns or count
            current_rows += 1
        else:
            if current_rows:
                blocks.append(current_rows)
            current_rows = 1 if count >= min_columns else 0
            current_columns = count if count >= min_columns else None
    if current_rows:
        blocks.append(current_rows)

    table_blocks = [rows for rows in blocks if rows >= min_rows_per_block]
    return TableInfo(table_row_count=sum(table_blocks), table_block_count=len(table_blocks))
