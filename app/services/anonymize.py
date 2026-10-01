"""Dokument anonymisieren: personenbezogene Angaben lokal und deterministisch ersetzen.

Kein Schritt dieser Erkennung verlaesst den Server - keine externe KI, kein OCR-Dienst. Der
Benutzer prueft das Ergebnis und entscheidet danach selbst, ob er den anonymisierten Text
kopiert oder im Assistenten verwendet.

Erkannt werden: Namen, Anschriften, Telefonnummern, E-Mail-Adressen, Kunden-, Vertrags- und
Schadennummern, Kennzeichen, IBAN, Geburtsdaten und weitere Kennungen (Steuer-ID,
Sozialversicherungs-, Ausweis- und Fahrgestellnummer). Gleiche Angaben erhalten denselben
Platzhalter, verschiedene werden durchnummeriert ([KUNDE], [KUNDE 2]).

Namen erkennt Zentriq ueber Anrede ("Herr Mueller"), Beschriftung ("Name: ..."), eine Liste
haeufiger Vornamen ("Max Mustermann") sowie den eigenen Kundenstamm und die Mitarbeiter des
eigenen Bueros. Eine automatische Erkennung findet nie jeden Namen - deshalb kontrolliert der
Benutzer die Vorschau vor jeder Weitergabe."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.anonymize_names import FIRST_NAMES
from app.utils.customer_keys import normalize_phone

MAX_INPUT_CHARS = 60_000
# Kundenstamm-Abgleich: so viele Textwoerter als Vorfilter, so viele Kandidaten.
MAX_NAME_PROBES = 200
MAX_NAME_ROWS = 2000

# Platzhalter je Kategorie (Anzeige im Ergebnis) - Reihenfolge = Rangfolge bei Ueberschneidung.
CATEGORIES = {
    "email": ("E-MAIL", "E-Mail-Adresse"),
    "iban": ("IBAN", "IBAN"),
    "birthdate": ("GEBURTSDATUM", "Geburtsdatum"),
    "customer_number": ("KUNDENNUMMER", "Kundennummer"),
    "contract_number": ("VERTRAGSNUMMER", "Vertragsnummer"),
    "claim_number": ("SCHADENNUMMER", "Schadennummer"),
    "identifier": ("KENNUNG", "Sonstige Kennung"),
    "plate": ("KENNZEICHEN", "Kennzeichen"),
    "phone": ("TELEFON", "Telefonnummer"),
    "address": ("ANSCHRIFT", "Anschrift"),
    "employee": ("MITARBEITER", "Name (Mitarbeiter)"),
    "name": ("KUNDE", "Name"),
}
_RANK = {key: index for index, key in enumerate(CATEGORIES)}
# Anzeige in der Vorschau: zuerst, wer und wo - dann Kontakt und Kennungen.
DISPLAY_ORDER = ("name", "employee", "address", "phone", "email", "birthdate", "customer_number", "contract_number", "claim_number", "plate", "iban", "identifier")

_UPPER = "A-ZÄÖÜ"
_LOWER = "a-zäöüß"
# Ein Namensbestandteil: "Müller", "Schmidt-Leutheusser", "O'Neill", auch in Grossbuchstaben.
_NAME_WORD = rf"[{_UPPER}][{_LOWER}{_UPPER}'’-]+"
_PARTICLE = r"(?:von|van|de|der|den|zu|di|da|du|el|al|ter|vom|zur)"
_FULL_NAME = rf"{_NAME_WORD}(?:\s+(?:{_PARTICLE}\s+)*{_NAME_WORD}){{0,3}}"

# Woerter, die auf einen Vornamen folgen koennen, ohne Nachname zu sein.
_NOT_SURNAME = {
    "und", "oder", "sie", "ich", "er", "es", "wir", "ihr", "der", "die", "das", "dem", "den", "des",
    "mit", "von", "vom", "bei", "am", "im", "zum", "zur", "aus", "auf", "an", "in", "ist", "hat",
    "war", "wird", "kann", "soll", "hier", "dort", "heute", "morgen", "gestern", "bitte", "danke",
    "kundennummer", "kunde", "kundin", "telefon", "tel", "mobil", "e-mail", "email", "vertrag",
    "geb", "geboren", "herr", "frau", "gmbh", "ag", "kg",
}

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")
_DATE = r"\d{1,2}\.\s?\d{1,2}\.\s?(?:\d{4}|\d{2})\b"
_BIRTHDATE = re.compile(
    rf"(?:geb(?:oren)?\.?(?:\s*am)?|geburtsdatum|geb\.?-?\s?dat(?:um|\.)?|\*)\s*:?\s*({_DATE})",
    re.IGNORECASE,
)


def _labeled(labels: str, value: str = r"[A-Z0-9][A-Z0-9\-/.]*\d(?:[A-Z0-9\-/.]*[A-Z0-9])?|\d[\d \-/]{1,24}\d") -> re.Pattern:
    """Beschriftete Kennung: "<Beschriftung> [ist|lautet|:] <Wert>" - ersetzt wird nur der Wert."""
    return re.compile(rf"\b(?:{labels})\s*(?:ist|lautet|:|#)?\s*({value})(?![\w])", re.IGNORECASE)


_NR = r"[\s.-]*(?:nummer|nr\.?|-?nr\.?)"
_CUSTOMER_NUMBER = _labeled(rf"(?:kunden|kd\.?|vn|versicherungsnehmer){_NR}")
_CONTRACT_NUMBER = _labeled(rf"(?:vertrags|versicherungsschein|vs|police|policen){_NR}|vertrag|police")
_CLAIM_NUMBER = _labeled(rf"(?:schaden|schadens|sch\.?){_NR}")
_IDENTIFIERS = [
    _labeled(r"steuer-?\s?id(?:entifikationsnummer)?|steuer-?identifikationsnummer|idnr\.?", r"\d{2}\s?\d{3}\s?\d{3}\s?\d{3}"),
    _labeled(rf"(?:sozialversicherungs|rentenversicherungs|sv|rv){_NR}", r"\d{2}\s?\d{6}\s?[A-Z]\s?\d{3}"),
    _labeled(rf"(?:personalausweis|ausweis|reisepass|pass|führerschein){_NR}", r"[A-Z0-9]{6,12}"),
    _labeled(r"fin|vin|fahrgestellnummer|fahrzeug-?ident(?:ifikations)?nummer", r"[A-HJ-NPR-Z0-9]{17}"),
]
# Ohne Beschriftung eindeutig: Vorgangsnummer der Leipziger Liste, SV-Nummer, Fahrgestellnummer.
_CONTRACT_PATTERN = re.compile(r"\b\d{3}/\d{6,8}-[A-Z](?:-\d{2})?\b")
_SV_PATTERN = re.compile(r"\b\d{2}\s?\d{6}\s?[A-Z]\s?\d{3}\b")
_VIN_PATTERN = re.compile(r"\b(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-HJ-NPR-Z])[A-HJ-NPR-Z0-9]{17}\b")

_PLATE = re.compile(rf"(?<![\w-])[{_UPPER}]{{1,3}}[- ][A-Z]{{1,2}} ?\d{{1,4}}[EH]?(?![\w-])")
_PLATE_LABELED = _labeled(r"(?:amtliches\s+)?kennzeichen|kfz-?kennzeichen", r"[A-ZÄÖÜ]{1,3}[- ]?[A-Z]{1,2}[- ]?\d{1,4}[EH]?")

# Am Ende darf ein Satzpunkt folgen, aber keine weitere Ziffernfolge ("12.03.").
_PHONE = re.compile(r"(?<![\w+/.-])(?:\+|00)?\d[\d \t\-/()]{5,22}\d(?![\w/-])(?!\.\d)")
_PHONE_LABELED = re.compile(
    r"\b(?:tel(?:efon)?|mobil|handy|fax|rufnummer|telefonnummer|festnetz)\.?\s*(?:nr\.?|nummer)?\s*:?\s*((?:\+|00)?\(?\d[\d \t\-/()]{4,22}\d)",
    re.IGNORECASE,
)

_STREET_SUFFIX = "straße|strasse|str\\.|weg|gasse|platz|allee|ring|damm|ufer|chaussee|steig|pfad|stieg|markt|promenade"
_STREET = re.compile(
    rf"(?<![\w-])(?:"
    rf"(?:[{_UPPER}][{_LOWER}]+[ -])*[{_UPPER}][{_LOWER}-]*(?:{_STREET_SUFFIX})"
    rf"|(?:[{_UPPER}][{_LOWER}]+[ -])+(?:Straße|Strasse|Str\.|Weg|Gasse|Platz|Allee|Ring|Damm|Ufer|Chaussee|Steig|Pfad|Markt)"
    rf"|(?:Am|An der|An den|Auf der|Auf dem|Im|In der|In den|Zum|Zur|Hinter der|Unter den) [{_UPPER}][{_LOWER}-]+"
    rf")\.? ?\d{{1,4}} ?[a-zA-Z]?(?: ?[-/] ?\d{{1,4}}[a-zA-Z]?)?(?![\d\w])"
)
_POSTAL_CITY = re.compile(
    rf"(?<![\w-])(?:D-)?\d{{5}} [{_UPPER}][{_LOWER}]+(?:-[{_UPPER}][{_LOWER}]+)*"
    rf"(?: (?:an der|am|im|in|bei|ob der) [{_UPPER}][{_LOWER}]+| \([{_UPPER}][{_LOWER}.]+\))?"
)

_SALUTATION = re.compile(rf"\b(?:Herrn?|Frau|Hr\.|Fr\.)\s+(?:(?:Dr|Prof)\.\s+)*({_FULL_NAME})")
_NAME_LABELED = re.compile(
    rf"\b(?:Name|Kunde|Kundin|Versicherungsnehmer(?:in)?|VN|Vor-?\s?und\s?Nachname|Ansprechpartner(?:in)?"
    rf"|Inhaber(?:in)?|Halter(?:in)?|Fahrer(?:in)?|Geschädigte[r]?|Anspruchsteller(?:in)?|Empfänger(?:in)?)"
    rf"\s*:\s*({_NAME_WORD}(?:,?\s+(?:{_PARTICLE}\s+)*{_NAME_WORD}){{0,3}})"
)
_WORD = re.compile(rf"{_NAME_WORD}")


@dataclass
class Finding:
    category: str
    placeholder: str
    original: str


@dataclass
class AnonymizationResult:
    text: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.findings)

    def grouped(self) -> list[tuple[str, list[Finding]]]:
        """Fundstellen je Kategorie (in der Reihenfolge von CATEGORIES) fuer die Vorschau."""
        groups = []
        for key in DISPLAY_ORDER:
            label = CATEGORIES[key][1]
            items = [finding for finding in self.findings if finding.category == key]
            if items:
                groups.append((label, items))
        return groups


@dataclass
class _Span:
    start: int
    end: int
    category: str
    key: str  # Vergleichswert: gleiche Angabe -> gleicher Platzhalter


def _norm(value: str) -> str:
    return re.sub(r"[\s.,\-/()]+", "", value).lower()


def _add(spans: list[_Span], match: re.Match, category: str, group: int = 0, key: str | None = None) -> None:
    start, end = match.span(group)
    value = match.group(group)
    spans.append(_Span(start, end, category, key or _norm(value)))


def _iban_valid(value: str) -> bool:
    compact = value.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        digits = "".join(str(int(char, 36)) for char in rearranged)
    except ValueError:
        return False
    return int(digits) % 97 == 1


def _name_key(value: str) -> str:
    """Vergleichswert fuer Namen: Woerter sortiert, damit "Max Mustermann" und
    "Mustermann, Max" denselben Platzhalter erhalten."""
    words = [word.lower() for word in re.findall(rf"[{_LOWER}{_UPPER}'’-]+", value) if word.lower() not in ("von", "van", "de", "der", "zu")]
    return " ".join(sorted(words))


def _detect_identifiers(text: str, spans: list[_Span]) -> None:
    for match in _EMAIL.finditer(text):
        _add(spans, match, "email")
    for match in _IBAN.finditer(text):
        if _iban_valid(match.group(0)):
            _add(spans, match, "iban")
    for match in _BIRTHDATE.finditer(text):
        _add(spans, match, "birthdate", 1)
    for match in _CUSTOMER_NUMBER.finditer(text):
        _add(spans, match, "customer_number", 1)
    for match in _CONTRACT_NUMBER.finditer(text):
        _add(spans, match, "contract_number", 1)
    for match in _CONTRACT_PATTERN.finditer(text):
        _add(spans, match, "contract_number")
    for match in _CLAIM_NUMBER.finditer(text):
        _add(spans, match, "claim_number", 1)
    for pattern in _IDENTIFIERS:
        for match in pattern.finditer(text):
            _add(spans, match, "identifier", 1)
    for pattern in (_SV_PATTERN, _VIN_PATTERN):
        for match in pattern.finditer(text):
            _add(spans, match, "identifier")
    for match in _PLATE_LABELED.finditer(text):
        _add(spans, match, "plate", 1)
    for match in _PLATE.finditer(text):
        _add(spans, match, "plate")
    for match in _PHONE_LABELED.finditer(text):
        _add(spans, match, "phone", 1, key=normalize_phone(match.group(1)) or _norm(match.group(1)))
    for match in _PHONE.finditer(text):
        # Ohne Beschriftung nur echte Rufnummern: mit Vorwahl (0 / +49 / 0049), 9-15 Ziffern.
        normalized = normalize_phone(match.group(0))
        if normalized and re.match(r"\s*(?:\+|00|0)", match.group(0)):
            _add(spans, match, "phone", key=normalized)


def _detect_addresses(text: str, spans: list[_Span]) -> None:
    parts = sorted(
        [(m.start(), m.end()) for m in _STREET.finditer(text)] + [(m.start(), m.end()) for m in _POSTAL_CITY.finditer(text)]
    )
    # Strasse und PLZ/Ort direkt hintereinander (nur Komma/Zeilenumbruch dazwischen) = eine Anschrift.
    merged: list[list[int]] = []
    for start, end in parts:
        if merged and re.fullmatch(r"[\s,]*", text[merged[-1][1] : start]):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in merged:
        spans.append(_Span(start, end, "address", _norm(text[start:end])))


def _detect_names(text: str, spans: list[_Span], known_names: dict[str, str]) -> None:
    def add_name(start: int, end: int, category: str = "name") -> None:
        value = text[start:end].strip(" ,")
        if not value or value.lower() in _NOT_SURNAME:
            return
        spans.append(_Span(start, start + len(text[start:end].rstrip(" ,")), category, _name_key(value)))

    for pattern in (_SALUTATION, _NAME_LABELED):
        for match in pattern.finditer(text):
            # Folgewoerter nach dem Namen ("Herr Müller Kundennummer") gehoeren nicht dazu.
            end = None
            for word in _WORD.finditer(text, match.start(1), match.end(1)):
                if word.group(0).lower() in _NOT_SURNAME:
                    break
                end = word.end()
            if end is not None:
                add_name(match.start(1), end)

    # "Vorname [Vorname] Nachname" bzw. "Nachname, Vorname" mit bekanntem Vornamen.
    words = list(_WORD.finditer(text))
    for index, match in enumerate(words):
        if match.group(0).lower() not in FIRST_NAMES:
            continue
        end_index = index
        while (
            end_index + 1 < len(words)
            and re.fullmatch(r"[ \t]+", text[words[end_index].end() : words[end_index + 1].start()])
            and words[end_index + 1].group(0).lower() in FIRST_NAMES
        ):
            end_index += 1
        following = words[end_index + 1] if end_index + 1 < len(words) else None
        if following and re.fullmatch(r"[ \t]+", text[words[end_index].end() : following.start()]) and following.group(0).lower() not in _NOT_SURNAME:
            add_name(match.start(), following.end())
            continue
        previous = words[index - 1] if index else None
        if previous and re.fullmatch(r",[ \t]*", text[previous.end() : match.start()]) and previous.group(0).lower() not in _NOT_SURNAME:
            add_name(previous.start(), words[end_index].end())

    # Namen aus dem eigenen Kundenstamm bzw. Mitarbeiter des Bueros, in beliebiger Reihenfolge.
    for name, category in known_names.items():
        tokens = [re.escape(token) for token in re.findall(rf"[{_LOWER}{_UPPER}'’-]{{2,}}", name)]
        if len(tokens) < 2:
            continue
        alternation = "|".join(tokens)
        pattern = re.compile(rf"(?<![\w-])(?:{alternation})(?:,?[ \t]+(?:{alternation}))+(?![\w-])", re.IGNORECASE)
        for match in pattern.finditer(text):
            add_name(match.start(), match.end(), category)


def _propagate_names(text: str, spans: list[_Span]) -> None:
    """Ist "Max Mustermann" erkannt, gilt auch ein spaeteres "Herr Mustermann" bzw.
    "Mustermann" allein als dieselbe Person."""
    persons = {}
    for span in spans:
        if span.category in ("name", "employee"):
            surname = re.findall(rf"{_NAME_WORD}", text[span.start : span.end])
            for word in surname:
                if word.lower() not in FIRST_NAMES and len(word) >= 3:
                    persons.setdefault(word, (span.category, span.key))
    for word, (category, key) in persons.items():
        for match in re.finditer(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text):
            spans.append(_Span(match.start(), match.end(), category, key))


def _resolve(spans: list[_Span]) -> list[_Span]:
    """Ueberschneidungen aufloesen: laengere Fundstelle zuerst, bei gleicher Laenge die
    eindeutigere Kategorie (Rangfolge in CATEGORIES)."""
    chosen: list[_Span] = []
    for span in sorted(spans, key=lambda s: (-(s.end - s.start), _RANK[s.category], s.start)):
        if all(span.end <= other.start or span.start >= other.end for other in chosen):
            chosen.append(span)
    return sorted(chosen, key=lambda s: s.start)


def anonymize(text: str, known_names: dict[str, str] | None = None) -> AnonymizationResult:
    """Ersetzt personenbezogene Angaben durch Platzhalter. known_names: {Name: "name"|"employee"}
    aus dem eigenen Buero (siehe office_known_names)."""
    text = (text or "").replace("\r\n", "\n")[:MAX_INPUT_CHARS]
    spans: list[_Span] = []
    _detect_identifiers(text, spans)
    _detect_addresses(text, spans)
    _detect_names(text, spans, known_names or {})
    _propagate_names(text, spans)
    resolved = _resolve(spans)

    numbering: dict[tuple[str, str], str] = {}
    per_category: dict[str, int] = {}
    findings: list[Finding] = []
    pieces: list[str] = []
    position = 0
    for span in resolved:
        ident = (span.category, span.key)
        if ident not in numbering:
            per_category[span.category] = per_category.get(span.category, 0) + 1
            base = CATEGORIES[span.category][0]
            number = per_category[span.category]
            numbering[ident] = f"[{base}]" if number == 1 else f"[{base} {number}]"
            findings.append(Finding(span.category, numbering[ident], text[span.start : span.end]))
        pieces.append(text[position : span.start])
        pieces.append(numbering[ident])
        position = span.end
    pieces.append(text[position:])
    return AnonymizationResult("".join(pieces), findings)


def office_known_names(text: str) -> dict[str, str]:
    """Namen aus dem Kundenstamm und den Mitarbeiterprofilen des EIGENEN Bueros, die im Text
    vorkommen. Beide Abfragen laufen ueber den Tenant-Filter (app/tenancy.py); der Text selbst
    verlaesst den Server nicht."""
    from sqlalchemy import or_

    from app.models import Customer, EmployeeProfile
    from app.services.customer_normalization import normalize_customer_name

    names: dict[str, str] = {}
    words = {word for word in normalize_customer_name(text).split() if len(word) >= 3}
    if words:
        # Vorfilter in der Datenbank wie beim Memo-Kundenabgleich: nur Kunden, deren Name ein
        # Wort des Textes enthaelt; danach muessen alle Namensbestandteile vorkommen.
        probes = sorted(words, key=len, reverse=True)[:MAX_NAME_PROBES]
        rows = (
            Customer.query.with_entities(Customer.name, Customer.name_key)
            .filter(Customer.name_key.isnot(None), or_(*(Customer.name_key.like(f"%{word}%") for word in probes)))
            .limit(MAX_NAME_ROWS)
            .all()
        )
        for row in rows:
            parts = row.name_key.split()
            if len(parts) >= 2 and all(part in words for part in parts):
                names[row.name] = "name"
    lowered = text.lower()
    for profile in EmployeeProfile.query.filter(EmployeeProfile.display_name.isnot(None)).all():
        parts = profile.display_name.split()
        if len(parts) >= 2 and all(part.lower() in lowered for part in parts):
            names[profile.display_name] = "employee"
    return names


# --- Dateien lokal auslesen ------------------------------------------------------------------

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_PDF_PAGES = 20
PDF_EXTENSIONS = {"pdf"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "tif", "tiff", "webp", "bmp"}


class UnreadableFileError(ValueError):
    """Datei kann nicht (lokal) gelesen werden - die Meldung ist fuer den Benutzer bestimmt."""


def extract_text_locally(filename: str, data: bytes) -> str:
    """Text aus PDF oder Bild - ausschliesslich lokal: eingebetteter PDF-Text, sonst Tesseract
    auf dem Server. Anders als die Dokument-Pipeline (app/services/ocr/pipeline.py) gibt es
    keinen Fallback auf OpenAI Vision: Rohdaten verlassen den Server nicht."""
    import io

    import fitz
    from PIL import Image, UnidentifiedImageError

    from app.services.ocr import tesseract_ocr

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if len(data) > MAX_FILE_BYTES:
        raise UnreadableFileError("Die Datei ist zu groß (höchstens 15 MB).")
    if extension in PDF_EXTENSIONS:
        try:
            document = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:  # noqa: BLE001 - beschaedigte PDF
            raise UnreadableFileError("Die PDF-Datei kann nicht gelesen werden.") from exc
        try:
            if document.page_count > MAX_PDF_PAGES:
                raise UnreadableFileError(f"Die PDF hat mehr als {MAX_PDF_PAGES} Seiten.")
            pages = []
            for page in document:
                text = page.get_text("text")
                if len(text.strip()) < 20:
                    # Gescannte Seite: lokal per Tesseract lesen.
                    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                    text = tesseract_ocr.ocr_image_lines(image)
                pages.append(text.strip())
        finally:
            document.close()
        return "\n\n".join(page for page in pages if page)
    if extension in IMAGE_EXTENSIONS:
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except (UnidentifiedImageError, OSError) as exc:
            raise UnreadableFileError("Das Bild kann nicht gelesen werden.") from exc
        return tesseract_ocr.ocr_image_lines(image.convert("RGB")).strip()
    raise UnreadableFileError("Bitte eine PDF-Datei oder ein Bild (PNG, JPG, TIFF, WEBP) hochladen.")
