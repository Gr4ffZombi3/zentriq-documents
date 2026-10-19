# ZentriqAI – Konzept und Architekturentscheidungen

Stand: 19.10.2026 · Phase 1–5 (Infrastruktur und Assistent) und Phase 7 (Dashboard-Umbau):
ZentriqAI ist Startseite für alle Rollen. OmniRoute ist wegen des Arbeitsspeichers (1,8 GB) nicht
installiert; die Modellauswahl läuft über den eingebauten Router.

## 1. Bestandsaufnahme (Phase 1)

| Bereich | Befund |
|---|---|
| Server | VPS (QEMU), 2 vCPU, **1,8 GB RAM**, 2 GB Swap (dauerhaft ~0,9 GB belegt), 77 GB Platte |
| Stack | Flask 3.1, SQLAlchemy 2, MariaDB 10.11, Celery 5.6 + Redis, Gunicorn (2 Worker × 4 Threads), nginx (Proxy-Timeout 60 s) |
| Rollen | SUPER_ADMIN (nur Plattform, **bewusst kein Zugriff auf Bürodaten**), OFFICE_ADMIN, EMPLOYEE; Default-Deny in `app/auth/permissions.py` |
| Mandanten | `TenantScopedMixin` filtert jede Abfrage automatisch (fail-closed) |
| Vorhandene KI | Textassistent (`app/services/assistant.py`, Anthropic, zustandslos, je Büro freigebbar), Leipziger-Liste-Extraktion/Suche/Analyse-Chat (OpenAI), Memo-Transkription lokal (faster-whisper, ~650 MB RAM-Spitze) |
| Zeiterfassung | `app/services/timetracking/` + Blueprint `timetracking` (Ein-/Ausstempeln, Pausen) – wird in Phase 7 nur eingebunden, nicht ersetzt |
| Deployment | Code läuft direkt aus `/root/Zentriqai`; `deploy.sh` → `git pull`, `pip`, `flask db upgrade`, Neustart |

## 2. Geprüfte Projekte (Phase 2)

| Projekt | Ergebnis | Begründung |
|---|---|---|
| **OmniRoute** (`diegosouzapw/OmniRoute`, MIT, aktiv gepflegt) | **derzeit nicht installiert** | Node ≥ 22; laut Doku ≥ 2 GB Container schon für Dashboard/leichten Chat – der Server hat 1,8 GB gesamt. Ein großer Teil der „Gratis“-Kapazität beruht auf OAuth-Logins in private Abos (Claude Code, Codex, Gemini CLI, Copilot) – für eine Mehrbenutzer-Anwendung nicht zulässig. Übernommen wurden die **Konzepte**: Fallback-Kette, Abkühlzeit je Modell/Anbieter, Kostengrenzen, OpenAI-kompatible Anbindung. Bei 4 GB RAM kann OmniRoute später als zusätzlicher OpenAI-kompatibler Anbieter (nur API-Key-Anbieter) eingebunden werden – ein Eintrag in `PROVIDERS`. |
| **Claude-Mem** (`thedotmack/claude-mem`, Apache-2.0) | **nicht verwendet** | Gedächtnis-Plugin für Coding-Agenten (Hooks auf Tool-Aufrufe), Bun + Chroma, ein Benutzer pro Installation, keine Mandantentrennung. Ersetzt durch eigene MariaDB-Lösung (Abschnitt 4). |
| **awesome-freellm-apis** (MIT) | **als Anbieterkatalog genutzt** | Liste kostenloser Kontingente. Alle sind begrenzt (z. B. Groq 30 RPM/250 RPD, Gemini Tageslimit) – der Router behandelt sie nie als unbegrenzt. |

**Claude Code auf dem Server:** ist installiert (2.1.x) und wird als Entwicklungswerkzeug mit
persönlicher Anmeldung genutzt. Diese Anmeldung wird **nicht** von ZentriqAI verwendet;
ZentriqAI nutzt ausschließlich offizielle API-Schlüssel (`ANTHROPIC_API_KEY` usw.).

## 3. Architektur (Phase 3)

```
Browser ──► /zentriq (Blueprint, nur SUPER_ADMIN)
              │
              ▼
        services/ai/chat.py      Gespräche, Kontext, Kundendaten-Prüfung, Zusammenfassung
              │
              ▼
        services/ai/router.py    Auswahl → Budget → Abkühlzeit → Aufruf → Fallback → Verbuchung
              │                     │                     │
              ▼                     ▼                     ▼
        services/ai/adapters.py  services/ai/state.py  ai_usage (MariaDB)
        (openai-SDK für alle      (Redis: Abkühlzeiten,
         kompatiblen Anbieter,     Antwort-Cache)
         anthropic-SDK)
```

- **Keine neuen Dienste, keine neuen Abhängigkeiten.** `openai` und `anthropic` waren bereits
  installiert; Redis läuft bereits. Zusätzlicher RAM-Bedarf: praktisch keiner.
- **Anbieter:** Gemini, Groq, Cerebras, Mistral, OpenRouter (OpenAI-kompatibel), Anthropic,
  OpenAI. Neuer kompatibler Anbieter = ein Eintrag in `app/services/ai/catalog.py` + Key in `.env`.
- **Modelle:** Tabelle `ai_models`, im Browser pflegbar (aktiv, kostenlos, Priorität, Preis).
  „Modelle abrufen“ liest die aktuelle Liste direkt beim Anbieter – keine veralteten IDs im Code.
- **Automatische Auswahl:** erst kostenlose Modelle (Priorität), dann kostenpflichtige mit Preis
  (nur innerhalb Tages-/Monatsbudget). Kostenpflichtige Modelle **ohne Preis werden nie gewählt**.
- **Fallback:** 429 → Modell 60 s (bzw. Retry-After) pausiert; 5xx/Timeout → Anbieter 30 s;
  ungültiger Key → Anbieter 1 h; unbekanntes Modell → 1 h. Sicherheitsablehnung (refusal) →
  **kein** Ausweichen auf andere Anbieter. Gesamtzeit ≤ 50 s (unter nginx-Timeout).
- **Manuelle Wahl:** gewähltes Modell zuerst; bei Ausfall Ausweichen mit Hinweis im Chat
  (abschaltbar: `ZENTRIQ_AI_MANUAL_FALLBACK=false`).
- **Kosten:** jeder Aufruf (auch fehlgeschlagene) in `ai_usage` mit Tokens, Kosten, Latenz,
  Fehlerart – ohne Inhalte. Budgets plattformweit (`ZENTRIQ_AI_DAILY_BUDGET_USD`,
  `ZENTRIQ_AI_MONTHLY_BUDGET_USD`, Standard 0 = nur kostenlose Modelle).
- **Cache:** identische Anfrage (gleicher Benutzer, gleicher Kontext, gleiche Modellwahl) wird
  bis zu 1 h aus Redis beantwortet. Schlüssel enthält die Benutzer-ID – kein Cache-Austausch
  zwischen Benutzern.

## 4. Gedächtnis und Kontext

- **Persistenz:** `ai_conversations` / `ai_messages` (MariaDB), je Mandant **und** Benutzer.
- **Tokensparend:** gesendet werden nur die jüngsten Nachrichten bis `ZENTRIQ_AI_CONTEXT_CHARS`
  (Standard 12 000 Zeichen) plus eine laufende Zusammenfassung älterer Nachrichten. Die
  Zusammenfassung erstellt der Celery-Worker im Hintergrund ab 16 unzusammengefassten Nachrichten
  mit dem günstigsten verfügbaren Modell.
- **Suche:** Titel und Nachrichteninhalt (eigene Gespräche).
- **Löschen:** einzelnes Gespräch, alle eigenen Gespräche; Aufbewahrung
  `flask zentriq-ai-purge` (Standard 180 Tage ohne Aktivität). Verbrauchsdaten ohne Inhalte bleiben.
- **Später (Phase 6/7):** projektbezogenes Gedächtnis (Tabelle `ai_memories` mit Bereich
  Benutzer/Büro/Projekt) und semantische Suche. Für die Datenmenge eines Büros reicht MariaDB-
  Volltextsuche; ein Vektor-Dienst (Chroma o. Ä.) ist bei 1,8 GB RAM nicht sinnvoll.

## 5. Datenschutz und Sicherheit

- **Kundendaten-Modus** je Gespräch: nur Anbieter mit ausdrücklicher Freigabe
  („Für Kundendaten freigegeben – Auftragsverarbeitung geprüft“, Standard: aus).
- Außerhalb des Modus werden Nachrichten mit erkennbaren Kundendaten (IBAN, E-Mail, Telefon,
  Geburtsdatum, Kunden-/Vertragsnummer) **abgelehnt**, nicht gesendet.
- Hinweise zu Gratis-Anbietern: Gemini – im EWR gelten auch im Gratis-Kontingent die Datenregeln
  der bezahlten Dienste; Mistral (Gratis-Plan) – Training mit Eingaben, sofern nicht in der
  Admin-Konsole abgewählt; Groq/Cerebras/OpenRouter – USA. **Empfehlung:** Für Kundendaten nur
  Anbieter mit Auftragsverarbeitungsvertrag freigeben (z. B. Anthropic-API, bezahlter Mistral-Plan).
- API-Schlüssel nur in `.env`; die Oberfläche zeigt nur „Schlüssel hinterlegt ja/nein“.
- Antworten werden im Browser nur als Text eingesetzt (kein HTML).
- Prompt-Injection: Benutzerinhalte sind laut Systemanweisung Material, keine Anweisung. Phase 5
  hat **keine Werkzeuge** – ein Modell kann nichts ausführen.
- Audit-Log: Änderungen an Anbietern/Modellen (`AI_SETTINGS_CHANGED`, im Sicherheitsprotokoll)
  und Löschungen (`AI_CONVERSATION_DELETED`), jeweils nur IDs.

## 6. Werkzeugarchitektur (Planung für Phase 6/7)

**Wichtig:** Der SUPER_ADMIN hat nach dem bestehenden Rechtemodell keinen Zugriff auf Bürodaten.
Werkzeuge mit Kunden-, Listen- oder Zeitdaten werden deshalb für OFFICE_ADMIN/EMPLOYEE
freigeschaltet, nicht für den SUPER_ADMIN.

- Registry `app/services/ai/tools/`: je Werkzeug Name, Beschreibung, JSON-Schema, benötigte Rolle,
  Art (lesen / schreiben / extern) und eine Funktion, die **bestehende Services** aufruft
  (z. B. `services/customers.py`, `services/leipziger_entries.py`, `services/tasks.py`,
  `services/timetracking/`) – keine parallelen Geschäftsprozesse.
- Vor jedem Aufruf: `is_endpoint_allowed`-äquivalente Rollenprüfung + Mandantenkontext des
  Benutzers (TenantScopedMixin greift automatisch).
- Schreibende/externe Werkzeuge: Modell **schlägt vor**, Benutzer bestätigt im Chat, erst dann
  Ausführung; Audit-Eintrag.
- Werkzeugergebnisse werden als Daten (nicht als Anweisung) zurückgegeben; Modelle mit
  Werkzeugen nur bei Anbietern, die für Kundendaten freigegeben sind.
- Bestehende Assistenten (Textassistent, Analyse-Chat, Memo) bleiben bis zum nachgewiesenen
  Funktionsersatz unverändert.

## 7. Server-Ressourcen

Nach Bereinigung (apt-/pip-Cache, Journal > 30 Tage) 64 GB Platte frei. RAM unverändert:
~0,75 GB verfügbar, Spitzen bei Memo-Transkription (+0,65 GB). Für OmniRoute oder weitere
Dienste ist **4 GB RAM** die kleinste sinnvolle Erweiterung.

## 8. Neue Oberfläche (Phase 7)

| Bereich | Umsetzung |
|---|---|
| Startseite | `/` → `/zentriq` für alle Rollen (ohne ZentriqAI wie bisher Übersicht bzw. Plattform). Die bisherige Übersicht bleibt unter „Übersicht“ (`/uebersicht`) erreichbar. |
| Seitenleiste | `components/app_sidebar.html` auf allen Seiten: Logo, Neue Unterhaltung, Chats durchsuchen, Gespeichert, Letzte Unterhaltungen (per JS nachgeladen), Bereiche aus `app/navigation.py` (unverändert rollenabhängig), Konto. Auf dem Desktop einklappbar, mobil per Menü. |
| Chat | `zentriq/index.html` + `js/zentriq-ai.js`: Markdown (sicheres Subset, HTML wird vorher maskiert), Codeblöcke mit Kopieren, Datei-Upload (PDF/TXT/MD/CSV, Audio), Lade-/Verarbeitungsanzeige, automatisches Scrollen, Modellwahl, Kundendaten-Modus, Speichern/Umbenennen/Projekt/Löschen. Adressen `/zentriq/c/<id>`. |
| Asynchron | KI-Antworten im Celery-Worker (`app/tasks/ai_tasks.py`), der Browser fragt den Platzhalter ab. Kein KI-Aufruf im Flask-Request-Thread; Zeiterfassung und Bürobereiche funktionieren unabhängig von der KI. Hängende Antworten werden nach 5 Minuten als Fehler markiert. |
| Werkzeuge | `app/services/ai/tools.py`, nur lesend: Arbeitszeiten, offene Vorgänge (Leipziger Liste), Suche, Aufgaben*, Team-Anwesenheit* (*Büro-Admin). Jedes Werkzeug ist an den Endpunkt gebunden, der dieselben Daten anzeigt, und nutzt `is_endpoint_allowed` – beim Anbieten und beim Ausführen. Werkzeuge nur über Anbieter mit Kundendaten-Freigabe; ohne Freigabe sagt ZentriqAI das. SUPER_ADMIN: keine Werkzeuge. |
| Anhänge | Text wird extrahiert (PyMuPDF), die Datei nicht gespeichert. Audio: Prüfung und lokale Transkription über die bestehende Memo-Logik (`services/memo.py`) im Worker, temporäre Datei wird gelöscht. Erkennbare Kundendaten und Audio nur im Kundendaten-Modus. |
| Zeiterfassung | `components/time_clock.html` unten rechts für Büro-Admins und Mitarbeiter: Status, Ein-/Ausstempeln, Pause, laufende Tageszeit. Nutzt ausschließlich die bestehenden Endpunkte (`timetracking.status` neu, Stempel-Endpunkte liefern bei `Accept: application/json` JSON). Auf dem Smartphone kompakt über der Chat-Eingabe. |
| KI-Einstellungen | Blueprint `zentriq_admin` (`/zentriq/einstellungen`), nur SUPER_ADMIN. |

**Bewusst noch nicht umgesetzt:** schreibende Werkzeuge (Aufgabe erledigen, Kunde anlegen …) –
sie benötigen eine Bestätigung im Chat vor der Ausführung; strukturierte Leipziger-Liste-Analyse
über den Chat (die bestehende Auswertung unter „Leipziger Liste“ bleibt maßgeblich; im Chat wird
das PDF als Text gelesen). Der bisherige Textassistent und der Analyse-Chat bleiben bis zum
nachgewiesenen Ersatz unverändert.

**Betrieb:** Der Celery-Worker bearbeitet Chat-Antworten und Memo-Transkriptionen gemeinsam
(Standard: 2 parallele Aufgaben). Läuft eine lange Transkription, kann eine Chat-Antwort warten.
Bei Bedarf später eine eigene Warteschlange für ZentriqAI einrichten (Änderung der Worker-Unit).

## 9. Orchestrierung (KI-Backend)

**Ablauf:** Anfrage → Absicht → berechtigte Daten → kurze Antwort (`app/services/ai/orchestrator.py`).

1. **Direktantwort ohne KI-Aufruf** für eindeutige, kurze Standardfragen („Was ist heute offen?“,
   „Welche Aufgaben sind offen?“, „Meine Arbeitszeiten diese Woche“, „Wer ist heute da?“). Das
   passende Werkzeug liefert die Daten, die Antwort wird lokal formatiert – keine Tokens, keine
   Datenübertragung, funktioniert auch ohne eingerichteten Anbieter. Bei Bearbeitungswünschen
   („formuliere“, „warum“, „analysiere“ …) oder manueller Modellwahl entscheidet das Modell.
2. **Modell mit Werkzeugen:** Das Modell wählt die benötigten Werkzeuge selbst; jedes prüft die
   Rechte serverseitig (Endpunkt-Bindung, Rolle, Mandant). Statusanzeige je Werkzeug
   („Prüfe Aufgaben“ …), keine Gedankengänge.
3. **Modellstufen:** einfache Anfragen → kleine Modelle (Stufe 1), Werkzeuge/Dokumente/
   Bearbeitungsaufgaben → starke Modelle (Stufe 2/3); kostenlose immer zuerst, kostenpflichtige
   nur mit Budget > 0 und hinterlegtem Preis. Stufe je Modell unter KI-Einstellungen.
4. **Datenschutz:** Gibt es einen für Kundendaten freigegebenen Anbieter, gehen Bürodaten nur
   dorthin. Sonst **Pseudonymisierung** (`privacy.py`): Personenfelder in Werkzeugergebnissen
   feldgenau, Freitext (Nachrichten, Transkripte, Verlauf, Zusammenfassungen) über den bestehenden
   Anonymisierer; Platzhalter wie `[Kunde 1]` werden erst lokal in der Antwort zurückgesetzt,
   Werkzeug-Argumente des Modells vor der Ausführung lokal aufgelöst.
5. **Dokumente:** Leipziger Listen (WM312) werden mit dem bestehenden deterministischen Parser
   lokal ausgewertet; an das Modell geht eine kompakte Auswertung (ohne Kundendaten-Modus nur
   Zählungen).
6. **Schreibende Aktionen** (`actions.py`): Das Modell kann sie nur vorbereiten (`ai_actions`,
   Status `pending`). Ausführung erst nach „Bestätigen“ durch denselben Benutzer, mit erneuter
   Rechteprüfung, über die bestehende Geschäftslogik, genau einmal (atomarer Statuswechsel),
   Audit-Eintrag `AI_ACTION_EXECUTED`. Erstes Werkzeug: Aufgabe als erledigt markieren.
7. **Doppelversand:** Jede Nachricht trägt eine Browser-Kennung (`client_ref`); Wiederholungen
   liefern die vorhandene Antwort statt einer neuen Verarbeitung.

**Werkzeuge** (lesend, sofern nicht anders angegeben): Tagesüberblick, Arbeitszeiten, offene
Vorgänge, Listenauswertung, Suche, Aufgaben*, Team-Anwesenheit*, Mitarbeiteraktivität*,
Sprachnachrichten*, Aufgabe erledigen* (schreibend, mit Bestätigung). *nur Büro-Admin.

**Nicht umgesetzt:** Streaming der Antworttexte (Antworten erscheinen nach Fertigstellung; der
Status wird laufend aktualisiert). OmniRoute ist als Anbieter vorbereitet (`OMNIROUTE_BASE_URL`,
`OMNIROUTE_API_KEY`), aber wegen des Arbeitsspeichers nicht installiert.
