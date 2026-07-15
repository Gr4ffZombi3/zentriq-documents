# M12 – AI Analysis Engine: Abschlussbericht

Meilenstein abgeschlossen über 10 Commits (`M12.1`–`M12.10`), strikt additiv: keine
bestehende Route, kein bestehendes Feld gelöscht, keine bestehende Business-Logik verändert –
insbesondere `app/services/llm/recommendations.py` (`build_recommendations()`/
`create_recommendations()`) ist **byte-identisch unverändert** geblieben (verifiziert per
`git diff`). Die einzige Signaturänderung an bestehendem, bereits getestetem Code ist
`extract_text()` (3-Tupel → 4-Tupel, um Seitengrenzen für die Mehrseitige-Einträge-Heuristik
verfügbar zu machen) – bewusst und einmalig, in `M12.2` zusammen mit den beiden betroffenen
Tests angepasst. Wie im Briefing gefordert: **kein neues UI, keine neuen Templates** – dieser
Meilenstein liefert ausschließlich Services und getestete API-Endpunkte; die UI-Anbindung ist
ein zukünftiger, separater Meilenstein. Testsuite: **110 → 190 bestehende Tests**, alle grün,
`ruff check` durchgehend sauber.

## 1. Architektur & Pipeline

Aus der bisherigen zweistufigen Pipeline (OCR → KI-Extraktion, zwei Fehlerzweige) wurde eine
elfstufige, einzeln testbare Analyse-Pipeline:

| # | Stufe | Umsetzung |
|---|---|---|
| 1 | PDF | unverändert |
| 2 | OCR | unverändert (`app/services/ocr/pipeline.py`), gibt seit M12.2 zusätzlich `page_texts` zurück |
| 3 | Layout-Erkennung | **neu**, `app/services/analysis/layout.py::detect_layout()` |
| 4 | Tabellen-Erkennung | **neu**, `app/services/analysis/tables.py::detect_tables()` |
| 5 | Semantische Extraktion | unverändert (`extract_document_data`/`extract_leipziger_liste_rows`), Schema um 4 Felder erweitert |
| 6 | Datenvalidierung | **neu**, `app/services/analysis/validation.py` – deterministische Plausibilitätsprüfung |
| 7 | Business-Regeln | unverändert (`recommendations.py`) **plus** neue Parallelschicht `app/services/analysis/business_rules.py` |
| 8 | KI-Auswertung | **neu**, `app/services/analysis/report.py` – deterministischer Bericht + optionale GPT-Formulierung |
| 9 | Empfehlungen | unverändert, jetzt mit generierter Begründung |
| 10 | Priorisierung | bereits vorhanden (`Priority`/`PRIORITY_ORDER`) |
| 11 | Dashboard aktualisieren | **kein neuer Code nötig** – `cockpit.py`/`bestand.py`/`kpis.py` lesen bereits live aus der DB, es gibt keinen zu invalidierenden Cache |

Jede neue Stufe ist eine reine, unabhängig aufrufbare Funktion (`raw_text -> Ergebnis` bzw.
`Extraktion -> Ergebnis`), ohne Datenbank- oder Netzwerkzugriff bei den Stufen 3/4/6 – das
erfüllt die Anforderung "Jeder Schritt muss einzeln testbar sein" direkt.

`app/tasks/document_tasks.py::_run_pipeline()` orchestriert alle Stufen, misst die Dauer jeder
Stufe (`stage_durations`) und erzeugt/aktualisiert für **jeden** Verarbeitungsversuch einen
`AnalysisRun`-Datensatz (Erfolg, OCR-Fehler und KI-Analyse-Fehler münden alle in einen
abgeschlossenen Lauf). `DocStatus.OCR_DONE` wird jetzt tatsächlich erreicht – das UI-Badge dafür
existierte bereits seit dem Frontend-Redesign, war aber bisher unerreichbar.

## 2. Erkennung – neue Felder

Bereits vorher erkannt: Kunde, Vertragsnummer, Produkt, Status (über Flags), Abschluss
(`is_neugeschaeft`), Angebot, Storno, Datum, Bemerkungen, Dokumenttyp, Vermittler (`broker`).

Neu hinzugekommen (additiv, `Default None`, in `DocumentExtraction` und `LeipzigerListeRow`):

| Feld | Bedeutung |
|---|---|
| `broker_number` | VM-Nummer, wie im Dokument aufgedruckt (nicht zu verwechseln mit `broker` = Name) |
| `product_line` | Sparte (z.B. "KFZ", "Sach"), getrennt von der freien Produktliste |
| `premium` | Beitrag/Prämie, bewusst als **String** (siehe Grenzen) |
| `tariff` | Tarifbezeichnung |

Tabellen (`app/services/analysis/tables.py`), Fußnoten (`app/services/analysis/layout.py`) und
mehrseitige Einträge (heuristisch über `page_texts`) sind neu erkennbare Layout-Signale, siehe
Abschnitt Grenzen für die genaue Methode.

**Explizite Klarstellung zu M11**: M11 hat bewusst entschieden, keine Euro-Beträge zu
extrahieren (KPI-Score bleibt ein transparent beschrifteter "Potenzial-Score"). `premium`/
`Beitrag` ist eine **neue, explizite Anforderung dieses Meilensteins** als angezeigtes/
gespeichertes Extraktionsfeld – das speist weiterhin **nicht** `potential_score.py`, die
M11-Entscheidung zu Nicht-Geldwert-Kennzahlen bleibt also in der Sache unverändert bestehen.

## 3. Business-Regeln – "verstehen, nicht nur erkennen"

`app/services/analysis/business_rules.py`, eine **eigenständige Parallelschicht** zu
`build_recommendations()` (nicht verändert, nicht erweitert):

| Regel | Auslöser | Priorität |
|---|---|---|
| Cross-Selling Gebäude→Hausrat | `"gebäude"` in Produkten, `"hausrat"` fehlt | MEDIUM |
| Cross-Selling KFZ→Privathaftpflicht | Fahrzeug oder `"kfz"` vorhanden, `"privathaftpflicht"` fehlt | MEDIUM |
| Vertriebsrisiko | ≥2 Angebote für denselben Kunden ohne je erfolgten Abschluss | HIGH |
| Storno-Priorität | `is_storno=True` | HIGH |

Jede erzeugte `Recommendation` bekommt eine generierte **Begründung** (`explanation`-Spalte,
Template über echte extrahierte Werte, kein GPT-Call) – z.B. für die bestehende
Nachfassen-Empfehlung exakt das Briefing-Beispiel:
*"Angebot wurde vor 18 Tagen erstellt. Keine Rückmeldung erkannt. Status unverändert."*
(`offer_followup_explanation()`, wiederverwendet in `wiedervorlagen.py`). Beim Promoten einer
Recommendation zu einem `Task` (M11-Mechanismus) wird die Begründung mitkopiert.

"Angebot älter als 14 Tage → hohe Priorität" war bereits seit M11 (`wiedervorlagen.py`)
vorhanden und bekam in diesem Meilenstein nur die Begründung ergänzt.

## 4. Analysebericht

`app/services/analysis/report.py::build_analysis_report()` – **deterministisch**, läuft ohne
jeden OpenAI-Aufruf: Executive Summary, Kurzfassung, Gesamtbewertung (positiv/neutral/kritisch),
Abschlussquote, neue Abschlüsse/Angebote, Stornos, offene Vorgänge, unbearbeitete Kunden,
Empfehlungsanzahl, Top-Chancen (via `potential_score.py`) und Top-Risiken (aus den neuen
Business-Regeln). Optional (`ANALYSIS_NARRATIVE_ENABLED`, Default an, in Tests aus) **ein**
zusätzlicher GPT-Aufruf, der ausschließlich die bereits berechneten Fakten in einen kurzen
Fließtext umformuliert – der System-Prompt verbietet neue Fakten, bei jedem Fehler sicherer
Fallback auf den deterministischen Text (gleiches Muster wie `search_parser.py`). Das ist die
konkrete Umsetzung der geforderten Hybrid-Engine für diese Stufe: Regeln liefern die Fakten, KI
formuliert (optional) nur um, die "Plausibilitätsprüfung" ist strukturell – die KI hat keinen
Weg, neue Fakten einzubringen.

Abrufbar über die neue Route `GET /documents/<id>/analysis-runs` (JSON), da ein Bericht sonst
nur per direktem DB-Zugriff einsehbar wäre.

## 5. Erkennungsqualität – Konfidenz, Quelle, Original-/Normalwert

`app/services/analysis/validation.py` – **deterministisch statt GPT-Selbsteinschätzung**
(Modelle "raten" bei Selbsteinschätzung oft hohe Werte). Pro Feld: 0–100-Score aus
nicht-leer (+40), Fuzzy-Match im OCR-Rohtext via `difflib` (+40), Format-Sanity-Check wo
definiert – aktuell Postleitzahl (+20, sonst automatisch gewährt). Boolesche Flags
(`is_storno` etc.) werden über Schlüsselwort-Präsenz im Rohtext bewertet, da sie kein
"im Text gefunden"-Konzept haben. Ergebnis je Feld: `{confidence, source (ocr/ki),
original_text, normalized_value, uncertain}`, gespeichert in `Document.field_confidence`
(generische Dokumente) bzw. `DocumentCustomer.field_confidence` (Liste, parallel zu
`row_data`, Leipziger Liste). Der Schwellenwert (`FIELD_CONFIDENCE_UNCERTAIN_THRESHOLD`,
Default 70) markiert unsichere Felder. `AnalysisRun.overall_confidence` fasst alle Feld-Scores
eines Laufs zusammen.

## 6. Vergleich mehrerer Listen

Bereits seit M11 deckte `app/services/list_comparison.py` sechs von sieben geforderten
Kategorien ab (neue Kunden, neue Verträge, neue Angebote, Statusänderungen, Stornos, entfernte
Kunden). Neu in M12: `ListChangeType.NEW_PRODUCT_LINE` ("neue Sparte"), erkannt **zusätzlich
zu**, nicht anstelle von, den bestehenden Kategorien – ein Kunde kann im selben Vergleichslauf
sowohl einen `NEW_CONTRACT`- als auch einen `NEW_PRODUCT_LINE`-Eintrag bekommen.

## 7. KI-Assistent

`app/services/analysis/chat_assistant.py` + `POST /api/chat` – acht feste Function-Calling-Tools
(kein generischer SQL-Generator, gleiches Sicherheitsmuster wie `search_parser.py`), jede
Funktion filtert strikt über `assigned_user_id` des angemeldeten Nutzers: heute anrufen, nur ein
Angebot ohne Abschluss, keine Rückmeldung seit ≥7 Tagen, nur ein bestimmtes Produkt, ein Produkt
fehlt, höchstes Abschlussrisiko, höchstes Cross-Selling-Potenzial, Vorgänge älter als 30 Tage.
Der Antworttext wird deterministisch aus den Abfrageergebnissen komponiert (kein zweiter
GPT-Aufruf).

## 8. Lernfähigkeit / Feedback

`app/services/feedback.py` + `POST /tasks/<id>/feedback` – 👍/👎-Bewertung pro Aufgabe
(`RecommendationFeedback`), `get_accuracy_by_type()` liefert die Trefferquote pro Aufgabentyp.
**Bewusst nur Datenerfassung + Auswertung** in diesem Meilenstein – keine automatische
Rückkopplung in Prompts oder Regelgewichte (das wäre ein eigenes, deutlich größeres
ML-Feature).

## 9. Analyse-Historie & Performance

Neue Tabelle `AnalysisRun` (eine Zeile **pro Verarbeitungsversuch**, nicht überschrieben wie
bisher die `Document`-Spalten): Engine-Version, Prompt-Version, verwendetes OpenAI-Modell,
Konfidenz, Gesamtdauer, Dauer pro Stufe, Fehlermeldung. Retries akkumulieren Historie statt sie
zu überschreiben – das schließt eine echte Lücke (vorher gab es keine Spur früherer
fehlgeschlagener Versuche nach einem erfolgreichen Retry).

Performance: die Pipeline war bereits asynchron (Celery); neu ist die granulare
Fortschrittsmessung pro Stufe. Layout-/Tabellen-Erkennung sind reine Regex-Operationen auf
bereits vorhandenem Text (keine zusätzlichen OCR-/GPT-Aufrufe) und tragen in Tests im
Millisekundenbereich zur Gesamtdauer bei.

## 10. Neue Datenbank-Elemente (eine Migration: `c7274613e41d`)

**Neue Tabellen**: `analysis_runs` (`AnalysisRun`), `recommendation_feedback`
(`RecommendationFeedback`).
**Neue Spalten**: `documents.{broker_number,product_line,premium,tariff,field_confidence}`,
`document_customers.field_confidence`, `recommendations.explanation`, `tasks.explanation`,
`list_comparisons.new_product_line_count`.
**Neue Enums**: `AnalysisRunStatus`, `FeedbackRating`; additive Werte auf `RecommendationType`
(4 neue Regeln) und `ListChangeType` (`NEW_PRODUCT_LINE`).
Migration verifiziert up/down/up gegen eine temporäre SQLite-Datenbank, inklusive der
Enum-Erweiterung auf der bestehenden `recommendations.type`-Spalte via `batch_alter_table`
(funktioniert dialektübergreifend für SQLite und die produktive MariaDB).

## 11. Neue Services

| Datei | Funktionen |
|---|---|
| `app/services/analysis/layout.py` | `detect_layout` |
| `app/services/analysis/tables.py` | `detect_tables` |
| `app/services/analysis/validation.py` | `score_field`, `score_extraction`, `score_leipziger_liste_row`, `score_boolean_flag` |
| `app/services/analysis/business_rules.py` | `build_advanced_recommendations`, `create_advanced_recommendations`, `offer_followup_explanation`, `count_offer_occurrences`, `customer_has_ever_closed` |
| `app/services/analysis/report.py` | `build_analysis_report` |
| `app/services/analysis/chat_assistant.py` | `answer_chat_query` + 8 Tool-Funktionen |
| `app/services/feedback.py` | `record_feedback`, `get_accuracy_by_type` |

Additive Erweiterungen bestehender Dateien: `app/services/documents.py` (Validierungs-/
Business-Regel-Hooks nach der bestehenden Extraktion), `app/services/tasks.py`
(`explanation`-Kopie, 4 neue `RecommendationType`→`TaskType`-Zuordnungen), `app/services/
wiedervorlagen.py` (Begründung auf bestehende Wiedervorlage-Tasks), `app/services/
list_comparison.py` (neue-Sparte-Erkennung), `app/services/ocr/pipeline.py` (`page_texts`
zurückgeben), `app/tasks/document_tasks.py` (vollständige Pipeline-Orchestrierung),
`app/blueprints/documents/routes.py` (`GET /analysis-runs`), `app/blueprints/tasks/routes.py`
(`POST /feedback`), neues `app/blueprints/chat/routes.py`.

## 12. Neue Tests

110 ursprüngliche Tests bleiben unverändert grün. 8 Testdateien neu, 4 erweitert:

- `test_m12_models.py` – Tenant-Scoping der 2 neuen Tabellen, neue Spalten defaulten auf `None`
- `test_analysis_layout.py`, `test_analysis_tables.py` – Heuristiken gegen mehrere Testdaten-Sets
- `test_pipeline.py` (erweitert) – 4-Tupel-Rückgabe von `extract_text()`
- `test_extraction.py`, `test_leipziger_liste.py` (erweitert) – neue Extraktionsfelder auf Dokument- bzw. Zeilenebene
- `test_analysis_validation.py` – Konfidenz-Formel, Format-Check, boolesche Flags, volle Zeilenbewertung
- `test_analysis_business_rules.py` – alle 4 neuen Regeln positiv/negativ, Begründungstext exakt gegen das Briefing-Beispiel, `recommendations.py` nachweislich unverändert
- `test_list_comparison.py` (erweitert) – neue Sparte eigenständig und in Koexistenz mit anderen Änderungstypen
- `test_analysis_run_history.py` – Regressionstest: Historie wächst bei Reprocessing, `Document`-Spalten bleiben idempotent
- `test_analysis_report.py` – deterministischer Pfad, Top-Chancen/-Risiken, Narrativ-Pfad aktiv und Fehler-Fallback
- `test_chat_assistant.py` – alle 8 Tools einzeln, Cross-User-Isolation, Routen-Verhalten
- `test_feedback.py` – Persistenz, Trefferquoten-Berechnung, Tenant-Scoping, Routen-Verhalten inkl. Cross-Tenant-404

**Ergebnis: 110 → 190 bestehende Tests, alle grün.**

## 13. Grenzen (bewusst, offen dokumentiert statt versteckt)

- **Layout-/Tabellen-/Fußnoten-/Mehrseitige-Einträge-Erkennung ist Regex-Heuristik, keine
  echte Computer-Vision-Layoutanalyse** – im Techstack existiert keine passende Bibliothek
  (nur PyMuPDF zum Rendern, Tesseract, Pillow), eine neue schwere Abhängigkeit dafür wäre für
  diesen Meilenstein unverhältnismäßig. Kann bei stark unstrukturierten Layouts Tabellen
  übersehen oder fälschlich erkennen.
- **`premium`/Beitrag wird als String, nicht als `Numeric`, gespeichert** – OCR-Text von
  Geldbeträgen ist zu uneinheitlich ("123,45 €" vs. "123.45" vs. "1.234,56€") für
  verlässliches automatisches Parsing ohne echte Validierungsdaten.
- **Chat-Assistent ist zustandslos (kein persistierter Gesprächsverlauf)** – jede Anfrage an
  `POST /api/chat` ist unabhängig, keine Mehrschritt-Konversation.
- **Feedback fließt nicht automatisch zurück** – reine Datenerfassung, keine Prompt- oder
  Regelanpassung basierend auf Bewertungen.
- **Storno-/Neugeschäft-Schlüsselwörter sind eine feste deutsche Liste** – funktioniert für
  die bisher beobachteten Leipziger-Listen-Formulierungen, nicht garantiert vollständig für
  jede denkbare Formulierung.

## 14. Verbesserungsmöglichkeiten (zukünftige Meilensteine)

- Persistierter Chat-Gesprächsverlauf für Mehrschritt-Dialoge.
- Echte Tabellen-/Layout-Erkennung über eine dedizierte Bibliothek (z.B. `pdfplumber`), falls
  die Regex-Heuristik in der Praxis an ihre Grenzen stößt.
- Hartes `Numeric`-Feld für `premium`, sobald eine verlässliche Normalisierungsstrategie für
  OCR-Geldbeträge steht.
- Feedback-gestützte Anpassung von Regelgewichten oder Few-Shot-Beispielen in den
  Extraktions-Prompts.
- UI-Anbindung aller in M12 gebauten API-Endpunkte (`/api/chat`, `/tasks/<id>/feedback`,
  `/documents/<id>/analysis-runs`) sowie sichtbare Konfidenz-/Unsicher-Markierungen im
  Dokumenten-Detail.

## 15. Qualität

- `manage.ps1 check` (ruff + volle Testsuite) vor jedem der 10 Commits grün.
- `recommendations.py`/`test_recommendations.py` per `git diff` als unverändert verifiziert.
- Neue API-Endpunkte per curl gegen einen lokalen Server verifiziert (Auth-Redirect, echter
  Upload-Roundtrip, JSON-Struktur).
- Migration up/down/up gegen temporäre SQLite-Datenbank verifiziert.
