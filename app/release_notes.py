"""Sichtbare Neuerungen je Release fuer "Was ist neu?".

Bei JEDEM Release mit sichtbaren Aenderungen hier einen Eintrag ergaenzen (neueste oben):
- date: Veroeffentlichungstag (ab dann sichtbar)
- title / text: wenige verstaendliche Saetze aus Sicht der Nutzer - keine Technik, keine
  Dateinamen, keine internen Bezeichnungen, keine Kundendaten
- audience: "alle", "admin" (nur Buero-Admins) oder "mitarbeiter"
- feature / hint (optional): markiert die geaenderte Funktion mit einem kleinen "Neu".
  feature = Schluessel, den das Template per new_badge("...") an die Funktion setzt;
  hint = ein Satz, der beim Antippen erscheint. Danach verschwindet die Markierung fuer diesen
  Benutzer (laengstens 30 Tage nach dem Datum sichtbar).

Buero-Admins koennen zusaetzlich eigene Eintraege fuer ihr Buero pflegen (Was ist neu? ->
Eintrag hinzufuegen, Tabelle office_news)."""

from datetime import date

RELEASE_NOTES = (
    {
        "date": date(2026, 11, 12),
        "title": "Kunde anlegen mit eingefügten Kundendaten",
        "text": "Unter „Kunden › Kunde anlegen“ können Sie einen Adressblock oder einen Abschnitt aus einer E-Mail einfügen. "
        "Zentriq trägt Name, Adresse, Telefon und E-Mail sofort in die Maske ein – alles bleibt vor dem Speichern änderbar. "
        "Gibt es den Kunden vermutlich schon, sehen Sie die passende Akte mit „Akte öffnen“.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 12),
        "title": "Leipziger Liste erkennt bestehende Kunden",
        "text": "Beim Import der Leipziger Liste ordnet Zentriq Verträge zuerst über die Vertragsnummer der vorhandenen Kundenakte zu. "
        "Von Hand erfasste Telefonnummern, Notizen und offene Rückrufe bleiben dabei erhalten. Unklare Fälle erscheinen in der "
        "Akte unter „Mögliche Dublette“ zur manuellen Zuordnung.",
        "audience": "admin",
    },
    {
        "date": date(2026, 11, 12),
        "title": "Kunden anlegen ohne feste Formulierung",
        "text": "„Lege Kunde Max Mustermann an“, „Neuer Kunde Max Mustermann“, „Nimm Max Mustermann als Kunden auf“ oder "
        "„Max Mustermann neu anlegen“ – der Assistent legt den Kunden jetzt sofort an, auch bei kleinen Tippfehlern. "
        "Gibt es den Namen schon, fragt er, ob Sie die vorhandene Akte öffnen oder trotzdem neu anlegen möchten. "
        "Ein reiner Name ohne Treffer bietet „neu anlegen“ an, ein Terminwunsch legt nie einen Kunden an.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 12),
        "title": "Teamchat im Büro",
        "text": "Unten rechts neben der Zeiterfassung gibt es jetzt den Teamchat: Über „Teamchat“ wählen Sie einen Kollegen "
        "aus Ihrem Büro und schreiben direkt im Browser – auch während Sie im Kalender, in einer Kundenakte oder im "
        "Assistenten arbeiten. Neue Nachrichten erscheinen unten mit dem Namen und einem Zähler. Ein Klick klappt den "
        "Chat auf; er lässt sich vergrößern und wieder minimieren und bleibt beim Seitenwechsel so, wie Sie ihn verlassen haben.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 12),
        "title": "Anruferkennung mit Placetel",
        "text": "Zentriq kann Anrufe jetzt auch direkt von Placetel gemeldet bekommen. Klingelt eine zugeordnete Nummer, "
        "zeigt Zentriq oben rechts, wer anruft – das Sprachmenü und die Rufweiterleitung bleiben dabei unverändert.",
        "audience": "admin",
    },
    {
        "date": date(2026, 11, 12),
        "title": "Termine und Notizen sauber getrennt",
        "text": "„Mach für diesen Kunden Donnerstag um 15 Uhr einen Termin“ trägt jetzt nur den Termin ein – ohne zusätzliche "
        "Notiz, auch wenn eine Angabe fehlt oder die Zeit belegt ist; Zentriq fragt dann gezielt nach. Eine Notiz entsteht, "
        "wenn Sie sie ausdrücklich verlangen, z. B. „Notiere: …“. Nennen Sie einen anderen Kunden als den ausgewählten, "
        "fragt Zentriq vor dem Speichern nach.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 12),
        "title": "Kundenakten für Mitarbeiter",
        "text": "Mitarbeiter können die Kundenakten ihres Büros jetzt aus der Kundensuche und aus dem Assistenten öffnen und "
        "lesen – mit Notizen und Memos. Bearbeiten und Löschen bleiben wie bisher geregelt.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 12),
        "title": "Kundenkarte aufgeräumt, Notizen löschen",
        "text": "Die Kundenkarte im Assistenten zeigt die Aktionen jetzt direkt unter dem Namen. Die Notizen haben einen eigenen "
        "Bereich: Die neueste ist sofort sichtbar, alle weiteren öffnen Sie mit „Alle Notizen anzeigen“. Die Leipziger Liste "
        "finden Sie unter „Kundendetails“. Über das Drei-Punkte-Menü an einer Notiz können Sie diese nach einer Rückfrage "
        "löschen – Büro-Admins jede Notiz, Mitarbeiter ihre eigenen.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 12),
        "title": "Kundensuche im Assistenten sofort",
        "text": "Ein Name, eine Telefonnummer oder eine VN-Nummer im Assistenten zeigt jetzt sofort die Kundenkarte – auch "
        "wenn kein Kunde gefunden wird, kommt die Antwort direkt statt nach langem Warten auf die KI. Bei Tippfehlern "
        "schlägt Zentriq passende Kunden vor. Ist gerade eine Kundenkarte geöffnet, wird ein anderer Name gesucht und "
        "nicht mehr als Notiz gespeichert. Fragt Zentriq bei einem Termin nach dem Kunden, genügt jetzt der Name.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 12),
        "title": "KI auf dem eigenen Rechner",
        "text": "Fotos, Scans, Dokumente und lange Anfragen kann jetzt ein KI-Modell auf Ihrem eigenen Rechner bearbeiten – "
        "kurze Fragen beantwortet weiterhin das eingestellte Modell. Unter „Verwaltung › Mein Rechner“ sehen Sie, ob Ihr "
        "Rechner erreichbar ist und welche Aufträge warten. Im Assistenten erzwingt „Auf meinem Rechner bearbeiten“ die "
        "lokale Bearbeitung. Ist der Rechner aus, warten die Aufträge und laufen später automatisch weiter.",
        "audience": "admin",
    },
    {
        "date": date(2026, 11, 12),
        "title": "Kfz-Vergleich aus dem Versicherungsschein",
        "text": "Unter „Kfz-Angebot (HUK)“ können Sie den Versicherungsschein eines Kunden fotografieren oder als PDF hochladen. "
        "Zentriq liest die Tarifangaben aus, fragt Fehlendes gesammelt ab und zeigt nach „Bei HUK berechnen“ den bisherigen "
        "und den HUK-Jahresbeitrag nebeneinander – mit Differenz und wichtigen Abweichungen. Wenn mehrere Personen fahren, "
        "fragt die Kfz-Berechnung jetzt zusätzlich, ob jemand 70 Jahre oder älter ist.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Test: Anruferkennung am Telefon",
        "text": "Für eine erste Test-Durchwahl zeigt Zentriq beim Klingeln oben rechts, wer anruft – mit „Kundenakte öffnen“, "
        "einer Auswahl bei mehreren Treffern oder „Rückrufnotiz anlegen“, wenn die Nummer noch keiner Akte zugeordnet ist. "
        "Die gerade geöffnete Akte bleibt dabei offen. Die Anrufliste wird nach 7 Tagen automatisch gelöscht.",
        "audience": "admin",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Abrechnungen: automatische Mitarbeiterübersicht",
        "text": "Nach dem Hochladen der HUK-Abrechnung erscheint sofort je Mitarbeiter die Zahl der Neuabschlüsse, die Anzahl je Sparte "
        "und die Vergütung ohne 10-€-Positionen. Die Zuordnung erfolgt über die VM-Nummer – Zeilen müssen nicht mehr einzeln geprüft werden.",
        "audience": "admin",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Assistent auf dem Handy aufgeräumt",
        "text": "Auf dem Handy sitzt die Zeiterfassung jetzt oben in der Kopfzeile und verdeckt nicht mehr den "
        "Senden-Knopf. „Offene Rückrufe“ und „Nächste Termine“ stehen direkt über dem Eingabefeld; beim Tippen "
        "bleibt nur das Eingabefeld sichtbar. Auch am Computer bleibt das Eingabefeld bei geöffneten Tabs "
        "vollständig sichtbar.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Willkommensseite für den ersten Besuch",
        "text": "Wer ZentriqAI zum ersten Mal am Computer öffnet, sieht eine kurze Willkommensseite. Neue Kolleginnen "
        "und Kollegen, für die Sie bereits ein Konto angelegt haben, gelangen dort über „Bereits ein Konto? Anmelden“ "
        "direkt zur Anmeldung – ab dann öffnet sich in diesem Browser immer gleich die Anmeldung.",
        "audience": "admin",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Sehen, wer gerade in der Kundenakte ist",
        "text": "In der Kundenakte und in der Kundenkarte im Assistenten sehen Sie, wer den Kunden gerade ebenfalls "
        "geöffnet hat und wer gerade die Kundendaten bearbeitet. Ändert jemand etwas, erscheint ein Hinweis mit "
        "„Aktualisieren“ – so überschreibt niemand versehentlich die Eingabe eines Kollegen.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Aufgeräumte Seitenleiste",
        "text": "Die linke Leiste klappt jetzt ohne Ruckeln ein und aus. „Werkzeuge“ und „Übersicht“ stehen nicht mehr "
        "in der Leiste – Kfz-Angebot, Anonymisieren & Co. finden Sie weiter über die Schnellsuche (Strg+K).",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "E-Mail-Entwürfe ohne Kundenakte",
        "text": "„Schreib eine E-Mail-Antwort: …“ ergibt im Assistenten sofort einen kopierbaren Entwurf mit Betreff, "
        "Anrede und Ihrer Bürosignatur – auch ohne Kundenakte. Ein genannter Name wird für die Anrede übernommen, "
        "sonst beginnt die Mail mit „Guten Tag,“. Gesendet wird nichts.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Weniger „Seite veraltet“, nichts geht doppelt",
        "text": "Seiten, die lange offen waren, speichern jetzt ohne die Meldung „Seite veraltet“. Bricht die Verbindung "
        "kurz ab, zeigt Zentriq „Verbindung wird wiederhergestellt“ – und erneutes Speichern legt nichts doppelt an. "
        "Ist die Anmeldung abgelaufen, bleibt die Aktion gesichert: nach dem erneuten Anmelden genügt „Jetzt speichern“.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Vorschau beim Tippen im Assistenten",
        "text": "Schon beim Tippen zeigt der Assistent, was er verstanden hat: passende Kunden, die Notiz zur "
        "Kundenakte oder einen Termin mit Kunde, Datum, Uhrzeit, Mitarbeiter und ob die Zeit frei ist. Ein Klick auf "
        "ein Feld ändert es, zum Beispiel auf eine freie Uhrzeit. Gespeichert wird erst mit „Termin speichern“ bzw. Enter.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Notiz oder Termin ohne Kundenakte",
        "text": "Findet die Suche keinen Kunden, können Sie direkt einen Vorgang ohne Kundenakte erfassen – mit Name, "
        "falls bekannt der Versicherungsnehmernummer, einer Notiz oder einem Termin im Bürokalender. Taucht die Nummer "
        "später in einer Leipziger Liste auf, landen Notiz und Termin automatisch in der passenden Kundenakte.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Feierabendgruß und Abmelden mit Zeiterfassung",
        "text": "Nach dem Ausstempeln zeigt Zentriq kurz, wie viel Zeit heute erfasst ist. Wer sich bei laufender "
        "Zeiterfassung abmeldet, kann wählen: ausstempeln und abmelden oder die Zeit weiterlaufen lassen.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Listen kompakt oder geräumig",
        "text": "Listen wie die Leipziger Liste lassen sich kompakt oder geräumig anzeigen. "
        "Die Wahl gilt nur für Sie und bleibt auf allen Geräten erhalten.",
        "audience": "alle",
        "feature": "listen-dichte",
        "hint": "Hier wählen Sie, ob Listen kompakt oder geräumig angezeigt werden – die Einstellung gilt nur für Sie.",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Termin buchen in Daumenreichweite",
        "text": "Auf dem Handy liegt „Termin buchen“ im Kalender unten rechts. Ist ein Tag noch leer, "
        "führt Sie der Kalender direkt zum ersten Termin.",
        "audience": "alle",
        "feature": "termin-buchen",
        "hint": "Neue Termine buchen Sie hier – auf dem Handy liegt der Knopf unten rechts in Daumenreichweite.",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Rückmeldung direkt am Knopf",
        "text": "Nach dem Kopieren zeigt der Knopf kurz „Kopiert ✓“, gespeicherte Felder zeigen „Gespeichert“. "
        "Klappt etwas nicht, bleiben Ihre Eingaben stehen und Sie sehen, was zu tun ist.",
        "audience": "alle",
    },
    {
        "date": date(2026, 11, 11),
        "title": "Persönliche Begrüßung am Morgen",
        "text": "Beim ersten Login des Tages begrüßt Zentriq Sie mit Namen und zeigt bis zu drei Neuerungen, die "
        "für Sie wichtig sind – einmal am Tag, auch wenn Sie mehrere Geräte nutzen. "
        "Neue Funktionen tragen außerdem ein kleines „Neu“ – antippen erklärt, was sich geändert hat.",
        "audience": "alle",
    },
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
