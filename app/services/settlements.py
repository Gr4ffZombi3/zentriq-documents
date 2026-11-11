"""Abrechnungen: automatische Mitarbeiteruebersicht aus der HUK-Werbegebuehrenabrechnung - ohne KI.

Lesen (parse_huk): eingebetteter PDF-Text mit Wortpositionen, ausschliesslich lokal. Gezaehlt werden
nur Zeilen der "Einzelübersicht"; der Betrag kommt ausschliesslich aus der Spalte "Betrag in Euro".
Deckblatt, Uebertraege, Zielerreichung, Zwischensummen ("Produktion in ...", "Ihre ...") und die
Summenseiten fliessen nie ein.

Mitarbeiterbloecke: Jede Tabellenzeile gehoert zum zuletzt gelesenen Block "VM-Nr. 084206N, Name" -
auch wenn der Block mitten auf einer Seite wechselt oder auf der naechsten Seite weiterlaeuft. Der
Name oben auf der Seite ist der Empfaenger der Gesamtabrechnung und wird NICHT zur Zuordnung
verwendet. Zugeordnet wird beim Anzeigen ueber die VM-Nummer der aktiven Buero-Mitglieder - ohne
manuelle Pruefung oder Zuordnung des Belegs zu einem einzelnen Mitarbeiter.

Zaehlen (overview):
- Neuabschluss: ein Vertrag (Vertragsnummer der Hauptzeile) zaehlt einmal, wenn er mindestens eine
  positive Verguetungszeile ausserhalb der 10-€-Positionen hat und die Summe dieser Zeilen > 0 ist.
  Unterzeilen (Risiken, Folgezeilen auch ueber den Seitenwechsel), Stornos und 10-€-Positionen
  erhoehen die Zahl nicht.
- Verguetung: Summe aller Zeilen "Betrag in Euro" ohne 10-€-Positionen (Stornos mindern sie).
- 10-€-Position: Betrag der einzelnen Zeile genau +10,00 oder -10,00 - separat mit Anzahl und Summe.

Vollstaendigkeit: Die Summe aller Einzelzeilen muss dem Betrag "Ihre Werbegebühren" entsprechen;
Seiten ohne lesbaren Text oder ohne erkennbaren Tabellenkopf, Betraege ohne Block oder Vertrag
machen die Auswertung "unvollständig" (kompakter Hinweis, keine Pruefliste, keine Null-Werte).

Neuauswertung (evaluate): dieselbe Auswertung wie beim Upload; das alte Ergebnis wird erst nach
erfolgreichem Lesen ersetzt - im selben Beleg, ohne Dubletten."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

TEN = Decimal("10.00")
ZERO = Decimal("0")
# Ergebnisse dieser Version zaehlen; aeltere Auswertungen (Zeilen-Parser, Pruefliste) erst nach "Neu auswerten".
EVALUATION_VERSION = 2
COUNTED_STATUSES = ("done", "incomplete")
MONTHS = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7, "august": 8,
          "september": 9, "oktober": 10, "november": 11, "dezember": 12}
MONTH_NAMES = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember")

_AMOUNT = re.compile(r"(?<![\d,.])(-\s?)?(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})(\s?-)?(?:\s?(?:€|EUR))?(?![\d,])")
_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_PERIOD_RANGE = re.compile(r"(?:abrechnungs)?zeitraum\s*:?\s*(\d{1,2}\.\d{1,2}\.\d{4})\s*(?:-|–|bis)\s*(\d{1,2}\.\d{1,2}\.\d{4})", re.I)
_PERIOD_MONTH = re.compile(r"(?:abrechnungs(?:monat|zeitraum)|monat|zeitraum)\s*:?\s*(" + "|".join(MONTHS) + r")\s+(\d{4})", re.I)

# HUK-Risiko-/Vertragsarten -> Sparte (Annahme nach HUK-Kuerzeln; unbekannte Kuerzel erscheinen als
# "Kürzel XYZ" und werden trotzdem gezaehlt).
HUK_CODES = {
    "Kfz": ("KH", "FV", "FT", "FAU", "ASS", "MOH", "MOF"),
    "Haftpflicht": ("PHPL", "PHC", "THH"),
    "Hausrat": ("HC", "HPL"),
    "Wohngebäude": ("WGC", "WPL"),
    "Unfall": ("UC", "UCD", "UPL"),
    "Kranken": ("RK", "CFZ", "CFV", "CFVP", "CHWS", "CHWG"),
}
_CODE_SPARTE = {code: sparte for sparte, codes in HUK_CODES.items() for code in codes}
HUK_NO_SPARTE = ("FAB",)  # Festbetrag, keine eigene Sparte (erbt die Sparte des Vertrags)
_HUK_CONTRACT = re.compile(r"^\d{3}/\d{6}-[A-Z](?:-\d{2})?$")
_HUK_VM = re.compile(r"^(\d{6}[A-Z]),?$")
_HUK_AMOUNT = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}-?$")
_HUK_SKIP_FIRST = ("Produktion", "Ihre", "Seite:", "Übertrag", "Uebertrag", "Zielerreichung", "Summe", "Gesamt")


class SettlementError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- Zerlegen -------------------------------------------------------------------------------------


@dataclass
class ParsedLine:
    page: int
    position: int
    raw: str
    amount: Decimal | None
    booking_date: date | None  # Antragsdatum des Vertrags
    contract_ref: str | None
    sparte: str | None
    booking_type: str | None  # abschluss (Betrag >= 0) | storno (Betrag < 0)
    booking_label: str | None  # HUK-Kuerzel der Zeile
    broker_number: str | None = None  # VM-Nummer des Tabellenblocks

    @property
    def is_ten(self) -> bool:
        return self.amount is not None and abs(self.amount) == TEN


@dataclass
class ParsedSettlement:
    period_start: date | None = None
    period_end: date | None = None
    lines: list[ParsedLine] = field(default_factory=list)
    blocks: dict = field(default_factory=dict)  # VM-Nummer -> Name laut Beleg
    document_total: Decimal | None = None  # "Ihre Werbegebühren"
    incomplete: list[str] = field(default_factory=list)


def parse_amount(text: str) -> Decimal | None:
    matches = list(_AMOUNT.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    try:
        value = Decimal(m.group(2).replace(".", "") + "." + m.group(3))
    except InvalidOperation:
        return None
    return -value if (m.group(1) or m.group(4)) else value


def _date(value: str) -> date | None:
    m = _DATE.search(value or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _month_end(year: int, month: int) -> date:
    from calendar import monthrange

    return date(year, month, monthrange(year, month)[1])


def parse_period(text: str) -> tuple[date | None, date | None]:
    """Abrechnungszeitraum aus dem Deckblatt (Zeitraum von-bis oder Abrechnungsmonat)."""
    m = _PERIOD_RANGE.search(text)
    if m:
        return _date(m.group(1)), _date(m.group(2))
    m = _PERIOD_MONTH.search(text)
    if m:
        month, year = MONTHS[m.group(1).lower()], int(m.group(2))
        return date(year, month, 1), _month_end(year, month)
    return None, None


def _huk_date(value: str) -> date | None:
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{2})", value or "")
    if not m:
        return None
    try:
        return date(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def huk_sparte(code: str | None) -> str | None:
    if not code or code in HUK_NO_SPARTE:
        return None
    return _CODE_SPARTE.get(code, f"Kürzel {code}")


def _money(value: Decimal) -> str:
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def parse_huk(data: bytes) -> ParsedSettlement | None:
    """HUK-Werbegebuehrenabrechnung ueber Wortpositionen. None, wenn keine Einzelübersicht mit
    Spalte "Betrag in Euro" (und eingebettetem Text) vorhanden ist."""
    import fitz

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001
        return None
    pages = []
    try:
        if document.page_count > 80:
            raise SettlementError("Die PDF hat mehr als 80 Seiten.")
        for page in document:
            pages.append((page.get_text("text"), page.get_text("words")))
    finally:
        document.close()
    detail_pages = [i for i, (text, _w) in enumerate(pages) if "Einzelübersicht" in text and "Betrag" in text and "in Euro" in text]
    if not detail_pages:
        return None
    result = ParsedSettlement()
    result.period_start, result.period_end = parse_period(pages[0][0])
    if result.period_start is None:
        result.incomplete.append("Abrechnungszeitraum nicht erkannt")
    # Seiten ohne lesbaren Text zwischen der ersten und letzten Einzelübersicht (z. B. eingescannt)
    for index in range(detail_pages[0], detail_pages[-1] + 1):
        if len(pages[index][0].strip()) < 20:
            result.incomplete.append(f"Seite {index + 1} ohne lesbaren Text")
    position, vm, contract, contract_sparte, contract_date = 0, None, None, None, None
    orphan_amounts = 0
    for index in detail_pages:
        words = pages[index][1]
        head = {w[4]: w for w in words if w[4] in ("Betrag", "Vertragsnummer", "Risiko-/", "Antrags-", "Beginn-/")}
        if "Betrag" not in head or "Vertragsnummer" not in head:
            result.incomplete.append(f"Seite {index + 1}: Tabellenkopf nicht erkannt")
            continue
        amount_left = head["Betrag"][0] - 25  # rechtsbuendige Spalte "Betrag in Euro"
        code_left = head["Risiko-/"][0] - 6 if "Risiko-/" in head else head["Vertragsnummer"][2] + 100
        code_right = code_left + 30  # Kuerzel beginnt am Spaltenanfang (danach folgt z. B. Bonus "J")
        contract_left, contract_right = head["Vertragsnummer"][0] - 6, head["Vertragsnummer"][2] + 25
        date_left = head["Antrags-"][0] - 6 if "Antrags-" in head else contract_right
        date_right = head["Beginn-/"][0] - 2 if "Beginn-/" in head else date_left + 40
        table_top = max(w[3] for w in words if w[4] in ("Euro", "Werbegebühr") and w[1] >= head["Betrag"][1] - 2)
        rows: dict = {}
        margin = head["Vertragsnummer"][0] - 100  # senkrechter Randtext links (z. B. "VM WG 9") ignorieren
        for w in words:
            if w[1] <= table_top or w[0] < min(margin, 20):
                continue
            key = next((k for k in rows if abs(k - w[1]) <= 2.5), w[1])
            rows.setdefault(key, []).append(w)
        for y in sorted(rows):
            cells = sorted(rows[y], key=lambda w: w[0])
            texts = [c[4] for c in cells]
            if texts[0] == "VM-Nr." and len(texts) > 1 and (m := _HUK_VM.match(texts[1])):
                vm, contract, contract_sparte, contract_date = m.group(1), None, None, None
                result.blocks.setdefault(vm, " ".join(texts[2:]).strip(" ,") or None)
                continue
            if texts[0] in _HUK_SKIP_FIRST:
                if texts[0] == "Ihre" and "Werbegebühren" in texts:
                    amounts = [c for c in cells if _HUK_AMOUNT.match(c[4])]
                    if amounts:
                        result.document_total = parse_amount(amounts[-1][4])
                continue  # Zwischensummen, Uebertraege und Gesamtbetraege
            ref_cell = next((c for c in cells if contract_left <= c[0] <= contract_right and _HUK_CONTRACT.match(c[4])), None)
            code_cell = next((c for c in cells if code_left <= c[0] <= code_right and re.fullmatch(r"[A-ZÄÖÜ]{2,6}", c[4])), None)
            code = code_cell[4] if code_cell else None
            if ref_cell is not None:  # Hauptzeile bestimmt den Vertrag - auch ohne eigenen Betrag
                contract, contract_sparte = ref_cell[4], huk_sparte(code)
                date_cell = next((c for c in cells if date_left <= c[0] <= date_right and _huk_date(c[4])), None)
                contract_date = _huk_date(date_cell[4]) if date_cell else None
            elif contract_sparte is None:
                contract_sparte = huk_sparte(code)  # z. B. Hauptzeile FAB, Sparte aus der Unterzeile (KH, FV ...)
            amount_cells = [c for c in cells if c[0] >= amount_left and _HUK_AMOUNT.match(c[4])]
            if not amount_cells:
                continue
            amount = parse_amount(amount_cells[-1][4])
            if vm is None or contract is None:
                orphan_amounts += 1
            raw = "   ".join(re.sub(r"\s+", " ", c[4]) for c in cells)
            position += 1
            result.lines.append(ParsedLine(
                page=index + 1, position=position, raw=raw[:500], amount=amount, booking_date=contract_date,
                contract_ref=contract, sparte=huk_sparte(code) or contract_sparte,
                booking_type=("storno" if amount is not None and amount < 0 else "abschluss"), booking_label=code, broker_number=vm,
            ))
    if orphan_amounts:
        result.incomplete.append(f"{orphan_amounts} Betrag/Beträge ohne Mitarbeiterblock oder Vertrag")
    # Sparte je Vertrag: erstes Kuerzel mit Sparte (FAB-Zeilen erben sie, auch wenn sie davor stehen)
    first_sparte: dict = {}
    for line in result.lines:
        if line.contract_ref and line.sparte and (line.broker_number, line.contract_ref) not in first_sparte:
            first_sparte[(line.broker_number, line.contract_ref)] = line.sparte
    for line in result.lines:
        if line.contract_ref and (line.broker_number, line.contract_ref) in first_sparte:
            line.sparte = first_sparte[(line.broker_number, line.contract_ref)]
    parsed_total = sum((line.amount for line in result.lines if line.amount is not None), ZERO)
    if result.document_total is not None and parsed_total != result.document_total:
        result.incomplete.append(
            f"Summe der Einzelzeilen {_money(parsed_total)} weicht vom Gesamtbetrag laut Beleg {_money(result.document_total)} ab"
        )
    if not result.lines:
        result.incomplete.append("keine Vergütungszeilen erkannt")
    return result


def fingerprint(parsed: ParsedSettlement) -> str:
    """Inhalt unabhaengig von Datei/Scan: Zeitraum und alle Buchungen (sortiert)."""
    parts = [str(parsed.period_start), str(parsed.period_end)]
    parts += sorted(f"{line.broker_number}|{line.contract_ref}|{line.amount}|{line.booking_label}" for line in parsed.lines)
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


# --- Einreichen / Verarbeiten -----------------------------------------------------------------------


def _storage_dir():
    from pathlib import Path

    from flask import current_app

    return Path(current_app.config.get("SETTLEMENT_STORAGE_DIR") or (Path(current_app.root_path).parent / "storage" / "settlements"))


def submit(user, filename: str, data: bytes):
    """Speichert die Datei und stoesst die Verarbeitung an. Gleiche Datei -> vorhandener Eintrag."""
    import uuid

    from app.extensions import db
    from app.models import Settlement

    if not (user.is_office_admin or user.is_employee):
        raise SettlementError("Keine Berechtigung.", 403)
    if not filename.lower().endswith(".pdf") or not data.startswith(b"%PDF"):
        raise SettlementError("Bitte eine PDF-Datei einreichen.")
    if len(data) > 20 * 1024 * 1024:
        raise SettlementError("Die Datei ist zu groß (höchstens 20 MB).")
    digest = hashlib.sha256(data).hexdigest()
    existing = Settlement.query.filter(Settlement.file_sha256 == digest).first()
    if existing is not None:
        return existing, False
    folder = _storage_dir() / str(user.tenant_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.pdf"
    (folder / name).write_bytes(data)
    record = Settlement(tenant_id=user.tenant_id, original_filename=filename[:255], stored_path=f"{user.tenant_id}/{name}",
                        file_sha256=digest, uploaded_by_user_id=user.id, status="processing")
    db.session.add(record)
    db.session.commit()
    from app.tasks.settlement_tasks import process_settlement

    process_settlement.delay(record.id, user.tenant_id)
    return record, True


def file_path(record):
    base = _storage_dir().resolve()
    path = (base / record.stored_path).resolve()
    if base not in path.parents or not path.is_file():
        return None
    return path


def read_file(record) -> ParsedSettlement:
    """Liest den gespeicherten Beleg. Fehler -> SettlementError (bisheriges Ergebnis bleibt)."""
    path = file_path(record)
    if path is None:
        raise SettlementError("Die Datei wurde nicht gefunden.")
    parsed = parse_huk(path.read_bytes())
    if parsed is None:
        raise SettlementError("Keine HUK-Einzelübersicht mit lesbarem Text gefunden – der Beleg kann nicht automatisch ausgewertet werden.")
    return parsed


def owner_map(tenant_id: int | None = None) -> dict:
    """VM-Nummer (Schluessel) -> aktives Buero-Mitglied. Nie SUPER_ADMIN, deaktivierte oder geloeschte Konten."""
    from app.models import User
    from app.models.enums import UserRole
    from app.utils.vermittlernummer import vermittlernummer_key

    query = User.query.filter(User.role != UserRole.SUPER_ADMIN, User.deleted_at.is_(None), User.is_active.is_(True))
    if tenant_id is not None:
        query = query.filter(User.tenant_id == tenant_id)
    return {vermittlernummer_key(u.vermittlernummer): u for u in query.all() if u.vermittlernummer}


def evaluate(record) -> None:
    """Dieselbe Auswertung fuer Upload und Neuauswertung. Liest zuerst vollstaendig; erst danach
    werden die bisherigen Zeilen desselben Belegs ersetzt (keine neuen Belege, keine Dubletten).
    Schlaegt das Lesen fehl, bleibt ein vorhandenes Ergebnis unveraendert (SettlementError)."""
    from app.extensions import db
    from app.models import Settlement, SettlementLine
    from app.models.settlement import _now
    from app.utils.vermittlernummer import vermittlernummer_key

    parsed = read_file(record)
    owners = owner_map(record.tenant_id)
    new_lines = [
        SettlementLine(
            tenant_id=record.tenant_id, position=line.position, page=line.page, raw_text=line.raw, booking_date=line.booking_date,
            contract_ref=line.contract_ref, sparte=line.sparte, booking_type=line.booking_type, booking_label=line.booking_label,
            amount=line.amount, is_ten=line.is_ten, broker_number=line.broker_number,
            employee_user_id=(owners.get(vermittlernummer_key(line.broker_number)).id if owners.get(vermittlernummer_key(line.broker_number)) else None),
        )
        for line in parsed.lines
    ]
    content = fingerprint(parsed)
    twin = Settlement.query.filter(Settlement.content_fingerprint == content, Settlement.id != record.id,
                                   Settlement.status.in_(COUNTED_STATUSES)).first()
    record.lines = new_lines  # alte Zeilen werden erst jetzt (im selben Commit) ersetzt
    record.period_start, record.period_end = parsed.period_start, parsed.period_end
    record.content_fingerprint = content
    record.broker_number = None
    record.employee_user_id = None
    record.text_method = "text"
    record.page_count = max((line.page for line in parsed.lines), default=record.page_count or 0) or record.page_count
    record.details = {
        "version": EVALUATION_VERSION,
        "blocks": [{"vm": vm, "name": name} for vm, name in parsed.blocks.items()],
        "document_total": str(parsed.document_total) if parsed.document_total is not None else None,
        "incomplete": parsed.incomplete,
    }
    if twin is not None:
        record.status, record.duplicate_of_id = "duplicate", twin.id
        record.message = f"Inhalt identisch mit „{twin.original_filename}“ – wird nicht gezählt"
    else:
        record.duplicate_of_id = None
        record.status = "incomplete" if parsed.incomplete else "done"
        record.message = ("Auswertung unvollständig: " + "; ".join(parsed.incomplete[:3])) if parsed.incomplete else None
    record.processed_at = _now()
    db.session.commit()


def process(record) -> None:
    """Upload: Beleg einlesen (im Celery-Worker). Nicht lesbar -> Status failed."""
    from app.extensions import db

    try:
        evaluate(record)
    except SettlementError as exc:
        db.session.rollback()
        record.status, record.message = "failed", str(exc)
        db.session.commit()


def reevaluate(record) -> None:
    """Vorhandene PDF neu auswerten. Bei Fehlern bleibt das alte Ergebnis vollstaendig erhalten."""
    from app.extensions import db

    try:
        evaluate(record)
    except Exception:
        db.session.rollback()
        raise


def is_current(record) -> bool:
    return bool(record.details and record.details.get("version") == EVALUATION_VERSION)


# --- Mitarbeiteruebersicht --------------------------------------------------------------------------


@dataclass
class Contract:
    ref: str
    sparte: str
    applied_on: date | None
    amount: Decimal  # Verguetung laut Beleg ohne 10-€-Positionen


@dataclass
class EmployeeOverview:
    user_id: int | None
    name: str
    vm: str | None
    closings: list = field(default_factory=list)  # gezaehlte Neuabschluesse (Contract)
    cancellations: list = field(default_factory=list)  # Vertraege mit Storno/Netto <= 0 (Contract)
    by_sparte: dict = field(default_factory=dict)
    regular: Decimal = ZERO  # Verguetung ohne 10-€-Positionen
    ten_count: int = 0
    ten_sum: Decimal = ZERO
    ten_lines: list = field(default_factory=list)
    has_data: bool = False
    unassigned: bool = False  # VM-Block ohne passendes Buero-Mitglied

    @property
    def count(self) -> int:
        return len(self.closings)


@dataclass
class Overview:
    year: int
    month: int
    rows: list
    documents: list
    incomplete: list  # kompakte Hinweise (Beleg: Grund)
    has_settlement: bool
    total_regular: Decimal = ZERO
    total_ten: Decimal = ZERO


def counted_settlements(year: int, month: int) -> list:
    from app.models import Settlement

    start, end = date(year, month, 1), _month_end(year, month)
    rows = Settlement.query.filter(Settlement.status.in_(COUNTED_STATUSES), Settlement.period_start >= start,
                                   Settlement.period_start <= end).order_by(Settlement.id).all()
    return [r for r in rows if is_current(r)]


def periods() -> list[tuple[int, int]]:
    from app.models import Settlement

    rows = Settlement.query.filter(Settlement.period_start.isnot(None), Settlement.status.in_(COUNTED_STATUSES)).all()
    return sorted({(r.period_start.year, r.period_start.month) for r in rows if is_current(r)}, reverse=True)


def overview(year: int, month: int) -> Overview:
    """Je aktivem Buero-Mitglied (auch ohne Abschluesse) und je nicht zuordenbarem VM-Block."""
    from app.models import SettlementLine, User
    from app.services.calendar import member_name
    from app.services.user_admin import office_members_query
    from app.utils.vermittlernummer import vermittlernummer_key

    documents = counted_settlements(year, month)
    members = office_members_query().filter(User.is_active.is_(True)).all()
    owners = {vermittlernummer_key(u.vermittlernummer): u for u in members if u.vermittlernummer}
    rows: dict = {}
    for user in members:
        rows[("u", user.id)] = EmployeeOverview(user.id, member_name(user), vermittlernummer_key(user.vermittlernummer))
    block_names = {}
    for record in documents:
        for block in record.details.get("blocks") or []:
            block_names.setdefault(block["vm"], block.get("name"))
    lines = (SettlementLine.query.filter(SettlementLine.settlement_id.in_([d.id for d in documents])).order_by(
        SettlementLine.settlement_id, SettlementLine.position).all() if documents else [])
    contracts: dict = defaultdict(list)
    for line in lines:
        if line.amount is None:
            continue
        user = owners.get(vermittlernummer_key(line.broker_number))
        if user is not None:
            key = ("u", user.id)
        else:
            key = ("vm", line.broker_number or "–")
            if key not in rows:
                name = block_names.get(line.broker_number) or "Name nicht erkannt"
                rows[key] = EmployeeOverview(None, name, line.broker_number, unassigned=True)
        row = rows[key]
        row.has_data = True
        if line.is_ten:
            row.ten_count += 1
            row.ten_sum += line.amount
            row.ten_lines.append(line)
            continue
        row.regular += line.amount
        contracts[(key, line.contract_ref or f"ohne-{line.id}")].append(line)
    for (key, ref), group in contracts.items():
        row = rows[key]
        net = sum((line.amount for line in group), ZERO)
        first = group[0]
        sparte = next((line.sparte for line in group if line.sparte), None) or "Sparte unklar"
        item = Contract(first.contract_ref or "ohne Vertragsnummer", sparte, first.booking_date, net)
        if net > 0 and any(line.amount > 0 for line in group):
            row.closings.append(item)
            row.by_sparte[sparte] = row.by_sparte.get(sparte, 0) + 1
        else:
            row.cancellations.append(item)
    for row in rows.values():
        row.closings.sort(key=lambda c: (c.applied_on or date.max, c.ref))
        row.by_sparte = dict(sorted(row.by_sparte.items()))
    incomplete = [f"{d.original_filename}: {'; '.join((d.details or {}).get('incomplete') or [])}" for d in documents if d.status == "incomplete"]
    ordered = sorted(rows.values(), key=lambda r: (r.unassigned, r.name.lower()))
    return Overview(year, month, ordered, documents, incomplete, bool(documents),
                    total_regular=sum((r.regular for r in ordered), ZERO), total_ten=sum((r.ten_sum for r in ordered), ZERO))


def month_label(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {year}"
