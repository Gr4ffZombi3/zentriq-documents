"""Wiederverwendbare Testdaten fuer die M12-Analyse-Engine-Tests (Layout-/Tabellen-Heuristik,
Validierung, Business-Regeln). Reine Python-Datenstrukturen, keine DB-/App-Abhaengigkeit."""

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
