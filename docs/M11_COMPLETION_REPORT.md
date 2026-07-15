# M11 – Vermittler-Cockpit & Intelligente Aufgabenverwaltung: Abschlussbericht

Meilenstein abgeschlossen über 12 Commits (`M11.1`–`M11.12`), strikt additiv: keine
bestehende Route, kein bestehendes Feld, keine bestehende Business-Logik (insbesondere
das `Recommendation`-System) wurde verändert oder gelöscht. Testsuite: **56 → 110
bestehende Tests**, alle grün, `ruff check` durchgehend sauber.

## 1. Neue Seiten

| Route | Zweck |
|---|---|
| `GET /tasks` | Aufgabenverwaltung – Filter nach Status (Offen/Erledigt/Verworfen/Alle), Priorität-Sortierung |
| `POST /tasks/<id>/status` | Aufgabenstatus ändern (HTMX-Partial-Response möglich) |
| `GET /cockpit` | Tagescockpit – 6 Kacheln (höchste Priorität, heute anrufen, höchstes Potenzial, überfällige Vorgänge, neue Dokumente, persönliche Abschlussquote) |
| `GET /bestand` | Mein Bestand – eigene Kunden/Aufgaben/Wiedervorlagen/Kennzahlen, strikt nach `assigned_user_id` gefiltert |
| `GET /customers/<id>` | Kundendetail mit Chronologie und zusammengeführtem Leipziger-Liste-Verlauf (Listenseite existierte bereits, Detailseite ist neu) |

Zusätzlich additive Erweiterung von `documents/detail.html` (keine neue Route): Dokumentenverlauf-Panel und Listenvergleich-Anzeige.

## 2. Neue Komponenten

- `app/templates/tasks/list.html`, `tasks/_task_row.html`
- `app/templates/cockpit/index.html`
- `app/templates/bestand/index.html`
- `app/templates/customers/detail.html`
- `app/templates/documents/_verlauf.html`, `documents/_list_comparison.html`
- 9 neue Lucide-Icons in `app/templates/ui/icons.html`: `phone`, `calendar`, `list-todo`, `briefcase`, `flame`, `target`, `trending-up`, `git-compare`, `history`
- 3 neue Sidebar-Einträge: Tagescockpit, Aufgaben, Mein Bestand

Alle neuen Templates nutzen ausschließlich die bestehende Komponenten-Bibliothek (`ui/display.html`, `ui/forms.html`, `ui/icons.html`) – keine neuen Basis-Komponenten nötig, volle Dark-Mode-/Responsive-Unterstützung geerbt.

## 3. Neue Datenbanktabellen

| Tabelle | Modell | Zweck |
|---|---|---|
| `tasks` | `Task` | Vereinheitlichte Aufgaben-/Wiedervorlagen-Tabelle, optional an eine `Recommendation` gekoppelt |
| `list_comparisons` | `ListComparison` | Kopfdaten eines Leipziger-Listen-Vergleichslaufs |
| `list_comparison_entries` | `ListComparisonEntry` | Eine Zeile pro geändertem Kunden (neu/entfernt/Storno/Statusänderung) |
| `customer_timeline_events` | `CustomerTimelineEvent` | Append-only Kundenchronologie |

Additive Spalten: `documents.uploaded_by_user_id`, `documents.is_storno`, `customers.assigned_user_id`.
Neue Enums: `TaskType`, `TaskStatus`, `WiedervorlageReason`, `ListChangeType`, `TimelineEventType`.
Eine Migration: `aff0bcca10b2` (verifiziert up/down/up gegen SQLite).
`LeipzigerListeRow`-Schema und Extraktions-Prompt additiv um `is_storno` erweitert.

## 4. Neue Services

| Datei | Funktionen |
|---|---|
| `app/services/tasks.py` | `create_tasks_from_recommendations`, `create_flag_based_tasks`, `update_task_status` |
| `app/services/timeline.py` | `log_timeline_event` |
| `app/services/wiedervorlagen.py` | `sweep_offer_wiedervorlagen`, `get_open_offer_customer_dates` |
| `app/services/list_comparison.py` | `compare_leipziger_liste` |
| `app/services/potential_score.py` | `compute_potential_score` |
| `app/services/kpis.py` | `get_sales_kpis` |
| `app/services/cockpit.py` | `get_daily_cockpit` |
| `app/services/bestand.py` | `get_bestand` |

Additive Erweiterungen bestehender Dateien: `app/services/documents.py` (Uploader-ID durchreichen, Task-/Timeline-Hooks nach den zwei bestehenden `create_recommendations()`-Aufrufen), `app/tasks/document_tasks.py` (Retry-Reset für `tasks`, Listenvergleich-Aufruf im bestehenden Leipziger-Liste-Zweig).

## 5. Neue Tests

56 ursprüngliche Tests bleiben unverändert grün. 14 Testdateien neu erstellt bzw. erweitert:

- `test_m11_models.py` – Tenant-Scoping aller 4 neuen Tabellen
- `test_extraction.py` (erweitert) – sticky Bestandszuordnung
- `test_upload.py` (erweitert) – Uploader-/Zuordnungs-Assertions
- `test_tasks.py` – automatische Aufgaben-Generierung, Retry-Idempotenz
- `test_timeline.py` – Chronologie-Einträge pro Flag-Typ
- `test_wiedervorlagen.py` – 7-/14-Tage-Erkennung, Eskalation, Idempotenz
- `test_list_comparison.py` – Listenvergleich-Engine (neue/entfernte Kunden, Storno)
- `test_kpis_cockpit_bestand.py` – Potenzial-Score, KPI-/Cockpit-/Bestand-Isolation zwischen Vermittlern
- `test_tasks_routes.py`, `test_cockpit_routes.py`, `test_bestand_routes.py` – Routen-Verhalten
- `test_customer_detail.py` – Kundendetailseite inkl. Cross-Tenant-404
- `test_document_history_and_comparison.py` – additive Dokument-Panels ohne Regression
- `test_m11_integration.py` – End-to-End über echte `/upload`-Route, Cross-Tenant-Isolation über Routen

**Ergebnis: 56 → 110 bestehende Tests, alle grün.**

## 6. Bewusste Scope-Entscheidungen

- Erinnerungen werden nicht persistiert, sondern live aus fälligen/überfälligen Aufgaben berechnet (Tagescockpit).
- Keine Euro-Beträge – "Potenzial" ist ein transparent beschrifteter Score aus vorhandenen Signalen, keine Änderung an der KI-Extraktion.
- "Termin notwendig" wird nicht automatisch per Freitext-Heuristik erkannt (zu unzuverlässig) – `SCHEDULE_APPOINTMENT` bleibt als Aufgabentyp definiert, aber ungenutzt in diesem Meilenstein.
- Wiedervorlagen-Sweep läuft synchron beim Laden von Tagescockpit/Mein Bestand statt über einen neuen Celery-Beat-Prozess – kein zusätzlicher Infrastruktur-/Deployment-Bedarf.

## 7. Qualität

- Responsive (Mobile/Desktop) und Dark Mode auf jeder neuen Seite live im Browser geprüft.
- Mandantentrennung für alle neuen Tabellen und Routen verifiziert (automatischer Tenant-Filter + `get_or_404_scoped`).
- Personenbezogene Trennung ("Mein Bestand", Tagescockpit) mit zwei echten Login-Sessions im selben Tenant live verifiziert.
- `manage.ps1 check` (ruff + volle Testsuite) vor jedem der 12 Commits grün.
