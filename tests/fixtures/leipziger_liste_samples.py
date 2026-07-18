"""Wiederverwendbare Testdaten fuer die M12-Analyse-Engine-Tests (Layout-/Tabellen-Heuristik,
Validierung, Business-Regeln). Reine Python-Datenstrukturen, keine DB-/App-Abhaengigkeit."""

from datetime import date

from app.models.enums import Priority
from app.services.llm.schemas import ExtractedCustomer, LeipzigerListeRow

# SAMPLE_CLEAN: alle Felder vollstaendig und im Rohtext wortgetreu auffindbar - Erwartung:
# durchgehend hohe Konfidenz.
SAMPLE_CLEAN_RAW_TEXT = (
    "Leipziger Liste - Bestandsuebersicht\n"
    "Max Mustermann, Musterstr. 1, 10115 Berlin\n"
    "Fahrzeug: VW Golf, Kennzeichen: B-XX 1234\n"
    "Versicherung: Musterversicherung AG, Vertrag: V-998877\n"
    "Vermittlernummer: VM-4711, Sparte: KFZ, Beitrag: 123,45 EUR, Tarif: Komfort\n"
    "Status: storniert\n"
)
SAMPLE_CLEAN_ROW = LeipzigerListeRow(
    customer=ExtractedCustomer(name="Max Mustermann", address="Musterstr. 1", city="Berlin", postal_code="10115"),
    vehicle="VW Golf",
    license_plate="B-XX 1234",
    insurer="Musterversicherung AG",
    contract_number="V-998877",
    broker_number="VM-4711",
    product_line="KFZ",
    premium="123,45 EUR",
    tariff="Komfort",
    is_storno=True,
    priority=Priority.HIGH,
)

# SAMPLE_SPARSE: nur der Name ist bekannt, alle optionalen Felder fehlen - Erwartung:
# fehlende Felder werden ausgelassen bzw. als unsicher markiert, kein Crash.
SAMPLE_SPARSE_RAW_TEXT = "Erika Musterfrau\n"
SAMPLE_SPARSE_ROW = LeipzigerListeRow(customer=ExtractedCustomer(name="Erika Musterfrau"))

# SAMPLE_OCR_NOISE: die Postleitzahl ist OCR-verfaelscht (Buchstabe "O" statt Ziffer "0") und
# besteht das Format-Sanity-Check daher nicht, obwohl der Name im Rohtext auffindbar ist.
SAMPLE_OCR_NOISE_RAW_TEXT = "Hans Beispiel, PLZ 1O115 Berlin\n"
SAMPLE_OCR_NOISE_ROW = LeipzigerListeRow(
    customer=ExtractedCustomer(name="Hans Beispiel", postal_code="1O115"),
)

# Rohtext mit einer klar tabellarischen Struktur (konsistente Spaltenanzahl ueber mehrere
# Zeilen) - fuer app/services/analysis/tables.py.
SAMPLE_RAW_TEXT_WITH_TABLE = (
    "Leipziger Liste - Bestandsuebersicht\n"
    "Kunde              Fahrzeug        Kennzeichen\n"
    "Max Mustermann      VW Golf         B-XX 1234\n"
    "Erika Musterfrau    Audi A4         M-YY 5678\n"
    "Hans Beispiel       Opel Corsa      K-ZZ 9012\n"
    "\n"
    "Ende der Liste."
)

# Rohtext mit Fussnoten-Markern - fuer app/services/analysis/layout.py.
SAMPLE_RAW_TEXT_WITH_FOOTNOTES = (
    "Leipziger Liste\n"
    "Max Mustermann - VW Golf - B-XX 1234\n"
    "\n"
    "* Sonderkonditionen siehe Anhang\n"
    "1) Gilt nur fuer Bestandskunden\n"
    "Anm.: Preise koennen abweichen\n"
)

# Rohtext ohne jede tabellarische Struktur - Negativfall fuer beide Heuristiken.
SAMPLE_RAW_TEXT_PROSE = (
    "Sehr geehrte Damen und Herren,\n"
    "hiermit bestaetigen wir den Eingang Ihres Antrags vom 01.02.2026.\n"
    "Mit freundlichen Gruessen\n"
)

# Zwei "Seiten" (wie sie extract_text() heute mit "\n\n" verbindet) mit einem ueber die
# Seitengrenze hinweg fortgesetzten Kundeneintrag - fuer die Mehrseitige-Eintraege-Heuristik.
SAMPLE_MULTI_PAGE_TEXTS = [
    "Leipziger Liste - Seite 1\nMax Mustermann      VW Golf         B-XX 1234\n"
    "Erika Musterfrau    Audi A4         M-YY 5678\n",
    "Fortsetzung Erika Musterfrau: Vertrag Nr. V-998877, Praemie 45,00 EUR\n"
    "Hans Beispiel       Opel Corsa      K-ZZ 9012\n",
]

# M13: SAMPLE_EIGENE_LISTE_ROWS - eine "eigene Liste" mit genau einer Vermittlernummer
# (VM-4711) und allen fuenf PotentialCategory-Faellen. SAMPLE_GS_LISTE_ROWS - die
# vollstaendige Geschaeftsstellen-Liste mit MEHREREN Vermittlernummern (VM-4711, VM-5522,
# VM-6633), die "Anna Angebot" und "Peter Pruefen" aus der eigenen Liste unveraendert
# enthaelt (kein Vergleichs-Eintrag erwartet) sowie zusaetzliche, nur dort vorkommende
# Kunden anderer Vermittler - fuer detect_list_scope() (Kardinalitaet der
# Vermittlernummern) und den Eigene-vs-GS-Vergleich (M13.4).
SAMPLE_EIGENE_LISTE_ROWS = [
    LeipzigerListeRow(
        customer=ExtractedCustomer(name="Anna Angebot"),
        broker_number="VM-4711",
        product_line="KFZ",
        is_angebot=True,
    ),
    LeipzigerListeRow(
        customer=ExtractedCustomer(name="Peter Pruefen"),
        broker_number="VM-4711",
        product_line="Hausrat",
        has_antrag=True,
    ),
    LeipzigerListeRow(
        customer=ExtractedCustomer(name="Otto Offen"),
        broker_number="VM-4711",
        product_line="Leben",
    ),
    LeipzigerListeRow(
        customer=ExtractedCustomer(name="Sabine Storno"),
        broker_number="VM-4711",
        product_line="KFZ",
        is_storno=True,
    ),
    LeipzigerListeRow(
        customer=ExtractedCustomer(name="Klaus Kunde"),
        broker_number="VM-4711",
        product_line="Hausrat",
        contract_start_date=date(2026, 1, 15),
    ),
]

SAMPLE_GS_LISTE_ROWS = [
    # Unveraendert aus der eigenen Liste - kein Vergleichs-Eintrag erwartet.
    LeipzigerListeRow(
        customer=ExtractedCustomer(name="Anna Angebot"),
        broker_number="VM-4711",
        product_line="KFZ",
        is_angebot=True,
    ),
    LeipzigerListeRow(
        customer=ExtractedCustomer(name="Peter Pruefen"),
        broker_number="VM-4711",
        product_line="Hausrat",
        has_antrag=True,
    ),
    # Nur in der GS-Liste - andere Vermittler.
    LeipzigerListeRow(
        customer=ExtractedCustomer(name="Frank Fremdvermittler"),
        broker_number="VM-5522",
        product_line="KFZ",
        is_angebot=True,
    ),
    LeipzigerListeRow(
        customer=ExtractedCustomer(name="Gisela Geschaeftsstelle"),
        broker_number="VM-6633",
        contract_start_date=date(2026, 2, 1),
    ),
]
