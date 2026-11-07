"""Abrechnungen: Einlesen (lokal), Zerlegen in Buchungen, Auswerten - ohne KI.

Lesen: eingebetteter PDF-Text je Seite, gescannte Seiten lokal per Tesseract (deu). Rohdaten
verlassen den Server nicht.

Zerlegen (je Textzeile mit Betrag):
- Betrag: letzter deutscher Betrag der Zeile ("1.234,56", "-10,00", "10,00-", "10,00 €").
- Referenz: Vertragsnummer (808/123456-E-12) oder "Auftrag/Antrag/Vertrag Nr. ...".
- Sparte: feste Stichwortliste (Kfz, Wohngebäude, Hausrat, Haftpflicht, ...).
- Buchungsart: abschluss (Abschluss-/Neugeschäftsprovision, Antrag), bestand, storno, korrektur,
  sonstig. Nur "abschluss" belegt einen geschriebenen Auftrag bzw. verkauften Vertrag.
- 10-€-Position: Betrag genau +10,00 oder -10,00.
Fehlt Referenz, Sparte oder Buchungsart, ist die Zeile "unklar" (Pruefung durch den Buero-Admin,
mit Seite im Original) und zaehlt erst nach Bestaetigung.

Auswerten (summary): je Mitarbeiter und Zeitraum Betraege und Anzahlen aus den gezaehlten
Buchungen; jede Zahl ist ueber lines_for() bis zu den Buchungen nachvollziehbar."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

TEN = Decimal("10.00")
MONTHS = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7, "august": 8,
          "september": 9, "oktober": 10, "november": 11, "dezember": 12}
MONTH_NAMES = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember")

_AMOUNT = re.compile(r"(?<![\d,.])(-\s?)?(\d{1,3}(?:\.\d{3})*|\d+),(\d{2})(\s?-)?(?:\s?(?:€|EUR))?(?![\d,])")
_CONTRACT = re.compile(r"(?<![\w/])(\d{3})\s*/\s*(\d{6})\s*-\s*([A-Za-z])(?:\s*-\s*(\d{2}))?(?![\w/])")
_ORDER = re.compile(r"\b(?:auftrag|antrag|vertrag|vers\.?-?nr|vsnr)\.?\s*(?:nr\.?|nummer)?\s*:?\s*([A-Z0-9][A-Z0-9/\-]{4,})", re.I)
_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_PERIOD_RANGE = re.compile(r"(?:abrechnungs)?zeitraum\s*:?\s*(\d{1,2}\.\d{1,2}\.\d{4})\s*(?:-|–|bis)\s*(\d{1,2}\.\d{1,2}\.\d{4})", re.I)
_PERIOD_MONTH = re.compile(r"(?:abrechnungs(?:monat|zeitraum)|monat|zeitraum)\s*:?\s*(" + "|".join(MONTHS) + r")\s+(\d{4})", re.I)
_BROKER = re.compile(r"(?:vermittler(?:nummer|-?nr\.?)?|vm-?nr\.?|agentur(?:nummer)?)\s*:?\s*(\d{2}\s*/\s*\d{4}\s*-\s*[A-Za-z])", re.I)
_SKIP = re.compile(r"\b(?:summe|zwischensumme|gesamt(?:summe|betrag)?|übertrag|uebertrag|saldo|auszahlung|überweisung|ueberweisung|seite\s+\d)\b", re.I)

SPARTEN = (
    ("Kfz", re.compile(r"\b(?:kfz|kraftfahrt|kh|vk|tk|vollkasko|teilkasko|auto|pkw|kraftrad)\b", re.I)),
    ("Wohngebäude", re.compile(r"\b(?:wohngeb(?:ä|ae)ude|vgv|wg|geb(?:ä|ae)ude)\b", re.I)),
    ("Hausrat", re.compile(r"\b(?:hausrat|hr)\b", re.I)),
    ("Haftpflicht", re.compile(r"\b(?:haftpflicht|phv|ph|privathaftpflicht)\b", re.I)),
    ("Rechtsschutz", re.compile(r"\b(?:rechtsschutz|rs)\b", re.I)),
    ("Unfall", re.compile(r"\b(?:unfall)\b", re.I)),
    ("Leben/Rente", re.compile(r"\b(?:leben|rente|lv|altersvorsorge)\b", re.I)),
    ("Kranken", re.compile(r"\b(?:kranken|pkv|zahn)\b", re.I)),
)
TYPES = (
    ("storno", re.compile(r"\b(?:storno|stornierung|r(?:ü|ue)ckbelastung|r(?:ü|ue)cknahme)\b", re.I)),
    ("korrektur", re.compile(r"\b(?:korrektur|berichtigung|nachberechnung|nachtrag|ausgleich)\b", re.I)),
    ("abschluss", re.compile(r"\b(?:abschluss\w*|neugesch(?:ä|ae)ft\w*|neuvertrag|antrag|vermittlungsprovision|wechselpr(?:ä|ae)mie)\b", re.I)),
    ("bestand", re.compile(r"\b(?:bestand\w*|folgeprovision|laufend\w*|betreuung\w*)\b", re.I)),
)
TYPE_LABELS = {"abschluss": "Abschluss", "bestand": "Bestand", "storno": "Storno", "korrektur": "Korrektur", "sonstig": "Sonstige"}


class SettlementError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- Lesen ---------------------------------------------------------------------------------------


def read_pages(data: bytes) -> tuple[list[str], str]:
    """Text je Seite, ausschliesslich lokal. Gibt (Seiten, Methode text|ocr|mixed) zurueck."""
    import fitz
    from PIL import Image

    from app.services.ocr import tesseract_ocr

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise SettlementError("Die PDF-Datei kann nicht gelesen werden.") from exc
    pages, methods = [], set()
    try:
        if document.page_count > 60:
            raise SettlementError("Die PDF hat mehr als 60 Seiten.")
        for page in document:
            text = page.get_text("text")
            if len(text.strip()) < 20:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2.5, 2.5))
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                text = tesseract_ocr.ocr_image_lines(image)
                methods.add("ocr")
            else:
                methods.add("text")
            pages.append(text)
    finally:
        document.close()
    return pages, ("mixed" if len(methods) > 1 else (methods.pop() if methods else "text"))


# --- Zerlegen -------------------------------------------------------------------------------------


@dataclass
class ParsedLine:
    page: int
    position: int
    raw: str
    amount: Decimal | None
    booking_date: date | None
    contract_ref: str | None
    sparte: str | None
    booking_type: str | None
    booking_label: str | None
    reasons: list[str] = field(default_factory=list)

    @property
    def is_ten(self) -> bool:
        return self.amount is not None and abs(self.amount) == TEN


@dataclass
class ParsedSettlement:
    period_start: date | None = None
    period_end: date | None = None
    broker_number: str | None = None
    lines: list[ParsedLine] = field(default_factory=list)


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


def parse(pages: list[str]) -> ParsedSettlement:
    result = ParsedSettlement()
    head = "\n".join(pages[:1])
    m = _PERIOD_RANGE.search(head)
    if m:
        result.period_start, result.period_end = _date(m.group(1)), _date(m.group(2))
    else:
        m = _PERIOD_MONTH.search(head)
        if m:
            month, year = MONTHS[m.group(1).lower()], int(m.group(2))
            result.period_start, result.period_end = date(year, month, 1), _month_end(year, month)
    m = _BROKER.search(head)
    if m:
        result.broker_number = re.sub(r"\s+", "", m.group(1)).upper()
    position = 0
    for page_no, text in enumerate(pages, start=1):
        for raw in text.splitlines():
            line = " ".join(raw.split())
            if not line or _SKIP.search(line) or _PERIOD_RANGE.search(line) or _PERIOD_MONTH.search(line) or _BROKER.search(line):
                continue
            amount = parse_amount(line)
            contract = _CONTRACT.search(line)
            ref = None
            if contract:
                ref = f"{contract.group(1)}/{contract.group(2)}-{contract.group(3).upper()}" + (f"-{contract.group(4)}" if contract.group(4) else "")
            elif (order := _ORDER.search(line)):
                ref = order.group(1).upper()
            if amount is None:
                if ref:  # Referenz ohne lesbaren Betrag -> pruefen
                    position += 1
                    result.lines.append(ParsedLine(page_no, position, line[:500], None, _date(line), ref, None, None, None, ["Betrag nicht lesbar"]))
                continue
            rest = _CONTRACT.sub(" ", line)
            sparte = next((name for name, rx in SPARTEN if rx.search(rest)), None)
            booking_type, label = None, None
            for name, rx in TYPES:
                hit = rx.search(rest)
                if hit:
                    booking_type, label = name, hit.group(0)
                    break
            reasons = []
            if ref is None:
                reasons.append("keine Vertrags-/Auftragsreferenz")
            if sparte is None:
                reasons.append("Sparte nicht erkannt")
            if booking_type is None:
                reasons.append("Buchungsart nicht erkannt")
            if len(list(_AMOUNT.finditer(line))) > 2:
                reasons.append("mehrere Beträge in der Zeile")
            position += 1
            result.lines.append(ParsedLine(page_no, position, line[:500], amount, _date(line), ref, sparte, booking_type, label, reasons))
    return result


def fingerprint(parsed: ParsedSettlement) -> str:
    """Inhalt unabhaengig von Datei/Scan: Zeitraum, Vermittler und alle Buchungen (sortiert)."""
    parts = [str(parsed.period_start), str(parsed.period_end), parsed.broker_number or ""]
    parts += sorted(f"{line.contract_ref}|{line.amount}|{line.booking_type}|{line.sparte}" for line in parsed.lines)
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


def process(record) -> None:
    """Liest und zerlegt einen Beleg (im Celery-Worker)."""
    from app.extensions import db
    from app.models import Settlement, SettlementLine, User
    from app.models.settlement import _now
    from app.utils.vermittlernummer import vermittlernummer_key

    path = file_path(record)
    try:
        if path is None:
            raise SettlementError("Die Datei wurde nicht gefunden.")
        pages, method = read_pages(path.read_bytes())
        parsed = parse(pages)
    except SettlementError as exc:
        record.status, record.message = "failed", str(exc)
        db.session.commit()
        return
    record.text_method, record.page_count = method, len(pages)
    record.period_start, record.period_end, record.broker_number = parsed.period_start, parsed.period_end, parsed.broker_number
    record.content_fingerprint = fingerprint(parsed)
    if parsed.broker_number:
        key = vermittlernummer_key(parsed.broker_number)
        owner = next((u for u in User.query.filter(User.tenant_id == record.tenant_id).all() if vermittlernummer_key(u.vermittlernummer) == key), None)
        record.employee_user_id = owner.id if owner else None
    twin = Settlement.query.filter(Settlement.content_fingerprint == record.content_fingerprint, Settlement.id != record.id,
                                   Settlement.status.in_(("done", "needs_review"))).first()
    record.lines = [
        SettlementLine(tenant_id=record.tenant_id, position=line.position, page=line.page, raw_text=line.raw, booking_date=line.booking_date,
                       contract_ref=line.contract_ref, sparte=line.sparte, booking_type=line.booking_type, booking_label=line.booking_label,
                       amount=line.amount, is_ten=line.is_ten, needs_review=bool(line.reasons), review_reason="; ".join(line.reasons) or None)
        for line in parsed.lines
    ]
    problems = []
    if twin is not None:
        record.status, record.duplicate_of_id = "duplicate", twin.id
        problems.append(f"Inhalt identisch mit „{twin.original_filename}“ – wird nicht gezählt")
    else:
        if record.period_start is None:
            problems.append("Abrechnungszeitraum nicht erkannt")
        if record.employee_user_id is None:
            problems.append("Mitarbeiter/Vermittlernummer nicht zugeordnet")
        if not parsed.lines:
            problems.append("keine Buchungen erkannt")
        unclear = sum(1 for line in parsed.lines if line.reasons)
        if unclear:
            problems.append(f"{unclear} unklare Zeile(n)")
        record.status = "needs_review" if problems else "done"
    record.message = "; ".join(problems) or None
    record.processed_at = _now()
    db.session.commit()


# --- Auswertung -----------------------------------------------------------------------------------


@dataclass
class EmployeeSummary:
    user_id: int | None
    name: str
    regular: Decimal = Decimal("0")
    ten_sum: Decimal = Decimal("0")
    ten_pos_count: int = 0
    ten_pos_sum: Decimal = Decimal("0")
    ten_neg_count: int = 0
    ten_neg_sum: Decimal = Decimal("0")
    storno_sum: Decimal = Decimal("0")
    korrektur_sum: Decimal = Decimal("0")
    lines: int = 0
    orders: int | None = None
    orders_cancelled: int = 0
    sales_by_sparte: dict = field(default_factory=dict)
    ten_lines: list = field(default_factory=list)
    unclear: int = 0

    @property
    def total(self) -> Decimal:
        return self.regular + self.ten_sum


def periods() -> list[tuple[int, int]]:
    from app.models import Settlement

    rows = Settlement.query.filter(Settlement.period_start.isnot(None), Settlement.status.in_(("done", "needs_review"))).all()
    return sorted({(r.period_start.year, r.period_start.month) for r in rows}, reverse=True)


def counted_lines(year: int, month: int, *, user_id=None, exclude_ten=False, exclude_sparten=(), only=None):
    """Gezaehlte Buchungen eines Monats (Grundlage aller Summen). only: 'ten'|'storno'|'korrektur'|'regular'|'unclear'."""
    from app.models import Settlement, SettlementLine

    start, end = date(year, month, 1), _month_end(year, month)
    query = SettlementLine.query.join(Settlement).filter(
        Settlement.status.in_(("done", "needs_review")), Settlement.period_start >= start, Settlement.period_start <= end
    )
    if user_id is not None:
        query = query.filter(Settlement.employee_user_id == (None if user_id == 0 else user_id))
    lines = query.order_by(Settlement.id, SettlementLine.position).all()
    if only == "unclear":
        return [line for line in lines if line.needs_review and line.review_state is None]
    lines = [line for line in lines if line.counts]
    if exclude_ten:
        lines = [line for line in lines if not line.is_ten]
    if exclude_sparten:
        lines = [line for line in lines if line.sparte not in exclude_sparten]
    if only == "ten":
        lines = [line for line in lines if line.is_ten]
    elif only in ("storno", "korrektur"):
        lines = [line for line in lines if line.booking_type == only]
    elif only == "regular":
        lines = [line for line in lines if not line.is_ten]
    return lines


def summary(year: int, month: int, *, exclude_ten=False, exclude_sparten=()) -> list[EmployeeSummary]:
    from app.models import Settlement, User
    from app.services.calendar import member_name

    lines = counted_lines(year, month, exclude_ten=exclude_ten, exclude_sparten=exclude_sparten)
    unclear = counted_lines(year, month, only="unclear")
    by_user: dict = {}

    def entry(settlement: Settlement) -> EmployeeSummary:
        uid = settlement.employee_user_id or 0
        if uid not in by_user:
            user = User.query.filter(User.id == uid).first() if uid else None
            by_user[uid] = EmployeeSummary(uid, member_name(user) if user else "Nicht zugeordnet")
        return by_user[uid]

    contracts: dict = defaultdict(lambda: defaultdict(list))
    for line in lines:
        e = entry(line.settlement)
        e.lines += 1
        if line.is_ten:
            e.ten_sum += line.amount
            e.ten_lines.append(line)
            if line.amount > 0:
                e.ten_pos_count += 1
                e.ten_pos_sum += line.amount
            else:
                e.ten_neg_count += 1
                e.ten_neg_sum += line.amount
        else:
            e.regular += line.amount
        if line.booking_type == "storno":
            e.storno_sum += line.amount
        elif line.booking_type == "korrektur":
            e.korrektur_sum += line.amount
        if line.contract_ref:
            contracts[e.user_id][line.contract_ref].append(line)
    for line in unclear:
        entry(line.settlement).unclear += 1
    for uid, e in by_user.items():
        refs = contracts.get(uid, {})
        # Auftrag/Abschluss nur bei ausdruecklicher Abschlussbuchung; mehrere Zeilen je Vertrag = 1.
        written = {ref: rows for ref, rows in refs.items() if any(r.booking_type == "abschluss" for r in rows)}
        if not written:
            e.orders = None
            continue
        e.orders = len(written)
        sales: dict = defaultdict(int)
        for ref, rows in written.items():
            net = sum((r.amount for r in rows if r.booking_type in ("abschluss", "storno")), Decimal("0"))
            if any(r.booking_type == "storno" for r in rows) and net <= 0:
                e.orders_cancelled += 1
                continue
            sparte = next((r.sparte for r in rows if r.booking_type == "abschluss" and r.sparte), None) or "Sparte unklar"
            sales[sparte] += 1
        e.sales_by_sparte = dict(sorted(sales.items()))
    return sorted(by_user.values(), key=lambda x: (x.user_id == 0, x.name.lower()))


def sparten() -> list[str]:
    return [name for name, _rx in SPARTEN]


def month_label(year: int, month: int) -> str:
    return f"{MONTH_NAMES[month - 1]} {year}"
