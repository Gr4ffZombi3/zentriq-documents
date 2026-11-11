"""Eingabefelder fuer den HUK-Pkw-Tarifrechner (Stand der Erkundung: 06.11.2026,
https://www.huk.de/tarifrechner/pkw).

Jedes Feld entspricht einer Frage des Rechners bis zur Seite "Angebot". Alle beitragsrelevanten
Angaben sind Pflicht - es gibt keine stillen Standardwerte, auch wenn der Rechner selbst
Antworten vorbelegt. Vorschlaege (z. B. Versicherungsbeginn) werden als solche gekennzeichnet
und muessen vom Benutzer bestaetigt werden. "label" ist der exakte Text der Auswahl im Rechner."""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class Option:
    value: str
    label: str  # Text im HUK-Rechner (exakt)
    short: str = ""  # Anzeige in Zentriq


@dataclass(frozen=True)
class Field:
    key: str
    question: str
    section: str
    kind: str  # "text" | "choice" | "multi" | "sf"
    options: tuple[Option, ...] = field(default_factory=tuple)
    hint: str = ""
    pattern: str | None = None
    placeholder: str = ""
    # Feld nur relevant, wenn condition(values) True ist.
    condition: object = None


SF_CLASSES = tuple(Option(v, v) for v in [f"SF {n}" for n in range(50, 0, -1)] + ["SF 1/2", "Klasse 0", "Klasse M"])
DEDUCTIBLES = (
    "Vollkasko 150 € / Teilkasko ohne", "Vollkasko 150 € / Teilkasko 150 €", "Vollkasko 300 € / Teilkasko ohne",
    "Vollkasko 300 € / Teilkasko 150 €", "Vollkasko 300 € / Teilkasko 300 €", "Vollkasko 500 € / Teilkasko ohne",
    "Vollkasko 500 € / Teilkasko 150 €", "Vollkasko 500 € / Teilkasko 500 €", "Vollkasko 1.000 € / Teilkasko ohne",
    "Vollkasko 1.000 € / Teilkasko 150 €", "Vollkasko 1.000 € / Teilkasko 500 €", "Vollkasko 2.500 € / Teilkasko 2.500 €",
)
TK_DEDUCTIBLES = ("Teilkasko ohne", "Teilkasko 150 €", "Teilkasko 300 €", "Teilkasko 500 €", "Teilkasko 2.500 €")


def _has_more_drivers(v: dict) -> bool:
    return any(d != "vn" for d in (v.get("fahrer") or []))


FIELDS: tuple[Field, ...] = (
    # --- Fahrzeugdaten
    Field("hsn", "HSN (Herstellerschlüsselnummer, Feld 2.1)", "Fahrzeug", "text", pattern=r"^\d{4}$", placeholder="0603"),
    Field("tsn", "TSN (Typschlüsselnummer, Feld 2.2, erste 3 Zeichen)", "Fahrzeug", "text", pattern=r"^[A-Z0-9]{3}$", placeholder="BGU"),
    Field("zulassung", "Ist das Fahrzeug bereits auf den Versicherungsnehmer zugelassen?", "Fahrzeug", "choice", (
        Option("wechsel", "Ja, ich möchte die Versicherung wechseln.", "Ja, Versicherungswechsel"),
        Option("neu", "Nein, das Auto wird noch zugelassen bzw. umgemeldet.", "Nein, Neuzulassung/Ummeldung"),
    )),
    Field("versicherungsbeginn", "Gewünschter Versicherungsbeginn", "Fahrzeug", "text", pattern=r"^\d{2}\.\d{2}\.\d{4}$", placeholder="TT.MM.JJJJ"),
    Field("erstzulassung", "Erstzulassung (Monat/Jahr)", "Fahrzeug", "text", pattern=r"^\d{2}\.\d{4}$", placeholder="MM.JJJJ"),
    Field("halter", "Wer ist Fahrzeughalter?", "Fahrzeug", "choice", (
        Option("ich", "Ich (Versicherungsnehmer)", "Versicherungsnehmer"), Option("ehepartner", "Ehepartner"),
        Option("lebenspartner", "Lebenspartner"), Option("kind", "Kind"), Option("sonstiger", "Sonstiger Halter"),
    )),
    Field("plz", "Postleitzahl des Wohnorts", "Fahrzeug", "text", pattern=r"^\d{5}$", placeholder="96450"),
    Field("halterzulassung", "Seit wann ist das Fahrzeug auf den Halter zugelassen? (Monat/Jahr)", "Fahrzeug", "text",
          pattern=r"^\d{2}\.\d{4}$", placeholder="MM.JJJJ", condition=lambda v: v.get("zulassung") == "wechsel"),
    Field("kennzeichen", "Kennzeichen", "Fahrzeug", "choice", (Option("normal", "Normales Kennzeichen"), Option("saison", "Saisonkennzeichen"))),
    Field("finanzierung", "Finanzierung", "Fahrzeug", "choice", (
        Option("eigen", "Eigenfinanziert"), Option("kredit", "Kreditfinanziert"), Option("leasing", "Leasing"),
    )),
    # --- Vertragspartner
    Field("geburtsdatum", "Geburtsdatum des Versicherungsnehmers", "Versicherungsnehmer", "text", pattern=r"^\d{2}\.\d{2}\.\d{4}$", placeholder="TT.MM.JJJJ"),
    Field("beschaeftigung", "Beschäftigung (als Rentner/Pensionär: ehemalige)", "Versicherungsnehmer", "choice", (
        Option("nicht_oeffentlich", "Nicht öffentlicher Dienst"), Option("oeffentlich", "Öffentlicher Dienst"),
        Option("selbststaendig", "Selbstständig"), Option("keine", "Kein Beschäftigungsverhältnis"),
    )),
    Field("ingenieur", "Ist der Versicherungsnehmer Ingenieur?", "Versicherungsnehmer", "choice", (Option("ja", "Ja"), Option("nein", "Nein"))),
    Field("wohneigentum", "Haus- oder Wohneigentum (auch des Ehe-/Lebenspartners)", "Versicherungsnehmer", "choice", (
        Option("kein", "Kein Haus- oder Wohneigentum"), Option("haus", "Hauseigentum"), Option("wohnung", "Eigentumswohnung"),
    )),
    # --- Nutzung & Fahrer
    Field("fahrleistung", "Jährliche Fahrleistung", "Nutzung", "km", pattern=r"^\d{1,2}000$", placeholder="12.000"),
    Field("abstellplatz", "Regelmäßiger Abstellplatz", "Nutzung", "choice", (
        Option("garage", "Einzel-/Doppelgarage"), Option("tiefgarage", "Reservierter Stellplatz in Tief-/Sammelgarage"),
        Option("carport", "Carport"), Option("sonstiger", "Sonstiger Stellplatz"),
    )),
    Field("privat", "Überwiegend private Nutzung inkl. Fahrten zur Arbeitsstelle?", "Nutzung", "choice", (
        Option("ja", "Ja"), Option("nein", "Nein - überwiegend geschäftlich", "Nein, überwiegend geschäftlich"),
    )),
    Field("fahrer", "Wer fährt mit dem Fahrzeug? (Mehrfachauswahl)", "Nutzung", "multi", (
        Option("vn", "Ich (Versicherungsnehmer)", "Versicherungsnehmer"), Option("partner", "Ehepartner/Lebenspartner"),
        Option("kinder", "Kind(er) des Versicherungsnehmers"), Option("weitere", "Weitere Personen"),
    )),
    Field("alle_ueber_25", "Sind alle Fahrer mindestens 25 Jahre alt?", "Nutzung", "choice", (Option("ja", "Ja"), Option("nein", "Nein")),
          condition=_has_more_drivers),
    # Seit 11.11.2026 fragt der Rechner bei weiteren Fahrern zusaetzlich nach Fahrern ab 70.
    Field("fahrer_ueber_70", "Gibt es Fahrer, die 70 Jahre oder älter sind?", "Nutzung", "choice", (Option("ja", "Ja"), Option("nein", "Nein")),
          condition=_has_more_drivers),
    # --- SF-Klassen
    Field("sf_haftpflicht", "Schadenfreiheitsklasse Haftpflicht (laut letzter Beitragsrechnung)", "SF-Klasse", "sf", SF_CLASSES),
    Field("vollkasko_bisher", "Bestand für das bisherige Fahrzeug eine Vollkasko?", "SF-Klasse", "choice", (Option("ja", "Ja"), Option("nein", "Nein"))),
    Field("sf_vollkasko", "Schadenfreiheitsklasse Vollkasko", "SF-Klasse", "sf", SF_CLASSES, condition=lambda v: v.get("vollkasko_bisher") == "ja"),
    # --- Gewuenschter Schutz
    Field("deckung", "Gewünschter Versicherungsschutz", "Schutz", "choice", (
        Option("haftpflicht", "Kfz-Haftpflicht"), Option("teilkasko", "Teilkasko inkl. Kfz-Haftpflicht"),
        Option("vollkasko", "Vollkasko inkl. Kfz-Haftpflicht"),
    )),
    Field("selbstbeteiligung", "Selbstbeteiligung", "Schutz", "choice", tuple(Option(d, d) for d in DEDUCTIBLES),
          condition=lambda v: v.get("deckung") == "vollkasko"),
    Field("selbstbeteiligung_tk", "Selbstbeteiligung Teilkasko", "Schutz", "choice", tuple(Option(d, d) for d in TK_DEDUCTIBLES),
          hint="Wird mit den im Rechner angebotenen Stufen abgeglichen.", condition=lambda v: v.get("deckung") == "teilkasko"),
    Field("zahlungsweise", "Zahlungsweise", "Schutz", "choice", (
        Option("jährlich", "jährlich"), Option("halbjährlich", "halbjährlich"), Option("vierteljährlich", "vierteljährlich"),
    )),
    Field("zahlungsart", "Zahlungsart", "Schutz", "choice", (Option("sepa", "SEPA-Lastschrift"), Option("ueberweisung", "Überweisung"))),
)
FIELD_BY_KEY = {f.key: f for f in FIELDS}
SECTIONS = ("Fahrzeug", "Versicherungsnehmer", "Nutzung", "SF-Klasse", "Schutz")


def active_fields(values: dict) -> list[Field]:
    return [f for f in FIELDS if f.condition is None or f.condition(values)]


DAY_FIELDS = ("versicherungsbeginn", "geburtsdatum")
MONTH_FIELDS = ("erstzulassung", "halterzulassung")
KM_CHOICES = tuple(range(1000, 100000, 1000))


def km_label(value) -> str:
    return f"{int(value):,} km/Jahr".replace(",", ".")


def normalize_day(value: str) -> str:
    """'09112026' -> '09.11.2026', '9.11.2026' -> '09.11.2026'. Unvollstaendiges bleibt unveraendert
    (Validierung meldet es) - es wird nichts geraten (auch kein zweistelliges Jahr)."""
    v = value.strip()
    if re.fullmatch(r"\d{8}", v):
        return f"{v[:2]}.{v[2:4]}.{v[4:]}"
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", v)
    return f"{int(m.group(1)):02d}.{int(m.group(2)):02d}.{m.group(3)}" if m else v


def normalize_month(value: str) -> str:
    """'112026' / '11.2026' / '11/2026' / '1.2026' -> 'MM.JJJJ' (Monat und Jahr, kein Tag)."""
    v = value.strip()
    if re.fullmatch(r"\d{6}", v):
        return f"{v[:2]}.{v[2:]}"
    m = re.fullmatch(r"(\d{1,2})[./-](\d{4})", v)
    return f"{int(m.group(1)):02d}.{m.group(2)}" if m else v


def normalize_km(value: str) -> str:
    """'9000', '9.000', '9.000 km/Jahr' -> '9000'. '9' bleibt '9' (Rueckfrage "Meinst du 9.000?")."""
    return re.sub(r"[.\s]|km|/?jahr|pro|p\.?a\.?", "", value, flags=re.I)


def corrections(values: dict) -> dict[str, str]:
    """Konkrete Korrekturvorschlaege (nur mit Bestaetigung uebernommen)."""
    out = {}
    km = values.get("fahrleistung") or ""
    if km.isdigit() and 1 <= int(km) <= 99:
        out["fahrleistung"] = str(int(km) * 1000)
    return out


def normalize(raw: dict) -> dict:
    """Formular-/JSON-Werte bereinigen (nur bekannte Felder, keine Ergaenzungen)."""
    values = {}
    for f in FIELDS:
        value = raw.get(f.key)
        if f.kind == "multi":
            items = value if isinstance(value, list) else ([value] if isinstance(value, str) and value else [])
            allowed = {o.value for o in f.options}
            values[f.key] = [v for v in items if v in allowed]
            continue
        if not isinstance(value, str):
            continue
        value = " ".join(value.split())
        if f.key in ("hsn", "tsn"):
            value = value.upper().replace(" ", "")
        if f.key == "fahrleistung":
            value = normalize_km(value)
        if f.key in DAY_FIELDS:
            value = normalize_day(value)
        if f.key in MONTH_FIELDS:
            value = normalize_month(value)
        if f.key == "tsn":
            value = value[:3]
        if value:
            values[f.key] = value
    return values


def _date(value: str, fmt_day: bool) -> date | None:
    try:
        if fmt_day:
            d, m, y = (int(x) for x in value.split("."))
            return date(y, m, d)
        m, y = (int(x) for x in value.split("."))
        return date(y, m, 1)
    except ValueError:
        return None


def validate(values: dict, today: date) -> tuple[list[str], dict[str, str]]:
    """Gibt (fehlende Felder, Fehler je Feld) zurueck."""
    missing, errors = [], {}
    for f in active_fields(values):
        value = values.get(f.key)
        if f.kind == "multi":
            if not value:
                missing.append(f.key)
            continue
        if not value:
            missing.append(f.key)
            continue
        if f.key == "fahrleistung" and f.key in corrections(values):
            errors[f.key] = f"Meinst du {km_label(corrections(values)[f.key])}?"
            continue
        if f.pattern and not re.match(f.pattern, value):
            if f.key in DAY_FIELDS:
                errors[f.key] = "Bitte ein vollständiges Datum eingeben, z. B. 09.11.2026 oder 09112026."
            elif f.key in MONTH_FIELDS:
                errors[f.key] = "Bitte Monat und Jahr eingeben, z. B. 11.2026 oder 112026."
            else:
                errors[f.key] = f"Format: {f.placeholder}" if f.placeholder else "Ungültiges Format."
            continue
        if f.options and value not in {o.value for o in f.options}:
            errors[f.key] = "Bitte eine der angebotenen Antworten wählen."
    if "versicherungsbeginn" in values and "versicherungsbeginn" not in errors:
        begin = _date(values["versicherungsbeginn"], True)
        if begin is None:
            errors["versicherungsbeginn"] = "Dieses Datum gibt es nicht."
        elif begin < today:
            errors["versicherungsbeginn"] = "Der Versicherungsbeginn darf nicht in der Vergangenheit liegen."
    for key in ("erstzulassung", "halterzulassung"):
        if key in values and key not in errors:
            d = _date(values[key], False)
            if d is None or d > today:
                errors[key] = "Ungültiger Monat (nicht in der Zukunft)."
    if values.get("erstzulassung") and values.get("halterzulassung") and not errors.get("halterzulassung"):
        if _date(values["halterzulassung"], False) and _date(values["erstzulassung"], False) and _date(values["halterzulassung"], False) < _date(values["erstzulassung"], False):
            errors["halterzulassung"] = "Die Zulassung auf den Halter kann nicht vor der Erstzulassung liegen."
    if values.get("geburtsdatum") and "geburtsdatum" not in errors:
        born = _date(values["geburtsdatum"], True)
        if born is None:
            errors["geburtsdatum"] = "Dieses Datum gibt es nicht."
        elif not (today.replace(year=today.year - 100) < born <= today.replace(year=today.year - 17)):
            errors["geburtsdatum"] = "Ungültiges Geburtsdatum (Versicherungsnehmer mind. 17 Jahre)."
    if values.get("fahrleistung") and "fahrleistung" not in errors and not 1000 <= int(values["fahrleistung"]) <= 99000:
        errors["fahrleistung"] = "Bitte zwischen 1.000 und 99.000 km/Jahr wählen."
    return missing, errors


def suggestions(today: date) -> dict[str, str]:
    """Nur Vorschlaege - werden im Formular gekennzeichnet und nie ohne Bestaetigung verwendet."""
    first_next = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    return {"versicherungsbeginn": first_next.strftime("%d.%m.%Y")}


def from_customer(customer) -> dict[str, str]:
    """Vorhandene, fuer diesen Auftrag freigegebene Kundendaten (Benutzer hat Zugriff)."""
    values = {}
    if customer is None:
        return values
    if customer.date_of_birth:
        values["geburtsdatum"] = customer.date_of_birth.strftime("%d.%m.%Y")
    if customer.postal_code and re.match(r"^\d{5}$", customer.postal_code.strip()):
        values["plz"] = customer.postal_code.strip()
    return values


def label_of(key: str, value) -> str:
    f = FIELD_BY_KEY[key]
    if f.kind == "multi":
        return ", ".join((o.short or o.label) for o in f.options if o.value in (value or [])) or "–"
    for o in f.options:
        if o.value == value:
            return o.short or o.label
    if key == "fahrleistung" and value and str(value).isdigit():
        return km_label(value)
    return value or "–"


def summary(values: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    rows = []
    for section in SECTIONS:
        items = [(f.question, label_of(f.key, values.get(f.key))) for f in active_fields(values) if f.section == section]
        if section == "Schutz":
            items.append(("Zusatzbausteine (Schutzbrief, Rabattschutz u. a.)", "keine – nicht ausgewählt"))
        rows.append((section, items))
    return rows
