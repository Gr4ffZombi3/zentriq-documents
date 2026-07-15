"""Wiederverwendbare Testdaten fuer die M12-Analyse-Engine-Tests (Layout-/Tabellen-Heuristik,
Validierung, Business-Regeln). Reine Python-Datenstrukturen, keine DB-/App-Abhaengigkeit."""

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
