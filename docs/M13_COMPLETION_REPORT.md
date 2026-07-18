# M13 – Spezialisierte Analyse der Leipziger Liste: Abschlussbericht

Meilenstein abgeschlossen über 8 Commits (`M13.1`–`M13.7`, `M13.9`; `M13.8` – erschöpfende
Explainability-Tests – wurde bereits in `M13.2`s Testabdeckung mit erledigt, `M13.10`
Regressionslauf und `M13.11` dieser Bericht sind Verifikations-Schritte ohne eigenen
Produktivcode), strikt additiv: keine bestehende Route, kein
bestehendes Feld gelöscht, keine bestehende Business-Logik verändert. Kernkurswechsel gegenüber
M11/M12: **keine eigenen Entscheidungen mehr** – keine Prognosen, keine künstlichen Prioritäten,
keine "Verkaufs-KI". Stattdessen eine rein deterministische, vollständig erklärbare
Klassifikation der Leipziger Liste, ausschließlich auf Basis tatsächlich im Dokument vorhandener
Daten. Zwei neue eigenständige Seiten (`/potenziale`, `/potenziale/vergleich`), die bestehenden
M11/M12-Seiten (Tagescockpit, Dokumente, Aufgaben) bleiben unangetastet. Testsuite:
**190 → 245 Tests**, alle grün, `ruff check` durchgehend sauber.

## 1. Architekturentscheidung: Klassifikation statt Vorhersage

`app/services/analysis/potential_classification.py::classify_row()` – eine reine, zustandslose
Funktion (kein DB-Zugriff, kein GPT-Aufruf), die jede Kundenzeile in genau eine von fünf
`PotentialCategory`-Werten einordnet, in dieser festen Prioritätsreihenfolge:

```
is_storno=True                        -> STORNIERT
contract_start_date vorhanden         -> ABGESCHLOSSEN
has_antrag=True (kein Beginn)         -> PRUEFEN
is_angebot=True (kein Beginn/Antrag)  -> NUR_ANGEBOT
sonst                                 -> OFFENER_VORGANG
```

Storno wird bewusst **vor** Abschluss geprüft, obwohl ein stornierter Vertrag durchaus ein
`contract_start_date` haben kann – laut Briefing soll ein Storno immer separat sichtbar bleiben,
nicht in der Abgeschlossen-Kategorie verschwinden. Kein Feld dieser Kette ist geraten oder
geschätzt: `is_storno`, `has_antrag` und `is_angebot` sind bereits bestehende bzw. (im Fall von
`has_antrag`) neu extrahierte Boolesche Flags, `contract_start_date` ist ein neu extrahiertes
Datumsfeld – die Klassifikation trifft keine Aussage, die nicht direkt aus dem Dokument ablesbar
wäre.

## 2. Neue Extraktionsfelder

Additiv auf `LeipzigerListeRow` (nicht auf `DocumentExtraction` – analog zum M12-Muster für
`broker_number`/`product_line`, bewusst zeilenbasiert statt auf Dokumentebene aggregiert):

| Feld | Bedeutung |
|---|---|
| `contract_start_date` | Versicherungsbeginn – das alleinige Abschlusskriterium laut Briefing |
| `has_antrag` | Antrag wurde gestellt (unabhängig davon, ob bereits ein Beginn-Datum vorliegt) |

`app/services/analysis/validation.py` erweitert um `has_antrag` in `BOOLEAN_FLAG_KEYWORDS`
(Schlüsselwort "antrag"), damit auch dieses neue Flag über die bestehende
Plausibilitätsprüfung eine Konfidenz bekommt. `contract_start_date` fließt bewusst **nicht** in
die zeilenweise Fuzzy-Match-Konfidenzformel ein – Datumsfelder passen nicht zum
String-Ähnlichkeits-Ansatz, der für Text-/Namensfelder entwickelt wurde.

## 3. Erklärbarkeit – feste Begründungstexte

`explain_category(row, category)` – jede Kategorie hat eine feste Textvorlage, wörtlich aus dem
Briefing übernommen, ausschließlich mit tatsächlich extrahierten Werten befüllt, kein GPT-Aufruf:

| Kategorie | Begründungstext |
|---|---|
| NUR_ANGEBOT | "Kunde erscheint in der Liste als Angebot. Es wurde kein Versicherungsbeginn gefunden." |
| PRUEFEN | "Antrag vorhanden, aber kein Beginn-Datum erkannt." |
| OFFENER_VORGANG | "Datensatz enthält keinen Hinweis auf einen Abschluss." |
| STORNIERT | "Datensatz wurde als storniert oder gekündigt erkannt." |
| ABGESCHLOSSEN | "Versicherungsbeginn am {datum} erkannt. Datensatz gilt als abgeschlossen." |

Jeder in `/potenziale` angezeigte Datensatz zeigt diese Begründung direkt neben dem
Kategorie-Badge – der Vermittler sieht immer, *warum* ein Kunde in einer Kategorie gelandet ist,
nie eine unbegründete Einstufung.

## 4. Eigene Liste vs. GS-Liste

`Document.list_scope` (neues Enum `ListScope`: `OWN`, `GESCHAEFTSSTELLE`), primär **automatisch**
erkannt über `app/services/analysis/list_scope_detection.py::detect_list_scope()`: Anzahl
unterschiedlicher `broker_number`-Werte über alle Zeilen des Dokuments – 0 oder 1 eindeutiger Wert
→ `OWN`, ≥2 → `GESCHAEFTSSTELLE`. Da diese Heuristik bei OCR-Rauschen oder einem
Ein-Personen-Büro danebenliegen kann, bekam das Upload-Formular zusätzlich eine manuelle
Auswahl ("Automatisch erkennen" / "Eigene Liste" / "GS-Liste") – echtes Sowohl-als-auch statt
nur automatisch oder nur manuell. Wird beim Upload ein Wert gesetzt, überspringt die Pipeline die
automatische Erkennung für dieses Dokument vollständig.

## 5. Vergleich Eigene Liste ↔ GS-Liste

Statt eine zweite Vergleichs-Engine zu bauen, wurde die bestehende, seit M11 produktiv laufende
`compare_leipziger_liste()` additiv erweitert: zwei neue optionale Parameter
(`previous_document`, `comparison_kind`), Default-Werte lassen den einzigen bestehenden Aufruf in
`document_tasks.py` sowie alle bestehenden Tests unverändert. Neues Enum `ComparisonKind`
(`TEMPORAL` – bisheriges zeitbasiertes Verhalten, `OWN_VS_GS` – neu) auf `ListComparison`.

**Die kritischste Änderung dieses Meilensteins**: die Idempotenz-Löschung bei Reprocessing wurde
von `filter_by(document_id=...)` auf `filter_by(document_id=..., comparison_kind=...)`
umgestellt. Ohne diese Präzisierung hätte ein neu laufender GS-Vergleich den zeitbasierten
Vergleich desselben Dokuments gelöscht (oder umgekehrt) – dediziert abgesichert durch
`test_temporal_and_own_vs_gs_comparisons_coexist_for_same_document` und
`test_reprocessing_does_not_duplicate_or_wipe_either_comparison_kind`.

Neue Funktion `find_paired_gs_or_own_document(document)` sucht das jeweils **neueste** Dokument
des entgegengesetzten `list_scope` desselben Tenants (bewusst ohne zeitliche Einschränkung, anders
als der zeitbasierte Vergleich – eine GS-Liste kann ein Gegenstück finden, das vor **oder** nach
ihr hochgeladen wurde). Die Pipeline ruft bei Fund zusätzlich `compare_leipziger_liste(document,
previous_document=paired, comparison_kind=OWN_VS_GS)` auf, neben dem immer laufenden
zeitbasierten Vergleich – ein Dokument kann also beide Vergleichsarten gleichzeitig haben.

## 6. Neue Abfrageschicht

`app/services/analysis/leipziger_liste_view.py` – dediziert statt einer Erweiterung von
`FilterSpec`/`search_documents()`, da Leipziger-Liste-Daten inhärent pro Zeile/pro Kunde sind,
nicht pro Dokument:

- `get_potential_records(category, include_closed, product_line, broker_number, date_from,
  date_to, document_id, list_scope)` – ein Ergebnis-Dict pro Kundenzeile, `include_closed=False`
  per Default (erledigte Datensätze stehen wie gefordert standardmäßig nicht im Fokus, sind aber
  über `include_closed=True` weiterhin abrufbar; eine explizite Kategorie-Filterung auf
  `ABGESCHLOSSEN` übersteuert den Default).
- `get_analysis_summary(document=None)` – die 7 Kennzahlen aus "Analyseergebnis": Datensätze,
  Abschlüsse, Angebote, offene Vorgänge, Stornos, Vorgänge ohne Beginn, Vorgänge ohne Antrag.

Beide iterieren tenant-gescopte `DocumentCustomer`-Queries (automatisch über
`TenantScopedMixin`), explodieren `row_data` und wenden `classify_row`/`explain_category` an.

## 7. Neue Seiten

- **`GET /potenziale`** – Zusammenfassung (7 Kennzahlen), GET-Query-Filter (Kategorie, Sparte,
  Vermittler, Datumsbereich, Storno/Abgeschlossen ein-/ausblendbar), je Eintrag Kunde, Produkt,
  Sparte, Kategorie-Badge, Angebotsdatum und die deterministische Begründung, mit Link zurück zum
  Quelldokument. Sidebar-Eintrag "Potenziale" (Icon `target`, bereits vorhanden/ungenutzt).
- **`GET /potenziale/vergleich`** – Gegenüberstellung Eigene Liste ↔ GS-Liste, liest
  `ListComparisonEntry` mit `comparison_kind=OWN_VS_GS`; bei mehreren Vergleichen über die Zeit
  per Dropdown wählbar (`document_id`-Query-Parameter), sonst automatisch der jüngste.
  Sidebar-Eintrag "Listenvergleich" (Icon `git-compare`, bereits vorhanden/ungenutzt).

Beide Seiten live im Browser verifiziert: Login-Pflicht, Filter im gerenderten Output sichtbar,
Dark Mode, Mobile-Viewport ohne horizontales Overflow, synthetisches Eigene/GS-Paar korrekt
dargestellt.

## 8. Neue Datenbank-Elemente (zwei Migrationen)

**Neue Enums**: `PotentialCategory` (5 Werte), `ListScope` (2 Werte), `ComparisonKind` (2 Werte).
**Neue Spalten**: `documents.list_scope` (nullable, einfache additive Migration
`84dc72643881`), `list_comparisons.comparison_kind` (NOT NULL, Default `TEMPORAL`) – Migration
`be3c6c699541` manuell im 3-Schritt-Muster geschrieben (nullable Spalte hinzufügen → bestehende
Zeilen per SQL auf `'TEMPORAL'` befüllen → auf NOT NULL setzen), da das autogenerierte
Ein-Schritt-NOT-NULL-Add gegen eine bereits befüllte `list_comparisons`-Tabelle fehlgeschlagen
wäre. Beide Migrationen up/down/up gegen eine temporäre SQLite-Datenbank verifiziert, die
comparison_kind-Migration zusätzlich mit einer manuell vorab eingefügten Zeile, um die
Backfill-Logik gegen echte Bestandsdaten zu beweisen.

## 9. Neue Services

| Datei | Funktionen |
|---|---|
| `app/services/analysis/potential_classification.py` | `classify_row`, `explain_category` |
| `app/services/analysis/list_scope_detection.py` | `detect_list_scope` |
| `app/services/analysis/leipziger_liste_view.py` | `get_potential_records`, `get_analysis_summary` |

Additive Erweiterungen bestehender Dateien: `app/services/list_comparison.py`
(`comparison_kind`-Parameter, `find_paired_gs_or_own_document`), `app/services/analysis/
validation.py` (`has_antrag`-Schlüsselwort), `app/services/llm/schemas.py`/`extraction.py`
(neue Zeilenfelder + Prompt-Ergänzung), `app/services/documents.py` (`list_scope`-Parameter an
`create_document()`), `app/tasks/document_tasks.py` (list_scope-Erkennung + Own-vs-GS-Vergleich
in der Pipeline), `app/blueprints/upload/routes.py` (manuelle `list_scope`-Auswahl).

## 10. Neue Tests

190 ursprüngliche Tests bleiben unverändert grün. 8 Testdateien neu, 2 erweitert:

- `test_potential_classification.py` – vollständige Regeltabelle inkl. Storno-Vorrang und
  Antrag-vs-Angebot-Tie-Break, Begründungstexte exakt gegen die Briefing-Beispiele
- `test_list_scope_detection.py` – Kardinalitäts-Fälle (0/1/mehrere Broker-Nummern)
- `test_upload.py` (erweitert) – manuelle Auswahl übersteuert Erkennung, leere Auswahl belässt
  `list_scope` unverändert
- `test_own_vs_gs_comparison.py` – Koexistenz von TEMPORAL und OWN_VS_GS für dasselbe Dokument
  (kritischster Test dieses Meilensteins), Reprocessing dupliziert/löscht keine Vergleichsart
  fälschlich
- `test_leipziger_liste_view.py` – jeder Filter einzeln und kombiniert, `include_closed`-Umschaltung,
  Tenant-Isolation, Kennzahlen-Berechnung
- `test_potenziale_routes.py`, `test_potenziale_vergleich_routes.py` – Route-Rendering,
  Login-Pflicht, Filter im gerenderten Output, Tenant-Isolation
- `test_m13_sample_fixtures_e2e.py` – die größeren `SAMPLE_EIGENE_LISTE_ROWS`/
  `SAMPLE_GS_LISTE_ROWS`-Fixtures einmal vollständig durch `process_document()` verifiziert
- `test_leipziger_liste.py` (erweitert) – Default-Werte und Row-Data-Serialisierung der 2 neuen Felder

**Ergebnis: 190 → 245 bestehende Tests, alle grün.**

## 11. Grenzen (bewusst, offen dokumentiert statt versteckt)

- **Scope-Reduktion bei den Kategorien**: Das Briefing nannte acht Beispielkategorien
  ("Nur Angebot", "Antrag fehlt", "Beginn fehlt", "Unterlagen fehlen", "Bearbeitung offen",
  "Rückfrage erforderlich", "Storniert", "Abgeschlossen"). Drei davon – "Unterlagen fehlen",
  "Bearbeitung offen", "Rückfrage erforderlich" – haben **kein** verlässliches, bereits
  extrahierbares Signal in der aktuellen Datenlage (kein Feld erfasst z.B. "wartet auf
  Gesundheitsfragebogen"). Statt eine unzuverlässige Freitext-Heuristik zu erfinden, die
  Datensätze fälschlich in eine dieser drei Kategorien einordnen könnte, fasst
  `classify_row()` sie bewusst in den ehrlichen Sammelbegriff `OFFENER_VORGANG` – gleiches
  Vorsichtsprinzip wie bereits bei M11s "Termin notwendig"-Entscheidung.
- **`detect_list_scope()` ist eine reine Kardinalitäts-Heuristik** – ein Ein-Personen-Büro mit
  einer einzigen Vermittlernummer sieht identisch aus wie eine unvollständig extrahierte
  GS-Liste, bei der die Vermittlernummer bei den meisten Zeilen fehlte. Die manuelle
  Übersteuerung im Upload-Formular ist die bewusste Absicherung gegen diesen Fall, nicht ein
  Versuch, die Heuristik selbst robuster zu machen.
- **`find_paired_gs_or_own_document()` paart ohne zeitliche Nähe** – bei mehreren Own- oder
  GS-Dokumenten über Monate hinweg wird immer nur das jeweils neueste Gegenstück verglichen,
  nicht das zeitlich naheliegendste. Für die erwartete Nutzung (eine aktuelle eigene Liste
  gegen eine aktuelle GS-Liste) ist das die richtige Wahl, kann bei sehr unregelmäßigen
  Upload-Rhythmen aber zu einem Vergleich gegen ein älteres Gegenstück führen als intuitiv
  erwartet.
- **Kein KI-Bericht-Text für die Potenziale-Seite** – anders als M12s `build_analysis_report()`
  gibt es hier bewusst keine (auch keine optionale) GPT-Formulierung der Zusammenfassung; jede
  Zahl und jeder Begründungstext ist ein direktes, deterministisches Template-Ergebnis, exakt
  wie vom Briefing gefordert ("keine eigenen Entscheidungen").

## 12. Verbesserungsmöglichkeiten (zukünftige Meilensteine)

- Falls sich in der Praxis ein verlässliches Signal für "Unterlagen fehlen"/"Bearbeitung
  offen"/"Rückfrage erforderlich" findet (z.B. ein neues, explizites Extraktionsfeld), können
  diese drei Kategorien aus `OFFENER_VORGANG` herausgelöst werden, ohne die bestehende
  Prioritätslogik zu verändern.
- Zeitlich näherungsweise Paarung in `find_paired_gs_or_own_document()`, falls mehrere
  Own-/GS-Dokumente parallel aktiv gepflegt werden.
- Export/Druckansicht der `/potenziale`-Liste für Vermittler, die außerhalb der Anwendung
  planen.

## 13. Qualität

- `manage.ps1 check` (ruff + volle Testsuite) vor jedem der 9 Commits grün.
- `git diff` von `2a1bcb6` (letzter M12-Commit) bis `HEAD` bestätigt: ausschließlich die in
  diesem Bericht genannten Dateien geändert, keine unbeteiligte bestehende Datei berührt.
- Beide neuen Migrationen up/down/up gegen eine temporäre SQLite-Datenbank verifiziert, die
  `comparison_kind`-Migration zusätzlich mit vorab eingefügten Bestandsdaten.
- Beide neuen Seiten live im Browser verifiziert (Login-Pflicht, Filter, Dark Mode, Mobile,
  synthetisches Eigene/GS-Paar).
