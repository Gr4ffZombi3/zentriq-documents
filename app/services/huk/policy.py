"""Kfz-Vergleich aus Versicherungsschein: Dokument auslesen, Angaben fuer den HUK-Rechner
vorschlagen, bisherigen Beitrag mit dem HUK-Beitrag vergleichen.

Ablauf
1. Upload (Foto oder PDF, optional zusaetzlich die aktuelle Beitragsrechnung). Die Dateien werden
   nur im Arbeitsspeicher gelesen - nie gespeichert, nie protokolliert.
2. Text lokal: eingebetteter PDF-Text, sonst Tesseract (kein externer Dienst).
3. Lokale Erkennung per Muster (HSN/TSN, SF-Klassen, Kasko, Selbstbeteiligung, Fahrleistung,
   Fahrerkreis, Beitrag, Zahlweise, Werkstattbindung).
4. Optional ein KI-Modell ueber den ZentriqAI-Router - nur mit den relevanten Zeilen und erst nach
   Pseudonymisierung (Name, Anschrift, Vertragsnummer, Kennzeichen, FIN, IBAN, Geburtsdatum werden
   lokal durch Platzhalter ersetzt). Ohne verfuegbares Modell bleibt es bei der lokalen Erkennung.
5. Zusammenfuehren: Stimmen Quellen nicht ueberein oder ist eine Angabe nicht eindeutig, bleibt das
   Feld LEER und bekommt einen Pruefhinweis - es wird nichts geraten oder ergaenzt.
   Die Beitragsrechnung ist fuer SF-Klassen und Beitrag aktueller als der Versicherungsschein.

An huk.de gehen spaeter ausschliesslich die Rechnerfelder aus schema.py (runner.py); die hier
gesammelten Vergleichsdaten (bisheriger Beitrag usw.) verlassen Zentriq nie."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.services.huk import schema

log = logging.getLogger(__name__)

MAX_FILES = 4  # je Dokumentart
MAX_PAGES = 6  # insgesamt (Laufzeit: Auslesen geschieht im Request)
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_OCR_SIDE = 2600  # groessere Fotos werden verkleinert (Tesseract-Laufzeit)
MIN_NATIVE_CHARS = 40
MAX_MODEL_LINES = 60

INTERVALS = ("jährlich", "halbjährlich", "vierteljährlich", "monatlich")
FACTORS = {"jährlich": 1, "halbjährlich": 2, "vierteljährlich": 4, "monatlich": 12}
HUK_INTERVALS = ("jährlich", "halbjährlich", "vierteljährlich")
WORKSHOP = ("ja", "nein")

# Felder, die aus dem Dokument kommen koennen (Rechnerfelder in schema.py).
DOCUMENT_FIELDS = ("hsn", "tsn", "erstzulassung", "fahrleistung", "fahrer", "alle_ueber_25", "sf_haftpflicht",
                   "vollkasko_bisher", "sf_vollkasko", "deckung", "selbstbeteiligung", "selbstbeteiligung_tk", "zahlungsweise")


class PolicyError(Exception):
    """Verstaendliche Meldung fuer den Benutzer (ohne technische Details)."""


# --- 1. Dateien lesen -------------------------------------------------------------------------


@dataclass
class Upload:
    filename: str
    data: bytes
    kind: str = "schein"  # "schein" | "rechnung"


def _ocr(image) -> str:
    from PIL import Image

    from app.services.ocr import tesseract_ocr

    longest = max(image.width, image.height)
    if longest > MAX_OCR_SIDE:
        factor = MAX_OCR_SIDE / longest
        image = image.resize((int(image.width * factor), int(image.height * factor)), Image.LANCZOS)
    elif image.width < 1400:
        factor = min(3, 1400 / max(1, image.width) + 0.5)
        image = image.resize((int(image.width * factor), int(image.height * factor)), Image.LANCZOS)
    return tesseract_ocr.ocr_image_lines(image.convert("L"))


def _pdf_pages(filename: str, data: bytes, budget: int) -> list[str]:
    import fitz
    from PIL import Image

    if len(data) > MAX_PDF_BYTES:
        raise PolicyError(f"„{filename}“ ist zu groß (höchstens {MAX_PDF_BYTES // (1024 * 1024)} MB).")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # beschaedigte oder verschluesselte Datei
        raise PolicyError(f"„{filename}“ kann nicht als PDF gelesen werden.") from exc
    try:
        if doc.needs_pass:
            raise PolicyError(f"„{filename}“ ist passwortgeschützt.")
        if doc.page_count > budget:
            raise PolicyError(f"„{filename}“ hat zu viele Seiten (insgesamt höchstens {MAX_PAGES} Seiten je Auswertung).")
        pages = []
        for page in doc:
            text = page.get_text("text")
            if len(text.strip()) < MIN_NATIVE_CHARS:  # Scan: Seite rendern und lokal erkennen
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                text = _ocr(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            pages.append(text)
        return pages
    finally:
        doc.close()


def read_documents(uploads: list[Upload]) -> dict[str, str]:
    """{"schein": text, "rechnung": text} - nur lokal, Dateien werden nicht gespeichert."""
    from app.services import image_appointments

    texts: dict[str, list[str]] = {"schein": [], "rechnung": []}
    pages = 0
    for kind in ("schein", "rechnung"):
        files = [u for u in uploads if u.kind == kind and u.data]
        if len(files) > MAX_FILES:
            raise PolicyError(f"Bitte höchstens {MAX_FILES} Dateien je Dokument hochladen.")
        for upload in files:
            name = upload.filename or "Datei"
            if name.lower().endswith(".pdf") or upload.data[:5] == b"%PDF-":
                found = _pdf_pages(name, upload.data, MAX_PAGES - pages)
            else:
                if pages >= MAX_PAGES:
                    raise PolicyError(f"Bitte insgesamt höchstens {MAX_PAGES} Seiten bzw. Fotos hochladen.")
                try:
                    image = image_appointments.load_image(name, upload.data)
                except image_appointments.ImageError as exc:
                    raise PolicyError(str(exc).replace("Foto oder einen Screenshot", "Foto oder PDF")) from exc
                found = [_ocr(image)]
            pages += len(found)
            texts[kind].extend(found)
    if not texts["schein"]:
        raise PolicyError("Bitte den Versicherungsschein als Foto oder PDF auswählen.")
    return {kind: "\n".join(t) for kind, t in texts.items()}


# --- 2. Lokale Erkennung -------------------------------------------------------------------------

_AMOUNT = r"(\d{1,3}(?:\.\d{3})*,\d{2})"
_EURO = r"\s*(?:€|EUR|Euro)"
_COVER = re.compile(r"(Kfz-Haftpflicht\w*|Haftpflicht\w*|\bKH\b|Vollkasko\w*|Vollversicherung|\bVK\b|Teilkasko\w*|Teilversicherung|\bTK\b)", re.I)
_NEGATED = re.compile(r"nicht\s+(?:versichert|vereinbart|beantragt|enthalten)|entf[äa]llt|ausgeschlossen|ohne\s+Versicherungsschutz|^\W*[-–—]\W*$|\bnein\b", re.I)
_SF = re.compile(r"(?:Schadenfreiheitsklasse|SF-?\s*Klasse|\bSF)\s*:?\s*(?:SF\s*)?(1\s*/\s*2|½|\d{1,2}(?![\d/])|M\b|S\b)", re.I)
_SF_KLASSE = re.compile(r"\bKlasse\s+(M|0|S)\b", re.I)
# Zwischen Betrag und "Selbstbeteiligung" liest die Texterkennung das €-Zeichen oft als "-", "+" o. Ae.
_SB_CUR = r"\s*(?:€|EUR|Euro|[-+–—=~*eE])?\s*"
_SB_NUM = r"(\d{1,3}(?:\.\d{3})?|\d{4})(?:,00)?(?![,.]?\d)"
_SB = re.compile(rf"(?:{_SB_NUM}{_SB_CUR}(?:Selbstbeteiligung|Selbstbehalt|\bSB\b))"
                 rf"|(?:(?:Selbstbeteiligung|Selbstbehalt|\bSB\b)\s*(?:von|in\s+Höhe\s+von|:)?\s*{_SB_NUM}(?!\s*,))", re.I)
_SB_NONE = re.compile(r"(?:ohne|keine)\s+(?:Selbstbeteiligung|Selbstbehalt|\bSB\b)|(?:Selbstbeteiligung|Selbstbehalt|\bSB\b)\s*:?\s*(?:0(?:,00)?\s*(?:€|EUR)?|keine|ohne)\b", re.I)


def _norm_text(text: str) -> str:
    text = (text or "").replace("\r", "").replace(" ", " ")
    text = re.sub(r"\bEUR\b|\bEuro\b", "€", text)
    return re.sub(r"[ \t]+", " ", text)


def _thousands(value: str) -> int | None:
    digits = re.sub(r"[.\s]", "", value or "")
    return int(digits) if digits.isdigit() else None


def _sf_option(raw: str) -> str | None:
    raw = re.sub(r"\s+", "", raw.strip().upper().replace("½", "1/2"))
    if raw in ("0", "KLASSE 0"):
        return "Klasse 0"
    if raw in ("M", "KLASSE M"):
        return "Klasse M"
    if raw == "S":
        return None  # Sonderklasse S gibt es im HUK-Rechner nicht -> Pruefhinweis
    if raw == "1/2":
        return "SF 1/2"
    if raw.isdigit() and 1 <= int(raw) <= 50:
        return f"SF {int(raw)}"
    return None


def _segments(text: str) -> list[tuple[str, str]]:
    """Text je Sparte: ab dem Spartenbegriff bis zum naechsten Spartenbegriff bzw. Zeilenende."""
    out = []
    for line in text.split("\n"):
        hits = list(_COVER.finditer(line))
        for i, hit in enumerate(hits):
            end = hits[i + 1].start() if i + 1 < len(hits) else len(line)
            word = hit.group(1).lower()
            cover = "vk" if word.startswith(("voll", "vk")) else "tk" if word.startswith(("teil", "tk")) else "kh"
            out.append((cover, line[hit.start():end]))
    return out


@dataclass
class Found:
    """Kandidaten je Angabe (mehrere verschiedene = widerspruechlich) plus Hinweise."""

    values: dict[str, list] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    def add(self, key: str, value) -> None:
        if value in (None, "", []):
            return
        bucket = self.values.setdefault(key, [])
        if value not in bucket:
            bucket.append(value)


def local_extract(text: str) -> Found:
    text = _norm_text(text)
    found = Found()

    for m in re.finditer(r"HSN\s*/\s*TSN\W{0,12}(\d{4})\s*/\s*([A-Z0-9]{3})", text, re.I):
        found.add("hsn", m.group(1))
        found.add("tsn", m.group(2).upper())
    for m in re.finditer(r"(?:\bHSN\b|Herstellerschl\w*|zu\s*2\.1\b)(?:\s*\((?:HSN|2\.1)\))?[\s:.\-]*(?:Nr\.?\s*)?(\d{4})(?!\d)", text, re.I):
        found.add("hsn", m.group(1))
    for m in re.finditer(r"(?:\bTSN\b|Typschl\w*|zu\s*2\.2\b)(?:\s*\((?:TSN|2\.2)\))?[\s:.\-]*(?:Nr\.?\s*)?([A-Za-z0-9]{3})[A-Za-z0-9]{0,7}\b", text, re.I):
        tsn = m.group(1)
        if tsn == tsn.upper() and re.fullmatch(r"[A-Z0-9]{3}", tsn):
            found.add("tsn", tsn)

    for m in re.finditer(r"Erstzulassung\w*[\s:.]*(?:am\s*)?(?:(\d{1,2})\.)?(\d{1,2})[./](\d{4})", text, re.I):
        month = int(m.group(2))
        if 1 <= month <= 12:
            found.add("erstzulassung", f"{month:02d}.{m.group(3)}")

    for m in re.finditer(r"(?:Fahrleistung|Kilometerleistung|Laufleistung|km-Leistung)[^\n\d]{0,40}?(\d{1,3}(?:[.\s]\d{3})+|\d{4,6})", text, re.I):
        km = _thousands(m.group(1))
        if km:
            found.add("fahrleistung_raw", km)

    for m in re.finditer(r"Fahrer(?:kreis)?\s*:\s*([^\n]{3,140})", text, re.I):
        line = m.group(1)
        drivers = []
        if re.search(r"Versicherungsnehmer|\bVN\b|\bSie\b", line, re.I):
            drivers.append("vn")
        if re.search(r"Ehe|Lebens|Partner", line, re.I):
            drivers.append("partner")
        if re.search(r"Kind", line, re.I):
            drivers.append("kinder")
        if re.search(r"beliebig|weitere|sonstige|jede|alle\s+Personen|ohne\s+Einschr", line, re.I):
            drivers += ["vn", "weitere"] if "vn" not in drivers else ["weitere"]
        if drivers:
            order = [o.value for o in schema.FIELD_BY_KEY["fahrer"].options]
            found.add("fahrer", sorted(set(drivers), key=order.index))
        else:
            found.notes["fahrer"] = "Fahrerkreis im Dokument nicht eindeutig – bitte auswählen."
        if re.search(r"(?:ab|über|mind(?:estens|\.)?)\s*25", line, re.I):
            found.add("alle_ueber_25", "ja")
        elif re.search(r"(?:ab|über|mind(?:estens|\.)?)\s*(?:1[78]|2[0-4])\b|unter\s*25", line, re.I):
            found.add("alle_ueber_25", "nein")

    covers = {"kh": False, "vk": False, "tk": False}
    for cover, seg in _segments(text):
        if _NEGATED.search(seg[:80]):
            continue
        covers[cover] = True
        m = _SF.search(seg) or _SF_KLASSE.search(seg)
        if m and cover in ("kh", "vk"):
            option = _sf_option(m.group(1))
            key = "sf_haftpflicht" if cover == "kh" else "sf_vollkasko"
            if option:
                found.add(key, option)
            else:
                found.notes[key] = f"SF-Klasse „{m.group(1)}“ gibt es im HUK-Rechner nicht – bitte prüfen."
        if cover in ("vk", "tk"):
            if _SB_NONE.search(seg):
                found.add(f"sb_{cover}", 0)
            else:
                for sb in _SB.finditer(seg):
                    value = _thousands(sb.group(1) or sb.group(2))
                    if value is not None:
                        found.add(f"sb_{cover}", value)
    if covers["vk"]:
        found.add("deckung", "vollkasko")
    elif covers["tk"]:
        found.add("deckung", "teilkasko")
    elif covers["kh"]:
        found.add("deckung", "haftpflicht")

    for m in re.finditer(r"Werkstatt\w*\s*:?\s*(ja|nein|vereinbart|nicht\s+vereinbart|enthalten|nicht\s+enthalten|mit|ohne)\b", text, re.I):
        word = m.group(1).lower()
        found.add("werkstatt", "nein" if word in ("nein", "ohne") or word.startswith("nicht") else "ja")
    if re.search(r"\bWerkstattbonus\b|\bWerkstattbindung\s+vereinbart\b|\bWerkstatt-?Tarif\b", text, re.I):
        found.add("werkstatt", "ja")

    for m in re.finditer(r"(?:Zahlweise|Zahlungsweise|Zahlungsart|Zahlungsabschnitt|Zahlung)\s*:?\s*(jährlich|jaehrlich|halbjährlich|halbjaehrlich|vierteljährlich|vierteljaehrlich|monatlich)", text, re.I):
        found.add("zahlweise", m.group(1).lower().replace("ae", "ä"))

    for m in re.finditer(rf"(?:Jahresbeitrag|Jahresprämie|Beitrag\s+(?:pro|je)\s+Jahr|jährliche[rn]?\s+(?:Gesamt)?beitrag)[^\n\d]{{0,40}}?{_AMOUNT}{_EURO}?", text, re.I):
        found.add("jahresbeitrag", m.group(1))
    for m in re.finditer(rf"(?:Monatsbeitrag|monatliche[rn]?\s+Beitrag|Beitrag\s+(?:pro|je)\s+Monat)[^\n\d]{{0,40}}?{_AMOUNT}{_EURO}?", text, re.I):
        found.add("ratenbeitrag", ("monatlich", m.group(1)))
    for m in re.finditer(rf"(?:Beitrag\s+je\s+Zahlungsabschnitt|zu\s+zahlender\s+Beitrag|Zahlbetrag|Gesamtbeitrag|Gesamtbetrag|Beitrag\s+gesamt)[^\n\d]{{0,40}}?{_AMOUNT}{_EURO}?", text, re.I):
        found.add("beitrag", m.group(1))
    return found


# --- 3. KI-Unterstuetzung (pseudonymisiert) ---------------------------------------------------------

_RELEVANT = re.compile(
    r"HSN|TSN|Hersteller|Typschl|2\.1|2\.2|Erstzulass|Fahrleistung|Kilometer|Fahrer|Haftpflicht|Kasko|\bKH\b|\bVK\b|\bTK\b|"
    r"SF|Schadenfreiheit|Selbstbeteil|Selbstbehalt|Werkstatt|Zahlweise|Zahlungs|Beitrag|Prämie|monatlich|jährlich|Summe|Gesamt",
    re.I,
)
MODEL_KEYS = {
    "hsn": "HSN, 4 Ziffern", "tsn": "TSN, erste 3 Zeichen", "erstzulassung": "Erstzulassung MM.JJJJ",
    "fahrleistung": "jährliche Fahrleistung in km als ganze Zahl", "fahrerkreis": "Liste aus vn, partner, kinder, weitere",
    "alle_ueber_25": "ja/nein, ob alle Fahrer mindestens 25 sind", "sf_haftpflicht": "SF-Klasse Haftpflicht, z. B. 'SF 12', 'SF 1/2', 'Klasse 0', 'Klasse M'",
    "sf_vollkasko": "SF-Klasse Vollkasko (leer ohne Vollkasko)", "deckung": "haftpflicht, teilkasko oder vollkasko",
    "sb_vollkasko": "Selbstbeteiligung Vollkasko in Euro als Zahl", "sb_teilkasko": "Selbstbeteiligung Teilkasko in Euro als Zahl (0 = ohne)",
    "werkstattbindung": "ja/nein", "zahlweise": "jährlich, halbjährlich, vierteljährlich oder monatlich",
    "beitrag": "zu zahlender Beitrag je Zahlungsabschnitt, z. B. '44,22'", "jahresbeitrag": "Jahresbeitrag, falls genannt",
}


def model_lines(text: str) -> list[str]:
    lines = [line.strip() for line in _norm_text(text).split("\n") if line.strip()]
    return [line[:200] for line in lines if _RELEVANT.search(line)][:MAX_MODEL_LINES]


def pseudonymize(lines: list[str]) -> str:
    from app.services.anonymize import anonymize

    return anonymize("\n".join(lines)).text


def model_extract(user, text: str) -> Found:
    """Modellvorschlaege fuer dieselben Angaben; ohne Modell leer. Das Modell sieht nur
    pseudonymisierte, relevante Zeilen - nie Namen, Anschrift, Vertrags- oder Kennzeichen."""
    from app.services.ai import router

    found = Found()
    lines = model_lines(text)
    if not lines or not router.can_answer():
        return found
    keys = "\n".join(f'- "{k}": {v}' for k, v in MODEL_KEYS.items())
    prompt = (
        "Die folgenden Zeilen stammen per Texterkennung aus einem deutschen Kfz-Versicherungsschein bzw. einer "
        "Beitragsrechnung. Personenangaben sind durch Platzhalter ersetzt. Antworte NUR mit einem JSON-Objekt mit "
        f"diesen Schlüsseln:\n{keys}\nÜbernimm nur, was ausdrücklich im Text steht. Erfinde, schätze oder rechne "
        "nichts: Fehlt eine Angabe oder ist sie unklar, gib \"\" (beim Fahrerkreis []) zurück.\n\n<<<\n"
        + pseudonymize(lines) + "\n>>>"
    )
    try:
        completion = router.complete(
            system="Du überträgst Angaben aus Versicherungsdokumenten in JSON. Nur JSON, keine Erklärungen.",
            messages=[{"role": "user", "content": prompt}], user_id=user.id, tenant_id=user.tenant_id,
            customer_data=False, purpose="huk_policy", max_tokens=700,
        )
    except router.AiError as exc:
        log.info("huk.policy.model_unavailable type=%s", exc.error_type)
        return found
    raw = completion.text or ""
    raw = raw[raw.find("{"): raw.rfind("}") + 1] if "{" in raw else "{}"
    try:
        data = json.loads(raw)
    except ValueError:
        log.info("huk.policy.model_invalid_json")
        return found
    if not isinstance(data, dict):
        return found
    text_of = {k: str(data.get(k) or "").strip() for k in MODEL_KEYS if k != "fahrerkreis"}
    if re.fullmatch(r"\d{4}", text_of["hsn"]):
        found.add("hsn", text_of["hsn"])
    if re.fullmatch(r"[A-Z0-9]{3,10}", text_of["tsn"].upper()):
        found.add("tsn", text_of["tsn"].upper()[:3])
    month = schema.normalize_month(text_of["erstzulassung"])
    if re.fullmatch(r"(0[1-9]|1[0-2])\.\d{4}", month):
        found.add("erstzulassung", month)
    km = _thousands(text_of["fahrleistung"])
    if km:
        found.add("fahrleistung_raw", km)
    drivers = data.get("fahrerkreis")
    allowed = [o.value for o in schema.FIELD_BY_KEY["fahrer"].options]
    if isinstance(drivers, list) and drivers and all(d in allowed for d in drivers):
        found.add("fahrer", sorted(set(drivers), key=allowed.index))
    if text_of["alle_ueber_25"] in ("ja", "nein"):
        found.add("alle_ueber_25", text_of["alle_ueber_25"])
    for key in ("sf_haftpflicht", "sf_vollkasko"):
        value = re.sub(r"^(?:SF|Klasse)\s*", "", text_of[key], flags=re.I)
        option = _sf_option(value) if value else None
        if option:
            found.add(key, option)
    if text_of["deckung"] in ("haftpflicht", "teilkasko", "vollkasko"):
        found.add("deckung", text_of["deckung"])
    for key, target in (("sb_vollkasko", "sb_vk"), ("sb_teilkasko", "sb_tk")):
        value = _thousands(text_of[key].replace(",00", "").replace("€", ""))
        if value is not None:
            found.add(target, value)
    if text_of["werkstattbindung"] in WORKSHOP:
        found.add("werkstatt", text_of["werkstattbindung"])
    if text_of["zahlweise"] in INTERVALS:
        found.add("zahlweise", text_of["zahlweise"])
    for key in ("beitrag", "jahresbeitrag"):
        m = re.search(_AMOUNT, text_of[key])
        if m:
            found.add(key, m.group(1))
    return found


# --- 4. Zusammenfuehren -----------------------------------------------------------------------------

LABELS = {
    "hsn": "HSN", "tsn": "TSN", "erstzulassung": "Erstzulassung", "fahrleistung_raw": "Fahrleistung", "fahrer": "Fahrerkreis",
    "alle_ueber_25": "Alter der Fahrer", "sf_haftpflicht": "SF-Klasse Haftpflicht", "sf_vollkasko": "SF-Klasse Vollkasko",
    "deckung": "Kaskoart", "sb_vk": "Selbstbeteiligung Vollkasko", "sb_tk": "Selbstbeteiligung Teilkasko",
    "werkstatt": "Werkstattbindung", "zahlweise": "Zahlweise", "beitrag": "Beitrag", "jahresbeitrag": "Jahresbeitrag",
}
# Fuer diese Angaben ist die (juengere) Beitragsrechnung massgeblich.
INVOICE_FIRST = ("sf_haftpflicht", "sf_vollkasko", "beitrag", "jahresbeitrag", "ratenbeitrag", "zahlweise")


def _show(key: str, value) -> str:
    if key == "fahrer":
        return schema.label_of("fahrer", value)
    if key == "fahrleistung_raw":
        return schema.km_label(value)
    if key in ("sb_vk", "sb_tk"):
        return "ohne" if value == 0 else f"{value} €"
    if key == "ratenbeitrag":
        return f"{value[1]} € {value[0]}"
    if key in ("beitrag", "jahresbeitrag"):
        return f"{value} €"
    return str(value)


def merge(schein_local: Found, schein_model: Found, invoice_local: Found, invoice_model: Found) -> tuple[dict, dict]:
    """Gibt (eindeutige Angaben, Pruefhinweise) zurueck. Widerspruch -> keine Angabe + Hinweis."""
    def combine(local: Found, model: Found) -> tuple[dict, dict]:
        values, notes = {}, dict(local.notes)
        for key in set(local.values) | set(model.values):
            candidates = list(local.values.get(key, []))
            for v in model.values.get(key, []):
                if v not in candidates:
                    candidates.append(v)
            if len(candidates) == 1:
                values[key] = candidates[0]
                notes.pop(key, None)
            else:
                notes[key] = "Widersprüchlich im Dokument: " + " oder ".join(_show(key, v) for v in candidates) + " – bitte prüfen."
        return values, notes

    schein, notes = combine(schein_local, schein_model)
    invoice, invoice_notes = combine(invoice_local, invoice_model)
    for key, value in invoice.items():
        if key in INVOICE_FIRST:
            if key in schein and schein[key] != value:
                notes[f"info_{key}"] = (f"{LABELS.get(key, key)}: laut Beitragsrechnung {_show(key, value)}, laut Versicherungsschein "
                                        f"{_show(key, schein[key])} – die Beitragsrechnung ist aktueller und wird verwendet.")
            schein[key] = value
            notes.pop(key, None)
        elif key not in schein and key not in notes:
            schein[key] = value
        elif key in schein and schein[key] != value:
            notes[key] = (f"Widersprüchlich: Versicherungsschein {_show(key, schein[key])}, Beitragsrechnung "
                          f"{_show(key, value)} – bitte prüfen.")
            schein.pop(key)
    for key, note in invoice_notes.items():
        if key not in schein:
            notes.setdefault(key, note)
    return schein, notes


def _money(value: str | None) -> Decimal | None:
    try:
        return Decimal((value or "").replace(".", "").replace(",", ".").replace("€", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def fmt_money(value: Decimal | None) -> str:
    if value is None:
        return "–"
    sign = "-" if value < 0 else ""
    return sign + f"{abs(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def _deductible_option(vk: int | None, tk: int | None, deckung: str) -> tuple[str | None, str | None]:
    """HUK-Auswahltext fuer die Selbstbeteiligung (nur exakt passende Stufe)."""
    def euro(v: int) -> str:
        return f"{v:,}".replace(",", ".") + " €"

    if deckung == "vollkasko":
        if vk is None or tk is None:
            return None, "Selbstbeteiligung (Vollkasko/Teilkasko) im Dokument nicht vollständig erkannt – bitte auswählen."
        label = f"Vollkasko {euro(vk)} / Teilkasko {'ohne' if tk == 0 else euro(tk)}"
        if label in schema.DEDUCTIBLES:
            return label, None
        return None, f"Bisherige Selbstbeteiligung ({label}) gibt es im HUK-Rechner so nicht – bitte die passende Stufe wählen."
    if deckung == "teilkasko":
        if tk is None:
            return None, "Selbstbeteiligung Teilkasko im Dokument nicht erkannt – bitte auswählen."
        label = "Teilkasko ohne" if tk == 0 else f"Teilkasko {euro(tk)}"
        if label in schema.TK_DEDUCTIBLES:
            return label, None
        return None, f"Bisherige Selbstbeteiligung ({label}) gibt es im HUK-Rechner so nicht – bitte die passende Stufe wählen."
    return None, None


def build(found: dict, notes: dict) -> tuple[dict, dict, dict]:
    """Aus den eindeutigen Angaben: (Rechnerfelder, bisheriger Vertrag, Pruefhinweise je Feld)."""
    values: dict = {}
    flags: dict[str, str] = {}
    for key in ("hsn", "tsn", "erstzulassung", "fahrer", "alle_ueber_25", "sf_haftpflicht", "sf_vollkasko", "deckung"):
        if key in found:
            values[key] = found[key]
    km = found.get("fahrleistung_raw")
    if km is not None:
        if km % 1000 == 0 and 1000 <= km <= 99000:
            values["fahrleistung"] = str(km)
        else:
            flags["fahrleistung"] = f"Laut Dokument {schema.km_label(km)} – der HUK-Rechner rechnet in 1.000er-Schritten. Bitte wählen."
    deckung = values.get("deckung")
    if deckung == "vollkasko" or "sf_vollkasko" in values:
        values["vollkasko_bisher"] = "ja"
    elif deckung in ("teilkasko", "haftpflicht"):
        values["vollkasko_bisher"] = "nein"
    if deckung:
        option, problem = _deductible_option(found.get("sb_vk"), found.get("sb_tk"), deckung)
        if option:
            values["selbstbeteiligung" if deckung == "vollkasko" else "selbstbeteiligung_tk"] = option
        elif problem:
            flags["selbstbeteiligung" if deckung == "vollkasko" else "selbstbeteiligung_tk"] = problem
    interval = found.get("zahlweise")
    if interval in HUK_INTERVALS:
        values["zahlungsweise"] = interval
    elif interval == "monatlich":
        flags["zahlungsweise"] = "Bisher monatlich – der HUK-Rechner bietet jährlich, halbjährlich oder vierteljährlich. Bitte wählen."

    # Rechnerfelder nur mit gueltigem Format (schema.validate); Ungueltiges wird Hinweis.
    _missing, errors = schema.validate(values, schema_today())
    for key in list(values):
        if key in errors and key in DOCUMENT_FIELDS:
            flags[key] = f"Erkannter Wert „{schema.label_of(key, values[key])}“ ist ungültig: {errors[key]}"
            values.pop(key)

    previous = {
        "fahrleistung": km,
        "fahrer": found.get("fahrer"),
        "alle_ueber_25": found.get("alle_ueber_25"),
        "deckung": deckung,
        "sb_vk": found.get("sb_vk"),
        "sb_tk": found.get("sb_tk"),
        "werkstatt": found.get("werkstatt"),
        "zahlweise": interval,
        "beitrag": None,
        "jahresbeitrag": found.get("jahresbeitrag"),
    }
    rate = found.get("ratenbeitrag")
    if found.get("beitrag"):
        previous["beitrag"] = found["beitrag"]
    elif rate and (not interval or interval == rate[0]):
        previous["beitrag"], previous["zahlweise"] = rate[1], rate[0]
    elif found.get("jahresbeitrag") and not interval:
        previous["beitrag"], previous["zahlweise"] = found["jahresbeitrag"], "jährlich"
    elif found.get("jahresbeitrag") and interval == "jährlich":
        previous["beitrag"] = found["jahresbeitrag"]
    if previous["beitrag"] and not previous["zahlweise"]:
        flags["bisher_zahlweise"] = "Zahlweise des bisherigen Beitrags nicht erkannt – bitte angeben."
    if not previous["beitrag"]:
        flags["bisher_beitrag"] = notes.pop("beitrag", None) or "Bisheriger Beitrag nicht erkannt – bitte aus dem Dokument eintragen (optional)."
    elif previous["jahresbeitrag"] and previous["zahlweise"] and previous["zahlweise"] != "jährlich":
        annual = _money(previous["beitrag"]) * FACTORS[previous["zahlweise"]]
        if _money(previous["jahresbeitrag"]) != annual:
            flags["info_jahresbeitrag"] = (f"Im Dokument steht ein Jahresbeitrag von {previous['jahresbeitrag']} €, "
                                           f"{FACTORS[previous['zahlweise']]} × {previous['beitrag']} € ergeben {fmt_money(annual)} "
                                           "(z. B. Ratenzuschlag). Verglichen wird der tatsächlich zu zahlende Betrag.")

    for key, note in notes.items():
        target = {"fahrleistung_raw": "fahrleistung", "sb_vk": "selbstbeteiligung", "sb_tk": "selbstbeteiligung_tk",
                  "werkstatt": "bisher_werkstatt", "zahlweise": "zahlungsweise", "jahresbeitrag": "bisher_beitrag"}.get(key, key)
        if target == "selbstbeteiligung_tk" and deckung == "vollkasko":
            target = "selbstbeteiligung"
        flags.setdefault(target, note)
    return values, previous, flags


def schema_today():
    from app.services.timetracking.clock import local_today

    return local_today()


def analyse(user, uploads: list[Upload]) -> dict:
    """Komplette Auswertung. Rueckgabe ohne Dokumenttext (der wird nicht aufbewahrt)."""
    texts = read_documents(uploads)
    schein_local = local_extract(texts["schein"])
    invoice_local = local_extract(texts["rechnung"]) if texts["rechnung"] else Found()
    schein_model = model_extract(user, texts["schein"])
    invoice_model = model_extract(user, texts["rechnung"]) if texts["rechnung"] else Found()
    found, notes = merge(schein_local, schein_model, invoice_local, invoice_model)
    values, previous, flags = build(found, notes)
    used_model = bool(schein_model.values or invoice_model.values)
    log.info("huk.policy.analysed fields=%s flags=%s model=%s invoice=%s", len(values), len(flags), used_model, bool(texts["rechnung"]))
    return {"values": values, "previous": previous, "flags": flags, "model": used_model,
            "readable": bool(values or previous.get("beitrag"))}


# --- 5. Vergleich ----------------------------------------------------------------------------------


def annual(amount: Decimal | None, interval: str | None) -> Decimal | None:
    if amount is None or interval not in FACTORS:
        return None
    return (amount * FACTORS[interval]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _calc_text(amount: Decimal | None, interval: str | None) -> str:
    if amount is None or interval not in FACTORS:
        return ""
    if interval == "jährlich":
        return f"{fmt_money(amount)} jährlich"
    return f"{FACTORS[interval]} × {fmt_money(amount)} ({interval}) = {fmt_money(annual(amount, interval))}"


def _interval_of(text: str | None, fallback: str | None) -> str | None:
    text = (text or "").lower()
    for interval in ("halbjährlich", "vierteljährlich", "monatlich", "jährlich"):
        if interval in text:
            return interval
    return fallback if fallback in FACTORS else None


def _sb_numbers(label: str | None) -> tuple[int | None, int | None]:
    if not label:
        return None, None
    vk = re.search(r"Vollkasko ([\d.]+) €", label)
    tk = re.search(r"Teilkasko (ohne|[\d.]+ €)", label)
    vk_v = _thousands(vk.group(1)) if vk else None
    tk_v = (0 if tk.group(1) == "ohne" else _thousands(tk.group(1).replace(" €", ""))) if tk else None
    return vk_v, tk_v


def huk_offer(job) -> dict | None:
    """Gewaehlter HUK-Tarif (automatisch) bzw. manuell eingetragener Beitrag."""
    result = job.result or {}
    if job.status == "done" and result.get("source") == "huk.de":
        tariffs = []
        for t in result.get("tariffs") or []:
            amount = _money(t.get("price"))
            interval = _interval_of(t.get("period"), result.get("payment_interval"))
            tariffs.append({**t, "amount": amount, "interval": interval, "annual": annual(amount, interval),
                            "workshop": "SELECT" in (t.get("name") or "").upper()})
        selected = next((t for t in tariffs if t.get("selected")), None)
        if selected is None or selected["annual"] is None:
            return None
        return {"source": "auto", "selected": selected, "tariffs": tariffs}
    manual = (job.comparison or {}).get("manual")
    if manual:
        amount = _money(manual.get("betrag"))
        interval = manual.get("zahlweise")
        selected = {"name": manual.get("tarif") or "HUK-Tarif (manuell eingetragen)", "amount": amount, "interval": interval,
                    "annual": annual(amount, interval), "workshop": manual.get("werkstatt") == "ja",
                    "workshop_known": manual.get("werkstatt") in WORKSHOP}
        return {"source": "manual", "selected": selected, "tariffs": [selected]}
    return None


def deviations(job, offer: dict | None) -> list[dict]:
    """Abweichungen zwischen bisherigem Vertrag (Dokument) und HUK-Berechnung."""
    comp = job.comparison or {}
    prev = comp.get("previous") or {}
    values = job.input_data or {}
    out = []

    def add(label, before, after, relevant=True):
        out.append({"label": label, "before": before, "after": after, "relevant": relevant})

    if prev.get("fahrleistung") and values.get("fahrleistung") and int(values["fahrleistung"]) != int(prev["fahrleistung"]):
        add("Fahrleistung", schema.km_label(prev["fahrleistung"]), schema.km_label(values["fahrleistung"]))
    if prev.get("fahrer") and values.get("fahrer") and set(prev["fahrer"]) != set(values["fahrer"]):
        add("Fahrerkreis", schema.label_of("fahrer", prev["fahrer"]), schema.label_of("fahrer", values["fahrer"]))
    if prev.get("alle_ueber_25") and values.get("alle_ueber_25") and prev["alle_ueber_25"] != values["alle_ueber_25"]:
        add("Alle Fahrer ab 25", prev["alle_ueber_25"], values["alle_ueber_25"])
    if prev.get("deckung") and values.get("deckung") and prev["deckung"] != values["deckung"]:
        add("Kasko", schema.label_of("deckung", prev["deckung"]), schema.label_of("deckung", values["deckung"]))
    huk_sb = values.get("selbstbeteiligung") or values.get("selbstbeteiligung_tk")
    vk, tk = _sb_numbers(huk_sb)
    if (prev.get("sb_vk") is not None or prev.get("sb_tk") is not None) and huk_sb:
        before_vk, before_tk = prev.get("sb_vk"), prev.get("sb_tk")
        differs = (values.get("deckung") == "vollkasko" and (before_vk != vk or before_tk != tk)) or (
            values.get("deckung") == "teilkasko" and before_tk != tk)
        if differs:
            parts = []
            if before_vk is not None:
                parts.append(f"Vollkasko {_show('sb_vk', before_vk)}")
            if before_tk is not None:
                parts.append(f"Teilkasko {_show('sb_tk', before_tk)}")
            add("Selbstbeteiligung", " / ".join(parts) or "–", huk_sb)
    if offer:
        sel = offer["selected"]
        known = offer["source"] == "auto" or sel.get("workshop_known")
        after = ("ja (Tarif " + sel["name"] + ")" if sel["workshop"] else "nein") if known else "nicht angegeben"
        before = {"ja": "ja", "nein": "nein"}.get(prev.get("werkstatt"), "nicht erkannt")
        if before != after.split(" ")[0] or before == "nicht erkannt":
            add("Werkstattbindung", before, after, relevant=before != "nicht erkannt" or sel["workshop"])
        if prev.get("zahlweise") and sel.get("interval") and prev["zahlweise"] != sel["interval"]:
            add("Zahlweise", prev["zahlweise"], sel["interval"], relevant=False)
    return out


def comparison_view(job) -> dict | None:
    comp = job.comparison
    if not comp:
        return None
    prev = comp.get("previous") or {}
    before_amount = _money(prev.get("beitrag"))
    before_annual = annual(before_amount, prev.get("zahlweise"))
    offer = huk_offer(job)
    huk_annual = offer["selected"]["annual"] if offer else None
    diff = (huk_annual - before_annual) if (huk_annual is not None and before_annual is not None) else None
    tariffs = []
    if offer:
        for t in offer["tariffs"]:
            tariffs.append({**t, "annual_text": fmt_money(t["annual"]),
                            "diff": (t["annual"] - before_annual) if (t["annual"] is not None and before_annual is not None) else None})
    return {
        "before_annual": before_annual,
        "before_calc": _calc_text(before_amount, prev.get("zahlweise")),
        "before_missing": before_annual is None,
        "offer": offer,
        "huk_annual": huk_annual,
        "huk_calc": _calc_text(offer["selected"]["amount"], offer["selected"]["interval"]) if offer else "",
        "diff": diff,
        "tariffs": tariffs,
        "deviations": deviations(job, offer),
        "fmt": fmt_money,
    }


def parse_amount(value: str | None) -> str | None:
    """Benutzereingabe '44,22' / '44.22' / '1.044,22 €' -> '44,22' bzw. '1.044,22' (oder None)."""
    raw = (value or "").replace("€", "").replace(" ", "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d+\.\d{1,2}", raw):
        raw = raw.replace(".", ",")
    amount = _money(raw)
    if amount is None or amount <= 0 or amount > Decimal("100000"):
        return None
    return fmt_money(amount).replace(" €", "")


def new_comparison(result: dict) -> dict:
    return {"source": "versicherungsschein", "previous": result["previous"], "flags": result["flags"],
            "extracted": sorted(result["values"]), "model": result["model"]}
