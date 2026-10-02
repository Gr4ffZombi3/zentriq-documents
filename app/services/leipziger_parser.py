"""Deterministischer Parser fuer die Leipziger Liste (Listenformat *WM312-L*).

Liest den nativen PDF-Text Seite fuer Seite, ohne OCR und ohne KI. Grundregeln:

- Jede Zeile, die mit einer Vertrags-/Vorgangsnummer beginnt (z. B. 720/307259-C-14 oder
  608/475130-N), ist genau EIN Vorgang. Ein Kunde kann beliebig viele Vorgaenge haben; es wird
  nie ueber den Kundennamen zusammengefasst.
- Zeilen darunter ohne Vorgangsnummer (Kennzeichen wie J / N / B / RP) gehoeren zum Vorgang
  darueber und beginnen keinen neuen Datensatz.
- Kopfzeilen (*WM312-L* ..., Spaltenueberschriften, Spaltennummern) werden auf jeder Seite
  uebersprungen.
- Beschriftete Felder (GEB.-DAT., BEGINN, VM-NR.) werden ausschliesslich hinter ihrer eigenen
  Beschriftung gelesen. Steht hinter BEGINN: kein Datum, bleibt das Datum leer - es wird nie
  ein anderes Datum der Seite uebernommen.
- Name, PLZ und Ort werden nur uebernommen, wenn die Zeile dem bekannten Aufbau
  "Name PLZ Ort GEB.-DAT.:" eindeutig entspricht; sonst bleibt das Feld leer und die Zeile
  zaehlt als nicht eindeutig lesbar."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

LIST_MARKER = "*WM312"

NUMBER_PATTERN = r"\d{3}/\d{6}-[A-Z](?:-\d{2})?"
RECORD_START = re.compile(rf"^\s*(?P<number>{NUMBER_PATTERN})(?=\s|$)")
# Nummer, Status (ANG/NEU/FZW ...), Art (PH, RS, KPKW ...), Rest der Zeile.
RECORD_HEAD = re.compile(rf"^\s*(?P<number>{NUMBER_PATTERN})\s+(?P<status>[A-Z]{{2,5}})\s+(?P<art>[A-Z]{{1,5}})\s+(?P<rest>.*)$")
# Rest: Name, fuenfstellige PLZ, Ort, dann die beschrifteten Felder.
PERSON = re.compile(r"^(?P<name>\S.*?)\s+(?P<plz>\d{5})\s+(?P<city>\S.*?)\s+GEB\.-DAT\.:")
LABELS = ("GEB.-DAT.:", "BEGINN:", "ABO:", "VM-NR.:")
DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
BROKER = re.compile(r"^\d{2}/\d{4}-[A-Z0-9]{1,2}$")
# Berichtsdatum im Seitenkopf: "*WM312-L* ... GS 08   COBURG, 19.07.2026   SEITE 64".
REPORT_DATE = re.compile(r"[A-ZÄÖÜ][A-ZÄÖÜa-zäöüß.\- ]*,\s*(\d{2}\.\d{2}\.\d{4})\b")

HEADER_LINES = (
    re.compile(r"^\s*\*WM\d+"),
    re.compile(r"^\s*MM\s+HAT\b"),
    re.compile(r"^\s*OUT\s+TEL\b"),
    re.compile(r"^\s*ZAD\b"),
    re.compile(r"^[\s\d]+$"),  # Spaltennummern 1 2 3 ... 27
)
# Kennzeichen-/Folgezeile: nur kurze Grossbuchstaben-/Ziffern-Token, keine Kleinbuchstaben.
FLAG_TOKEN = re.compile(r"^[A-Z0-9*_\-/.<=]{1,8}$")

STATUS_FLAGS = {
    "ANG": "is_angebot",
    "NEU": "is_neugeschaeft",
    "FZW": "is_fahrzeugwechsel",
    "STO": "is_storno",
    "STORNO": "is_storno",
}


@dataclass
class ParsedRecord:
    number: str
    status: str | None
    art: str | None
    customer_name: str | None
    postal_code: str | None
    city: str | None
    date_of_birth: date | None
    start_date: date | None
    broker_number: str | None
    page: int
    row: int
    uncertain: bool = False
    issues: list[str] = field(default_factory=list)

    def identity(self) -> tuple:
        """Exakt identischer Vorgang: alle gelesenen Werte gleich (nicht nur der Name)."""
        return (
            self.number,
            self.status,
            self.art,
            self.customer_name,
            self.postal_code,
            self.city,
            self.date_of_birth,
            self.start_date,
            self.broker_number,
        )


@dataclass
class ParseResult:
    records: list[ParsedRecord]
    page_count: int
    pages_with_records: list[int]
    duplicate_count: int
    unreadable_lines: list[tuple[int, int]]  # (Seite, Zeile) - bewusst ohne Inhalt (Personendaten)
    flag_line_count: int

    @property
    def with_date(self) -> int:
        return sum(1 for record in self.records if record.start_date is not None)

    @property
    def without_date(self) -> int:
        return sum(1 for record in self.records if record.start_date is None)

    @property
    def uncertain_count(self) -> int:
        return sum(1 for record in self.records if record.uncertain)

    @property
    def unreadable_count(self) -> int:
        """Nicht eindeutig lesbar: unbekannte Zeilen plus Vorgaenge mit unsicheren Feldern."""
        return len(self.unreadable_lines) + self.uncertain_count

    def stats(self) -> dict:
        return {
            "parser": "wm312",
            "pdf_pages": self.page_count,
            "records": len(self.records),
            "with_date": self.with_date,
            "without_date": self.without_date,
            "duplicates": self.duplicate_count,
            "unreadable": self.unreadable_count,
            "unreadable_lines": len(self.unreadable_lines),
            "uncertain_records": self.uncertain_count,
            "flag_lines": self.flag_line_count,
            "pages_with_records": self.pages_with_records,
        }


def is_wm312_list(page_texts: list[str]) -> bool:
    """Erkennt das Listenformat am Seitenkopf und an mindestens einer Vorgangszeile."""
    has_marker = any(LIST_MARKER in (text or "") for text in page_texts)
    has_record = any(RECORD_HEAD.match(line) for text in page_texts for line in (text or "").splitlines())
    return has_marker and has_record


def _parse_date(value: str | None) -> date | None:
    match = DATE.match((value or "").strip())
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def detect_report_date(page_texts: list[str]) -> date | None:
    """Berichtsdatum der Liste aus der ersten Kopfzeile ("ORT, TT.MM.JJJJ") - Grundlage fuer
    Berichtsjahr/Kalenderwoche und damit fuer die Wahl der Vorgaengerliste beim Vergleich.
    Gesucht wird nur in den Kopfzeilen vor dem ersten Vorgang, nie in Vorgangszeilen."""
    for text in page_texts[:2]:
        for line in (text or "").splitlines():
            if RECORD_START.match(line):
                break
            match = REPORT_DATE.search(line)
            if match and (found := _parse_date(match.group(1))):
                return found
    return None


def _labeled_value(text: str, label: str) -> str | None:
    """Text direkt hinter `label` bis zur naechsten bekannten Beschriftung. None, wenn die
    Beschriftung fehlt; "" wenn sie vorhanden, aber leer ist."""
    start = text.find(label)
    if start < 0:
        return None
    start += len(label)
    end = len(text)
    for other in LABELS:
        position = text.find(other, start)
        if position >= 0:
            end = min(end, position)
    return text[start:end].strip()


def _parse_record(line: str, page: int, row: int) -> ParsedRecord:
    number = RECORD_START.match(line).group("number")
    record = ParsedRecord(
        number=number,
        status=None,
        art=None,
        customer_name=None,
        postal_code=None,
        city=None,
        date_of_birth=None,
        start_date=None,
        broker_number=None,
        page=page,
        row=row,
    )
    head = RECORD_HEAD.match(line)
    if not head:
        record.uncertain = True
        record.issues.append("aufbau")
        rest = line[RECORD_START.match(line).end() :]
    else:
        record.status = head.group("status")
        record.art = head.group("art")
        rest = head.group("rest")

    person = PERSON.match(rest)
    if person:
        record.customer_name = " ".join(person.group("name").split())
        record.postal_code = person.group("plz")
        record.city = " ".join(person.group("city").split())
    else:
        record.uncertain = True
        record.issues.append("kunde")

    birth = _labeled_value(rest, "GEB.-DAT.:")
    if birth:
        record.date_of_birth = _parse_date(birth)
        if record.date_of_birth is None:
            record.uncertain = True
            record.issues.append("geburtsdatum")

    start = _labeled_value(rest, "BEGINN:")
    if start is None:
        record.uncertain = True
        record.issues.append("beginn_fehlt")
    elif start:
        record.start_date = _parse_date(start)
        if record.start_date is None:
            # Etwas steht hinter BEGINN:, ist aber kein gueltiges Datum: nicht raten.
            record.uncertain = True
            record.issues.append("beginn_unlesbar")

    broker = _labeled_value(rest, "VM-NR.:")
    broker = broker.split()[0] if broker else None
    if broker and BROKER.match(broker):
        record.broker_number = broker
    else:
        record.uncertain = True
        record.issues.append("vermittler")
    return record


def _is_header(line: str) -> bool:
    return any(pattern.match(line) for pattern in HEADER_LINES)


def _is_flag_line(line: str) -> bool:
    tokens = line.split()
    return bool(tokens) and all(FLAG_TOKEN.match(token) for token in tokens)


def parse_pages(page_texts: list[str]) -> ParseResult:
    records: list[ParsedRecord] = []
    seen: set[tuple] = set()
    duplicates = 0
    unreadable: list[tuple[int, int]] = []
    flag_lines = 0
    pages_with_records: list[int] = []
    # Folgezeilen gehoeren zum letzten Vorgang - auch nach einem Seitenumbruch.
    after_record = False

    for page_number, text in enumerate(page_texts, start=1):
        row_on_page = 0
        for line_number, line in enumerate((text or "").splitlines(), start=1):
            if not line.strip() or _is_header(line):
                continue
            if RECORD_START.match(line):
                row_on_page += 1
                record = _parse_record(line, page_number, row_on_page)
                after_record = True
                if record.identity() in seen:
                    duplicates += 1
                    continue
                seen.add(record.identity())
                records.append(record)
                if page_number not in pages_with_records:
                    pages_with_records.append(page_number)
                continue
            if after_record and _is_flag_line(line):
                flag_lines += 1
                continue
            unreadable.append((page_number, line_number))

    return ParseResult(
        records=records,
        page_count=len(page_texts),
        pages_with_records=pages_with_records,
        duplicate_count=duplicates,
        unreadable_lines=unreadable,
        flag_line_count=flag_lines,
    )


def to_extraction(result: ParseResult):
    """Uebersetzt die Vorgaenge in das bestehende Zeilenformat der Leipziger-Auswertung."""
    from app.services.llm.schemas import ExtractedCustomer, LeipzigerListeExtraction, LeipzigerListeRow

    rows = []
    for record in result.records:
        flags = {flag: False for flag in set(STATUS_FLAGS.values())}
        flag = STATUS_FLAGS.get(record.status or "")
        if flag:
            flags[flag] = True
        rows.append(
            LeipzigerListeRow(
                customer=ExtractedCustomer(
                    name=record.customer_name or "Unbekannt",
                    postal_code=record.postal_code,
                    city=record.city,
                    date_of_birth=record.date_of_birth,
                ),
                contract_number=record.number,
                status_code=record.status,
                product_line=record.art,
                broker_number=record.broker_number,
                contract_start_date=record.start_date,
                source_page=record.page,
                source_row=record.row,
                special_notes="Nicht eindeutig lesbar: " + ", ".join(record.issues) if record.uncertain else None,
                **flags,
            )
        )
    return LeipzigerListeExtraction(
        rows=rows,
        analysis_meta={
            "total_pages": result.page_count,
            "processed_pages": result.page_count,
            "processed_page_numbers": list(range(1, result.page_count + 1)),
            "failed_pages": [],
            "failed_page_count": 0,
            "raw_row_count": len(rows) + result.duplicate_count,
            "batch_size": 1,
            "parser_stats": result.stats(),
        },
    )
