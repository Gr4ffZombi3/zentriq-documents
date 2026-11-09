"""Eingefuegte/hochgeladene Kunden-E-Mails zerlegen und dem Kunden zuordnen - deterministisch.

Getrennt werden:
- Kopf (Von/An/Betreff/Datum), Nachrichtentext, Signatur, zitierte aeltere Nachrichten
  (">", "Am ... schrieb ...:", "-----Ursprüngliche Nachricht-----", weiterer "Von:"-Block).
  Aus Zitaten wird NICHTS als Kundendatum uebernommen.
- Absender (Kunde) und Empfaenger (Buero): E-Mail-Adressen des Bueros (Buero-Profil und
  Benutzerkonten des Bueros) sind nie Kundendaten.
- Feldbezeichnungen ("Geburtsdatum: ...") sind Beschriftungen, keine Namensteile.
- Nummern: VN-Nummer "808/123456-E", Vertragsnummer "808/123456-E-12" (VN + Vertragsendung),
  Vermittlernummer "08/1234-A" (nie Kundendatum).
Der Text ist Datenmaterial - Anweisungen darin werden nie ausgefuehrt."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.utils.customer_keys import VN_PATTERN, normalize_phone

_HEADER = re.compile(r"^\s*(von|from|an|to|cc|betreff|subject|datum|date|gesendet|sent)\s*:\s*(.*)$", re.I)
_QUOTE_START = re.compile(
    r"^\s*(?:>|-{2,}\s*(?:original|ursprüngliche|urspruengliche|weitergeleitete)\s+(?:message|nachricht)"
    r"|am\s.+\sschrieb\s.+:\s*$|on\s.+\swrote:\s*$|(?:von|from)\s*:\s*.+)",
    re.I,
)
_CLOSING = re.compile(
    r"^\s*(?:mit\s+)?(?:freundlichen|besten|herzlichen|vielen|liebe|beste|viele|schöne|schoene)?\s*(?:grüßen|grüssen|gruessen|grüße|grüsse|gruesse|gruß|gruss|dank(?:e)?)\b[\s,!.]*$"
    r"|^\s*(?:mfg|vg|lg|bg)[\s,.]*$|^--\s*$",
    re.I,
)
_LABEL = re.compile(
    r"^\s*(anrede|name|vor-?\s*und\s*nachname|vorname|nachname|geburtsdatum|geb\.?(?:-?datum)?|telefon(?:nummer)?|tel\.?|mobil(?:nummer)?|handy(?:nummer)?"
    r"|e-?mail|anschrift|adresse|straße|strasse|plz|ort|wohnort|vertragsnummer|versicherungs(?:schein)?nummer|vn(?:-?nr\.?)?"
    r"|versicherungsnehmer(?:nummer)?|vermittler(?:nummer)?|kennzeichen)\s*[:\-]\s*(.+?)\s*$",
    re.I,
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ADDR = re.compile(r"^\s*\"?([^\"<>]*?)\"?\s*<\s*([\w.+-]+@[\w-]+(?:\.[\w-]+)+)\s*>\s*$")
_BROKER = re.compile(r"(?<![\w/])(\d{2})\s*/\s*(\d{4})\s*-\s*([A-Za-z])(?![\w/])")
_CONTRACT = re.compile(r"(?<![\w/])(\d{3})\s*/\s*(\d{6})\s*-\s*([A-Za-z])\s*-\s*(\d{2})(?![\w/])")
_NAME_WORD = re.compile(r"^[A-ZÄÖÜ][a-zäöüßA-ZÄÖÜ'’-]+$")
_PARTICLES = {"von", "van", "de", "der", "den", "zu", "vom", "zur", "di", "da", "le", "la", "ter", "ten"}
_NOT_NAME = {
    "geburtsdatum", "vertragsnummer", "telefon", "betreff", "anschrift", "adresse", "kunde", "kundin", "herr", "frau",
    "sehr", "geehrte", "geehrter", "damen", "herren", "hallo", "guten", "tag", "team", "service", "info", "kontakt",
    "versicherung", "huk", "coburg", "agentur", "büro", "buero", "gmbh", "mit", "freundlichen", "grüßen",
}


@dataclass
class EmailInfo:
    raw: str
    sender_name: str | None = None
    sender_email: str | None = None
    recipients: list[str] = field(default_factory=list)
    subject: str | None = None
    body: str = ""
    signature: str = ""
    quoted: str = ""
    salutation: str | None = None  # "Herr" / "Frau" nur bei ausdruecklicher Angabe
    first_name: str | None = None
    last_name: str | None = None
    name_conflict: bool = False
    phone: str | None = None  # normalisiert
    birthdate: date | None = None
    vn: str | None = None  # Anzeigeform 808/123456-E
    contracts: list[str] = field(default_factory=list)
    broker_numbers: list[str] = field(default_factory=list)
    street: str | None = None
    house_number: str | None = None
    postal_code: str | None = None
    city: str | None = None
    uncertain: list[str] = field(default_factory=list)

    @property
    def name(self) -> str | None:
        return f"{self.first_name} {self.last_name}" if self.first_name and self.last_name else None

    def evidence(self) -> list[str]:
        """Belegte Angaben (fuer die Statusmeldung)."""
        out = []
        if self.sender_email:
            out.append("E-Mail")
        if self.phone:
            out.append("Telefon")
        if self.vn:
            out.append("VN-Nummer")
        if self.street or self.postal_code:
            out.append("Anschrift")
        if self.birthdate:
            out.append("Geburtsdatum")
        return out


def looks_like_email(text: str) -> bool:
    """Grobe Erkennung einer eingefuegten E-Mail (Kopfzeilen oder Anrede + Gruss)."""
    t = text or ""
    if len(t) < 60:
        return False
    headers = len([line for line in t.splitlines()[:12] if _HEADER.match(line)])
    greeting = re.search(r"^\s*(sehr geehrte|hallo|guten tag|liebe[rs]?|moin|servus)\b", t, re.I | re.M)
    closing = any(_CLOSING.match(line) for line in t.splitlines())
    return headers >= 2 or bool(greeting and closing) or bool(headers and (greeting or closing))


def _split_name(value: str) -> tuple[str, str] | None:
    words = [w.strip(",.;") for w in value.split() if w.strip(",.;")]
    if "," in value and len(words) == 2:
        words = [words[1], words[0]]
    if not 2 <= len(words) <= 4:
        return None
    for w in words:
        if w.lower() in _NOT_NAME or not (_NAME_WORD.match(w) or w.lower() in _PARTICLES):
            return None
    particle = next((i for i, w in enumerate(words) if i > 0 and w.lower() in _PARTICLES), None)
    if particle is not None:
        return " ".join(words[:particle]), " ".join(words[particle:])
    if len(words) == 2:
        return words[0], words[1]
    return None  # "Hans Peter Schulz": Aufteilung unklar - nicht raten


def parse(text: str, office_emails: set[str] | None = None) -> EmailInfo:
    office_emails = {e.lower() for e in (office_emails or set())}
    info = EmailInfo(raw=text or "")
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # 1. Kopfzeilen am Anfang
    headers: dict[str, str] = {}
    index = 0
    while index < len(lines) and (not lines[index].strip() and not headers):
        index += 1
    while index < len(lines):
        m = _HEADER.match(lines[index])
        if not m:
            break
        key = m.group(1).lower()
        key = {"from": "von", "to": "an", "subject": "betreff", "date": "datum", "sent": "datum", "gesendet": "datum"}.get(key, key)
        headers.setdefault(key, m.group(2).strip())
        index += 1
    rest = lines[index:]

    # 2. Zitat abtrennen (erste Zitatzeile nach Beginn des eigentlichen Texts)
    body_lines, quoted = [], []
    started = False
    for i, line in enumerate(rest):
        if started and _QUOTE_START.match(line):
            quoted = rest[i:]
            break
        if line.strip():
            started = True
        body_lines.append(line)
    info.quoted = "\n".join(quoted).strip()

    # 3. Signatur abtrennen (nach der Grussformel)
    sig_index = next((i for i, line in enumerate(body_lines) if _CLOSING.match(line)), None)
    if sig_index is not None:
        info.signature = "\n".join(body_lines[sig_index + 1:]).strip()
        body_lines = body_lines[:sig_index]
    info.body = "\n".join(body_lines).strip()
    info.subject = headers.get("betreff") or None

    # 4. Absender / Empfaenger
    def address(value: str) -> tuple[str | None, str | None]:
        m = _ADDR.match(value or "")
        if m:
            return (m.group(1).strip() or None), m.group(2).lower()
        e = _EMAIL.search(value or "")
        return None, (e.group(0).lower() if e else None)

    sender_name, sender_email = address(headers.get("von", ""))
    recipients = [e.lower() for e in _EMAIL.findall(headers.get("an", "") + " " + headers.get("cc", ""))]
    if sender_email and sender_email in office_emails:
        # Unsere eigene Nachricht: Kunde ist der Empfaenger.
        other = [e for e in recipients if e not in office_emails]
        sender_name, sender_email = None, (other[0] if len(other) == 1 else None)
        recipients = [e for e in recipients if e in office_emails]
    info.sender_email = sender_email
    info.recipients = recipients

    # 5. Beschriftete Felder (nur eigener Text + Signatur, nie Zitat)
    own = info.body + "\n" + info.signature
    labeled: dict[str, str] = {}
    for line in own.split("\n"):
        m = _LABEL.match(line)
        if m:
            labeled.setdefault(re.sub(r"[\s.\-]", "", m.group(1).lower()), m.group(2).strip())

    anrede = labeled.get("anrede", "").strip().lower()
    if anrede in ("herr", "frau"):
        info.salutation = anrede.capitalize()

    # Name: Beschriftung > Absenderanzeige > erste Signaturzeile. Widersprueche -> kein Name.
    candidates = []
    if labeled.get("vorname") and labeled.get("nachname"):
        candidates.append((labeled["vorname"].strip(), labeled["nachname"].strip()))
    for key in ("name", "vorundnachname"):
        if labeled.get(key) and (split := _split_name(labeled[key])):
            candidates.append(split)
    if sender_name and (split := _split_name(sender_name)):
        candidates.append(split)
    sig_first = next((line.strip() for line in info.signature.split("\n") if line.strip()), "")
    if sig_first and (split := _split_name(sig_first)):
        candidates.append(split)
    unique = {(f.lower(), last.lower()) for f, last in candidates}
    if len(unique) == 1:
        info.first_name, info.last_name = candidates[0]
    elif len(unique) > 1:
        lasts = {last for _f, last in unique}
        if len(lasts) == 1:
            # Gleicher Nachname, Vorname unterschiedlich geschrieben -> laengste Form nur, wenn Praefix.
            firsts = sorted({f for f, _l in unique}, key=len)
            if all(firsts[-1].startswith(f.rstrip(".")) for f in firsts):
                best = next(c for c in candidates if c[0].lower() == firsts[-1])
                info.first_name, info.last_name = best
            else:
                info.name_conflict = True
        else:
            info.name_conflict = True
        if info.name_conflict:
            info.uncertain.append("Name")

    # Telefon
    phones = []
    for key in ("telefon", "telefonnummer", "tel", "mobil", "mobilnummer", "handy", "handynummer"):
        if labeled.get(key) and (p := normalize_phone(labeled[key])):
            phones.append(p)
    if not phones:
        for m in re.finditer(r"(?:tel(?:efon)?|mobil|handy|fon)\.?\s*:?\s*((?:\+|0)[\d\s/()-]{6,})", info.signature, re.I):
            if p := normalize_phone(m.group(1)):
                phones.append(p)
    if phones:
        info.phone = phones[0]
        if len(set(phones)) > 1:
            info.uncertain.append("Telefon")

    # Geburtsdatum
    for key in ("geburtsdatum", "geb", "gebdatum"):
        if labeled.get(key):
            m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", labeled[key])
            if m:
                try:
                    info.birthdate = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                except ValueError:
                    info.uncertain.append("Geburtsdatum")
            else:
                info.uncertain.append("Geburtsdatum")
            break

    # Nummern (eigener Text): Vermittler- und Vertragsnummern zuerst ausblenden.
    work = own
    for m in _BROKER.finditer(work):
        info.broker_numbers.append(f"{m.group(1)}/{m.group(2)}-{m.group(3).upper()}")
    work = _BROKER.sub(" ", work)
    for m in _CONTRACT.finditer(work):
        info.contracts.append(f"{m.group(1)}/{m.group(2)}-{m.group(3).upper()}-{m.group(4)}")
    vns = {f"{c[:12]}" for c in info.contracts}
    work = _CONTRACT.sub(" ", work)
    for m in VN_PATTERN.finditer(work):
        vns.add(f"{m.group(1)}/{m.group(2)}-{m.group(3).upper()}")
    if len(vns) == 1:
        info.vn = vns.pop()
    elif len(vns) > 1:
        info.uncertain.append("VN-Nummer")

    # Anschrift (Beschriftung oder Signatur)
    from app.services.customer_create import _PLZ_CITY, _STREETS, _cap_place

    address_text = " ".join(filter(None, [labeled.get("anschrift"), labeled.get("adresse"), labeled.get("straße"), labeled.get("strasse"),
                                          labeled.get("plz"), labeled.get("ort"), labeled.get("wohnort")])) or info.signature
    for rx in _STREETS[:3]:
        m = rx.search(address_text)
        if m:
            info.street, info.house_number = _cap_place(m.group(1).strip()), re.sub(r"\s+", "", m.group(2))
            break
    m = re.search(r"(?<!\d)(\d{5})(?!\d)\s+([A-ZÄÖÜ][\wäöüß.\-]+(?:\s+(?:am|an der|im|bei)\s+[A-ZÄÖÜ][\wäöüß.\-]+)?)", address_text)
    if m:
        info.postal_code, info.city = m.group(1), m.group(2)
    elif (m := _PLZ_CITY.search(labeled.get("plz", ""))):
        info.postal_code = m.group(1)
    return info


def office_emails(tenant_id: int) -> set[str]:
    from app.models import OfficeSettings, User

    emails = {u.email.lower() for u in User.query.filter(User.tenant_id == tenant_id).all() if u.email}
    s = OfficeSettings.query.first()
    if s and s.email:
        emails.add(s.email.lower())
    return emails


# --- Kunde zuordnen bzw. anlegen ----------------------------------------------------------------


@dataclass
class Resolution:
    status: str  # "assigned" | "created" | "ambiguous" | "unknown"
    customer: object | None = None
    candidates: list = field(default_factory=list)
    message: str = ""
    differing: list[str] = field(default_factory=list)


def _differences(customer, info: EmailInfo) -> list[str]:
    out = []
    if info.phone and customer.phone_keys and info.phone not in customer.phone_keys:
        out.append("Telefon")
    if info.sender_email and customer.email and customer.email.lower() != info.sender_email:
        out.append("E-Mail")
    if info.postal_code and customer.postal_code and customer.postal_code.strip() != info.postal_code:
        out.append("PLZ")
    if info.birthdate and customer.date_of_birth and customer.date_of_birth != info.birthdate:
        out.append("Geburtsdatum")
    return out


def resolve(user, info: EmailInfo, *, create: bool = True) -> Resolution:
    """Kunde im eigenen Buero (Tenant-Filter) finden; bei eindeutiger Identitaet anlegen."""
    from sqlalchemy import func

    from app.models import Customer
    from app.services.customer_notes import _load_customers, customers_by_vn
    from app.utils.customer_keys import name_key, normalize_customer_number

    def done(customer, status="assigned"):
        verb = "Kunde angelegt" if status == "created" else "Kunde zugeordnet"
        diff = _differences(customer, info) if status == "assigned" else []
        msg = f"{verb}: {customer.name}"
        if diff:
            msg += f" (abweichend in der Nachricht: {', '.join(diff)} – nicht übernommen)"
        return Resolution(status, customer, message=msg, differing=diff)

    if info.vn and "VN-Nummer" not in info.uncertain:
        owners = _load_customers(customers_by_vn(normalize_customer_number(info.vn)))
        if len(owners) == 1:
            return done(owners[0])
        if owners:
            return Resolution("ambiguous", candidates=owners, message=f"Mehrere Kunden zur VN-Nummer {info.vn}.")
    if info.sender_email:
        by_mail = Customer.query.filter(func.lower(Customer.email) == info.sender_email).limit(4).all()
        if len(by_mail) == 1:
            return done(by_mail[0])
        if by_mail:
            return Resolution("ambiguous", candidates=by_mail, message="Mehrere Kunden mit dieser E-Mail-Adresse.")
    named = Customer.query.filter(Customer.name_key == name_key(info.name)).limit(6).all() if info.name else []
    if info.phone:
        # Telefonnummer nur zusammen mit passendem Namen (Familien teilen Nummern).
        by_phone = [c for c in Customer.query.filter(Customer.phone_key_in([info.phone])).limit(6).all() if not info.name or c in named]
        if len(by_phone) == 1 and info.name:
            return done(by_phone[0])
    if named:
        fitting = [c for c in named if not _differences(c, info)]
        if len(fitting) == 1:
            return done(fitting[0])
        return Resolution("ambiguous", candidates=named, message=f"Mehrere bzw. abweichende Kunden „{info.name}“ – bitte VN-Nummer oder Kunde nennen.")
    if info.name_conflict:
        return Resolution("unknown", message="Der Name des Absenders ist nicht eindeutig – kein Kunde angelegt.")
    if not create or not info.name:
        return Resolution("unknown", message="Kein passender Kunde gefunden.")
    if not (info.sender_email or info.phone or info.vn):
        return Resolution("unknown", message=f"„{info.name}“ ist nicht angelegt; ohne E-Mail, Telefon oder VN-Nummer wird nicht automatisch angelegt.")
    return _create(user, info)


def _create(user, info: EmailInfo) -> Resolution:
    from flask import current_app

    from app.extensions import db
    from app.models import Customer
    from app.models.audit_log import AuditEventType
    from app.services import customer_sources
    from app.services.audit import log_audit_event
    from app.services.customer_lock import CustomerLockTimeout, customer_creation_lock, fresh_transaction
    from app.utils.customer_keys import format_phone

    lock = customer_creation_lock(user.tenant_id, timeout_seconds=20)
    try:
        lock.acquire()
    except CustomerLockTimeout:
        return Resolution("unknown", message="Kundendaten werden gerade gespeichert – Kunde nicht angelegt, bitte erneut versuchen.")
    try:
        fresh_transaction()
        again = resolve(user, info, create=False)  # parallele Verarbeitung derselben Nachricht
        if again.status != "unknown":
            return again
        values = {
            "name": info.name, "first_name": info.first_name, "last_name": info.last_name,
            "email": info.sender_email, "phone": format_phone(info.phone) if info.phone else None,
            "customer_number": info.vn, "date_of_birth": info.birthdate,
            "street": info.street, "house_number": info.house_number,
            "address": " ".join(filter(None, [info.street, info.house_number])) or None,
            "postal_code": info.postal_code, "city": info.city,
        }
        values = {k: v for k, v in values.items() if v}
        customer = Customer(
            tenant_id=user.tenant_id, assigned_user_id=user.id, source=customer_sources.E_MAIL,
            field_sources={k: customer_sources.E_MAIL for k in values}, **values,
        )
        db.session.add(customer)
        db.session.commit()
    finally:
        lock.release()
    try:
        log_audit_event(AuditEventType.CUSTOMER_CREATED, user=user, details={"customer_id": customer.id, "source": customer_sources.E_MAIL,
                                                                             "fields": sorted(values)})
    except Exception:  # noqa: BLE001
        current_app.logger.exception("email_context.audit_failed customer=%s", customer.id)
        db.session.rollback()
    res = Resolution("created", customer)
    res.message = f"Kunde angelegt: {customer.name} (aus der E-Mail übernommen: {', '.join(info.evidence()) or 'Name'})"
    return res
