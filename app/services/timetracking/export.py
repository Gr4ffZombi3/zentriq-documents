"""Monatsexport der Arbeitszeiten (CSV fuer die Lohnabrechnung, PDF zur Archivierung).

CSV: Semikolon-getrennt, UTF-8 mit BOM - oeffnet sich in einem deutschen Excel direkt korrekt.
PDF: schlichte Tabelle per PyMuPDF (bereits Abhaengigkeit der OCR), keine weitere Bibliothek."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

import fitz

from app.services.timetracking.calc import PeriodSummary, format_duration
from app.services.timetracking.clock import to_local, utcnow_naive
from app.template_filters import month_name, weekday_short


def _time(value: datetime | None) -> str:
    local = to_local(value)
    return local.strftime("%H:%M") if local else ""


def _note(day) -> str:
    notes = []
    if day.holiday:
        notes.append(f"Feiertag: {day.holiday}")
    elif day.absence_kind == "sick":
        notes.append("Krank")
    elif day.absence_kind == "vacation":
        notes.append("Urlaub")
    if day.is_running:
        notes.append("läuft")
    if day.has_incomplete:
        notes.append("Ausstempeln fehlt")
    if day.is_corrected:
        notes.append("korrigiert")
    return ", ".join(notes)


def day_rows(summary: PeriodSummary, today: date) -> list[list[str]]:
    rows = []
    for day in summary.days:
        has_data = bool(day.sessions) or bool(day.target_seconds)
        rows.append(
            [
                day.day.strftime("%d.%m.%Y"),
                weekday_short(day.day),
                _time(day.first_start),
                _time(day.last_end),
                format_duration(day.break_seconds) if day.sessions else "",
                format_duration(day.net_seconds) if day.sessions else "",
                format_duration(day.target_seconds) if day.target_seconds else "",
                format_duration(day.balance_seconds, signed=True) if has_data and day.day <= today else "",
                format_duration(day.sick_seconds) if day.sick_seconds else "",
                format_duration(day.vacation_seconds) if day.vacation_seconds else "",
                _note(day),
            ]
        )
    return rows


HEADER = ["Datum", "Tag", "Beginn", "Ende", "Pausen", "Ist", "Soll", "Saldo", "Krank", "Urlaub", "Hinweis"]
LEGAL_NOTE = ("Die Krankheitszeiträume dokumentieren Ausfallzeiten als Grundlage zur Prüfung des Erstattungsantrags; "
              "ein Erstattungsanspruch oder -betrag ergibt sich daraus nicht.")


def _totals(summary: PeriodSummary) -> list[str]:
    return [
        "Summe",
        "",
        "",
        "",
        format_duration(summary.break_seconds),
        format_duration(summary.net_seconds),
        format_duration(summary.target_seconds),
        format_duration(summary.balance_seconds, signed=True),
        format_duration(summary.sick_seconds),
        format_duration(summary.vacation_seconds),
        "",
    ]


def _sick_lines(sick_rows) -> list[list[str]]:
    """Krankheitszeitraeume des Monats (absences.OverviewRow)."""
    lines = []
    for row in sick_rows or ():
        a = row.absence
        period = a.start_date.strftime("%d.%m.%Y") + ("" if a.end_date == a.start_date else " – " + a.end_date.strftime("%d.%m.%Y"))
        lines.append([period, str(row.month_days), format_duration(row.month_seconds), a.status_label])
    return lines


def month_csv(employee_name: str, personnel_number: str | None, summary: PeriodSummary, today: date, sick_rows=None) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Arbeitszeitnachweis", month_name(summary.start)])
    writer.writerow(["Mitarbeiter", employee_name])
    if personnel_number:
        writer.writerow(["Personalnummer", personnel_number])
    writer.writerow([])
    writer.writerow(HEADER)
    writer.writerows(day_rows(summary, today))
    writer.writerow(_totals(summary))
    sick = _sick_lines(sick_rows)
    if sick:
        writer.writerow([])
        writer.writerow(["Krankheitszeiträume (Grundlage zur Prüfung des Erstattungsantrags)"])
        writer.writerow(["Zeitraum", "Ausgefallene Arbeitstage im Monat", "Krankheitsstunden im Monat", "Bearbeitungsstatus"])
        writer.writerows(sick)
        writer.writerow([LEGAL_NOTE])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def team_csv(rows: list[tuple[str, str | None, PeriodSummary]], month: date) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(["Arbeitszeiten", month_name(month)])
    writer.writerow([])
    writer.writerow(["Mitarbeiter", "Personalnummer", "Ist", "Soll", "Saldo", "Pausen", "Unvollständige Buchungen",
                     "Krankheitstage", "Krankheitsstunden", "Urlaubstage", "Urlaubsstunden"])
    for name, personnel_number, summary in rows:
        writer.writerow(
            [
                name,
                personnel_number or "",
                format_duration(summary.net_seconds),
                format_duration(summary.target_seconds),
                format_duration(summary.balance_seconds, signed=True),
                format_duration(summary.break_seconds),
                "ja" if summary.has_incomplete else "nein",
                str(summary.sick_days),
                format_duration(summary.sick_seconds),
                str(summary.vacation_days),
                format_duration(summary.vacation_seconds),
            ]
        )
    return ("﻿" + buffer.getvalue()).encode("utf-8")


# --- PDF ------------------------------------------------------------------------------------

_PAGE_WIDTH, _PAGE_HEIGHT = fitz.paper_size("a4")
_MARGIN = 40
_COLUMNS = [  # (Kopf, Breite in pt, rechtsbuendig)
    ("Datum", 58, False),
    ("Tag", 24, False),
    ("Beginn", 38, True),
    ("Ende", 36, True),
    ("Pausen", 40, True),
    ("Ist", 38, True),
    ("Soll", 38, True),
    ("Saldo", 44, True),
    ("Krank", 38, True),
    ("Urlaub", 40, True),
    ("Hinweis", 121, False),
]
_ROW_HEIGHT = 17
_FONT_SIZE = 8.5


def _text(page, x, y, value, size=_FONT_SIZE, bold=False, color=(0.1, 0.13, 0.17)):
    # Die PDF-Standardschrift kennt das typografische Minus nicht.
    value = value.replace("\u2212", "-")
    page.insert_text((x, y), value, fontsize=size, fontname="hebo" if bold else "helv", color=color)


def _row(page, y, values, bold=False, shade=False):
    if shade:
        page.draw_rect(fitz.Rect(_MARGIN, y - 12, _PAGE_WIDTH - _MARGIN, y + 5), color=None, fill=(0.95, 0.96, 0.97))
    x = _MARGIN + 4
    for (_, width, right), value in zip(_COLUMNS, values, strict=False):
        if right:
            text_width = fitz.get_text_length(value.replace("\u2212", "-"), fontname="hebo" if bold else "helv", fontsize=_FONT_SIZE)
            _text(page, x + width - 8 - text_width, y, value, bold=bold)
        else:
            _text(page, x, y, value, bold=bold)
        x += width
    page.draw_line(fitz.Point(_MARGIN, y + 5), fitz.Point(_PAGE_WIDTH - _MARGIN, y + 5), color=(0.85, 0.87, 0.9), width=0.5)


def month_pdf(
    office_name: str, employee_name: str, personnel_number: str | None, summary: PeriodSummary, today: date,
    sick_rows=None,
) -> bytes:
    document = fitz.open()
    page = document.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
    y = _MARGIN + 10
    _text(page, _MARGIN, y, "Arbeitszeitnachweis " + month_name(summary.start), size=14, bold=True)
    y += 20
    meta = f"{employee_name}" + (f" · Personalnummer {personnel_number}" if personnel_number else "") + f" · {office_name}"
    _text(page, _MARGIN, y, meta, size=9.5, color=(0.32, 0.36, 0.42))
    y += 14
    totals = (
        f"Ist {format_duration(summary.net_seconds)} Std. · Soll {format_duration(summary.target_seconds)} Std. · "
        f"Differenz {format_duration(summary.balance_seconds, signed=True)} Std. · Pausen {format_duration(summary.break_seconds)} Std."
    )
    if summary.sick_seconds or summary.vacation_seconds:
        totals += (f" · Krank {format_duration(summary.sick_seconds)} Std. ({summary.sick_days} Tage)"
                   f" · Urlaub {format_duration(summary.vacation_seconds)} Std.")
    _text(page, _MARGIN, y, totals, size=9.5)
    y += 26

    _row(page, y, [label for label, _, _ in _COLUMNS], bold=True, shade=True)
    for values in day_rows(summary, today):
        y += _ROW_HEIGHT
        if y > _PAGE_HEIGHT - _MARGIN - 40:
            page = document.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
            y = _MARGIN + 10
            _row(page, y, [label for label, _, _ in _COLUMNS], bold=True, shade=True)
            y += _ROW_HEIGHT
        _row(page, y, values)
    y += _ROW_HEIGHT
    _row(page, y, _totals(summary), bold=True, shade=True)

    sick = _sick_lines(sick_rows)
    if sick:
        if y > _PAGE_HEIGHT - _MARGIN - 80 - _ROW_HEIGHT * len(sick):
            page = document.new_page(width=_PAGE_WIDTH, height=_PAGE_HEIGHT)
            y = _MARGIN
        y += 30
        _text(page, _MARGIN, y, "Krankheitszeiträume – Grundlage zur Prüfung des Erstattungsantrags", size=10, bold=True)
        for period, days, hours, status in sick:
            y += _ROW_HEIGHT
            _text(page, _MARGIN + 4, y, f"{period}: {days} ausgefallene Arbeitstage, {hours} Std. Krankheit · Status: {status}")
        y += _ROW_HEIGHT
        _text(page, _MARGIN + 4, y, LEGAL_NOTE[:118], size=7.5, color=(0.32, 0.36, 0.42))
        if len(LEGAL_NOTE) > 118:
            _text(page, _MARGIN + 4, y + 10, LEGAL_NOTE[118:].strip(), size=7.5, color=(0.32, 0.36, 0.42))

    footer = f"Erstellt am {to_local(utcnow_naive()).strftime('%d.%m.%Y %H:%M')} Uhr · Zentriq"
    for number, current in enumerate(document, start=1):
        _text(current, _MARGIN, _PAGE_HEIGHT - 24, footer, size=7.5, color=(0.45, 0.5, 0.56))
        _text(current, _PAGE_WIDTH - _MARGIN - 40, _PAGE_HEIGHT - 24, f"Seite {number}/{len(document)}", size=7.5, color=(0.45, 0.5, 0.56))
    data = document.tobytes()
    document.close()
    return data
