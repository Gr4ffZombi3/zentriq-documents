"""Sichtbare Neuerungen je Release fuer "Was ist neu?".

Bei JEDEM Release mit sichtbaren Aenderungen hier einen Eintrag ergaenzen (neueste oben):
- date: Veroeffentlichungstag (ab dann sichtbar)
- title / text: wenige verstaendliche Saetze aus Sicht der Nutzer - keine Technik, keine
  Dateinamen, keine internen Bezeichnungen, keine Kundendaten
- audience: "alle", "admin" (nur Buero-Admins) oder "mitarbeiter"

Buero-Admins koennen zusaetzlich eigene Eintraege fuer ihr Buero pflegen (Was ist neu? ->
Eintrag hinzufuegen, Tabelle office_news)."""

from datetime import date

RELEASE_NOTES = (
    {
        "date": date(2026, 11, 10),
        "title": "Tabs, Schnellsuche und Seitenfenster",
        "text": "Kundenakten, Kalender und Kfz-Angebote lassen sich als Tabs oben parallel offen halten. "
        "Mit Strg+K (Mac: Cmd+K) oder dem Lupen-Knopf finden Sie Kunden und Seiten sofort. "
        "Aus Kalender und Leipziger Liste öffnet sich die Kundenakte rechts daneben – Ihre Liste bleibt, wie sie war.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 10),
        "title": "Kfz-Angebot später fortsetzen",
        "text": "Angefangene Kfz-Angaben werden automatisch zwischengespeichert. Nach dem Neuladen oder an einem "
        "anderen Gerät können Sie den Entwurf fortsetzen oder verwerfen. Ein Entwurf startet keine Berechnung.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 10),
        "title": "Gespeicherte Ansichten und eigene Startseite",
        "text": "Filter der Leipziger Liste lassen sich unter einem Namen speichern und mit einem Klick wieder öffnen, "
        "zum Beispiel „ohne Datum, ohne Storno“. Unter Mein Konto wählen Sie, mit welcher Seite Zentriq startet.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 10),
        "title": "Kundendaten direkt in der Akte ändern",
        "text": "Festnetz, Privattelefon, E-Mail und Adresse lassen sich direkt am Feld bearbeiten. "
        "Am Feld steht sofort, ob gespeichert wurde oder eine Angabe korrigiert werden muss.",
        "audience": "admin",
    },
    {
        "date": date(2026, 11, 10),
        "title": "Genauerer Fortschritt beim PDF-Import",
        "text": "Beim Einlesen einer Liste sehen Sie, welche Seite gerade gelesen wird. Bei einem Fehler steht dabei, "
        "ob die Datei angekommen ist und welcher Schritt wiederholt werden kann.",
        "audience": "admin",
    },
    {
        "date": date(2026, 11, 10),
        "title": "E-Mail-Entwürfe ohne Hintergrund kopieren",
        "text": "Markierter Text aus einem E-Mail-Entwurf wird als reiner Text kopiert – in Outlook erscheint kein weißer Hintergrund mehr.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 9),
        "title": "Wochenplan in der Zeiterfassung",
        "text": "Für jeden Mitarbeiter kann ein eigener Wochenplan ab einem Stichtag hinterlegt werden. Soll- und Überstunden richten sich danach.",
        "audience": "admin",
    },
    {
        "date": date(2026, 11, 9),
        "title": "Festnetz und Privattelefon",
        "text": "Bei Kunden werden Festnetz- und Privatnummer getrennt geführt.",
        "audience": "alle",
    },
)
