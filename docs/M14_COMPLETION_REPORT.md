# M14 – Premium Dashboard Redesign: Abschlussbericht

Meilenstein abgeschlossen über 10 Commits (`M14.1`–`M14.10`; `M14.11` ist dieser
Verifikations-/Berichts-Schritt ohne eigenen Produktivcode), **strikt visuell**: kein
einziges `.py`-File und keine Migration wurden angefasst (per `git diff --name-only` über
alle M14-Commits verifiziert – leere Ausgabe für `*.py` und `migrations/`). Keine Route,
keine API, kein Model, keine Business-Logik, keine KI-Auswertung wurde verändert. Geändert
wurden ausschließlich Jinja-Templates, die zentrale Tailwind-/Macro-Bibliothek und die
kompilierte `app.css`. Testsuite: **245 → 245 Tests**, durchgehend grün, `ruff check`
durchgehend sauber (keine Python-Änderung, daher irrelevant, aber zur Vollständigkeit
mitgeprüft).

## 1. Architektur: additive Design-System-Erweiterung statt Neubau

Alles baut auf dem bereits bestehenden, bewährten System auf (Tailwind v4 CSS-first,
`app/templates/ui/*.html`-Macro-Bibliothek) – nichts wurde ersetzt:

- **`card()`**: `rounded-xl` → `rounded-2xl`, `shadow-sm` → neues `.shadow-soft` (gedämpfter,
  mehrstufiger Schatten, dark-mode-angepasst), neuer optionaler Parameter `hover=False`
  (Hover-Lift-Animation, opt-in). Jede der über 30 bestehenden `{% call card() %}`-Stellen
  hat den neuen Look automatisch geerbt, ohne dass jede Stelle einzeln geändert werden
  musste – nur Stellen, die zusätzlich `hover=True` oder größeres Padding wollten, wurden
  gezielt angepasst.
- **`button()`**: neuer optionaler Parameter `size='md'|'lg'`, `rounded-lg` → `rounded-xl`,
  dezentes `active:scale-[0.98]` auf alle Varianten einheitlich.
- **`input_field()`**: größere Eingabefelder (`px-4 py-3`, `rounded-xl`).
- **Neue Macro `pill()`**: Segmented-Control-/Chip-Optik für ein *echtes* `<input
  type="radio"|"checkbox">` über die reine CSS-`has-[:checked]`-Variante – kein Alpine/JS
  nötig, das serverseitig gerenderte `checked`-Attribut bleibt exakt erhalten (kritisch für
  Testkompatibilität, siehe Abschnitt 5).
- **Neue Utilities** `.shadow-soft`/`.shadow-soft-lg` und `.glass-surface` (dezentes
  `backdrop-blur`, bewusst nur für Topbar/Dropdown/Modal, nicht für Cards/Sidebar – vermeidet
  "Glassmorphism überall").
- **Neues Icon** `panel-left` für den Sidebar-Collapse-Toggle.

## 2. Sidebar & Topbar: ChatGPT-artige Navigation

Die Desktop-Sidebar ist jetzt einklappbar: neuer `railCollapsed`-Alpine-State auf `<body>`,
persistiert in `localStorage` (`zentriq-sidebar-collapsed`) – exakt das gleiche Muster wie
der bereits bestehende Dark-Mode-Toggle, also eine rein clientseitige UI-Präferenz, keine
neue Backend-Funktion. Umgesetzt über Alpines Objekt-`:class`-Syntax mit einer immer
vorhandenen Basisklasse (`lg:w-64`/`lg:pl-64`) plus einer additiven `lg:!w-20`/`lg:!pl-20`-
Overrides bei `railCollapsed=true` – Alpine muss dadurch nie eine Klasse entfernen, nur per
`!important` überschreiben (robuster als ein Klassen-Austausch, siehe Abschnitt 6). Nav-Items
haben jetzt einen festen Icon-Kreis-Slot (Label blendet beim Einklappen per `x-show` aus),
deutlicherer aktiver Zustand, Hover-Verschiebung. Die `nav_items`-Liste (12 Einträge,
Endpoints/Anchors) ist byte-identisch geblieben. Topbar-Suche größer/schlichter
(`name="q"` unverändert), Header und User-Dropdown nutzen `.glass-surface`/`.shadow-soft-lg`.

## 3. Seiten-für-Seiten-Überarbeitung

| Commit | Seiten | Kernänderung |
|---|---|---|
| M14.3 | Dashboard, Cockpit | `card(hover=True)`, Kennzahlen `text-2xl` → `text-3xl` |
| M14.4 | Potenziale, Vergleich | Filter als Pills/Toggle-Switch (echte Controls, s.u.), Conversation-List-Karten |
| M14.5 | Dokumente (Liste/Row/Upload/Detail) | Upload-Dropzone größer, `list_scope` als Pills, Detail-Spalten `lg:sticky` |
| M14.6 | Kunden (Liste/Detail) | Card-Grid mit Avatar-Motiv, Tabelle mit Sticky-Header/großen Zeilen |
| M14.7 | Empfehlungen, Aufgaben | Card-Hover, Status-Tabs als Segmented Control |
| M14.8 | Mein Bestand | Gleiches KPI-Card-Muster wie Dashboard/Cockpit |
| M14.9 | Login, Registrierung | `button(size='lg')`, dezenter radialer Hintergrund-Verlauf |
| M14.10 | Einstellungen, Profil | Icon-/Avatar-Kreise vereinheitlicht |

## 4. Neue UI-Komponenten/-Muster

| Komponente | Zweck | Ort |
|---|---|---|
| `card(hover=…)` | Hover-Lift-Animation für klickbare Karten | `ui/display.html` |
| `pill()` | Segmented Control/Chip auf Basis eines echten Radio/Checkbox | `ui/display.html` |
| `.shadow-soft`/`.shadow-soft-lg` | Gedämpfter, mehrstufiger Schatten (Card-Basis) | `tailwind_source.css` |
| `.glass-surface` | Dezentes Blur nur für Topbar/Dropdown/Modal | `tailwind_source.css` |
| Sidebar-Rail-Collapse | Alpine + localStorage, analog Dark-Mode-Toggle | `components/sidebar.html`, `base.html` |
| Toggle-Switch (sr-only + `has-[:checked]`) | Visuelle Checkbox-Alternative ohne JS | `potenziale/index.html` |
| Sticky Detail-Spalten | `lg:sticky lg:top-24` für PDF-Viewer + Analyse-Karte | `documents/detail.html` |

## 5. Kritischste Entscheidung: Filter-Controls bleiben echte Formularelemente

Das Briefing wollte Filter "nicht als langweilige Selectboxen, sondern als Badges/Pills/
Segmented Controls" – die bestehende Testsuite prüft aber an mehreren Stellen exakte
gerenderte HTML-Attribute (`'value="pruefen" selected' in body`, `'checked' in body`). Die
gewählte Lösung: `category` bleibt ein echtes `<select>`, `include_closed` und `list_scope`
bleiben echte `<input type="checkbox"|"radio">` – nur visuell per `sr-only` +
`has-[:checked]`-CSS als Pill/Toggle-Switch umgestylt. Das serverseitig gerenderte
`selected`/`checked`-Attribut bleibt dadurch exakt wie zuvor. **Ergebnis: kein einziger
bestehender Test musste geändert werden** – ursprünglich als größtes Regressionsrisiko
dieses Meilensteins identifiziert, am Ende risikofrei umgesetzt.

## 6. Ein reales Problem während der Umsetzung (und seine Lösung)

Beim ersten Entwurf des Sidebar-Collapse (`:class="… ? 'lg:w-20' : 'lg:w-64'"`, Ternary-
String-Konkatenation) zeigte die Live-Browser-Verifikation scheinbar widersprüchliche
Ergebnisse (Klassen-Attribut korrekt, aber `getComputedStyle`/`getBoundingClientRect` zeigten
teils veraltete Werte). Nach ausführlicher Diagnose (Abgleich über `document.styleSheets`,
Kontrollelement-Test, `git stash`-Vergleich mit dem unveränderten Vorzustand) stellte sich
heraus: die Kernlogik war die ganze Zeit korrekt, die Diskrepanz lag am bekannten
RAF/Layout-Stall-Verhalten des Browser-Test-Tools selbst (bereits vorher dokumentiert,
jetzt mit einer schnelleren Diagnosemethode ergänzt). Die Implementierung wurde trotzdem
präventiv auf ein robusteres Muster umgestellt: eine immer vorhandene Basisklasse
(`lg:w-64`/`lg:pl-64`) plus eine additive `!important`-Override (`lg:!w-20`/`lg:!pl-20`) statt
eines Klassen-Austauschs – Alpines offizielle Objekt-`:class`-Syntax, die nie eine Klasse
entfernen muss. Dieses Muster ist unabhängig vom ursprünglichen Diagnose-Ergebnis die
sauberere Lösung und wird für zukünftige bedingte Klassen empfohlen.

## 7. Grenzen (bewusst, offen dokumentiert)

- **Kein Favicon/Theme-Color** – im Briefing nicht erwähnt, bewusst außen vor gelassen, um
  den Diff eng am tatsächlichen Auftrag zu halten.
- **`customers/detail.html`s Leipziger-Liste-Verlauf bleibt ein `<table>`** – korrekt für
  Tabellendaten, WCAG-konformer als eine nachgebaute Grid-Lösung; nur mit Sticky-Header,
  größeren Zeilen und einem begrenzten Scroll-Container umgestylt, keine strukturelle
  Neufassung der `selectattr`-gesteuerten Iteration.
- **Kein neues Chat-Widget** – `POST /api/chat` bleibt reines JSON, kein Template existiert
  dafür, im Briefing auch nicht in der Seitenliste genannt.
- **`documents/_list_comparison.html` zeigt weiterhin nur 6 von 7 möglichen Zähler-Badges**
  (kein `new_product_line_count`, anders als `potenziale/vergleich.html`) – bewusst nicht im
  Rahmen dieses rein visuellen Meilensteins nachgezogen, um keine Inhalts-/Feature-Änderung
  einzuschmuggeln; bleibt eine Notiz für einen künftigen, expliziten Auftrag.

## 8. Performance

- Keine neue Library, kein Bootstrap, kein jQuery, keine React-Migration – ausschließlich
  das bestehende Tailwind-v4-/htmx-/Alpine-Setup weiterverwendet.
- Kein zusätzliches `<script src>` in irgendeinem geänderten Template.
- `app.css` wuchs durch die neuen Utilities/Klassen nur geringfügig (neue Utility-Regeln,
  keine neuen Frameworks) – weiterhin eine einzelne, lokal per `tools/tailwindcss.exe`
  kompilierte Datei ohne Laufzeit-Overhead.

## 9. Accessibility

- Fokus-Ringe (`focus-visible:ring-2`) auf allen interaktiven Elementen unverändert/erweitert
  (Buttons, Pills, Inputs, Links).
- Farbe ist nie alleiniger Statusindikator – `badge()` kombiniert immer Text (+ optional
  Icon), nie nur Farbe.
- Bestehende `aria-label`s (Hamburger-Menü, Benachrichtigungs-Glocke, Such-Icon-Buttons)
  unverändert erhalten.
- Kontrastwerte der Farb-Tokens wurden in M12/M13 bereits WCAG-AA-konform gewählt
  (`--color-success-text`/`-warning-text`/`-error-text` als abgedunkelte Badge-Textvarianten)
  – in M14 unverändert übernommen, keine neue Farbe eingeführt.

## 10. Responsive- & Dark-Mode-Test

- Mobile-Viewport (375×812) auf Dashboard, Potenziale und Dokument-Detail geprüft: kein
  horizontales Overflow (`document.documentElement.scrollWidth` == `clientWidth` auf allen
  drei Seiten).
- Sidebar-Mobile-Overlay (Scrim + Slide-in) unverändert, nur bei `lg:`-Breakpoint zusätzlich
  einklappbar – kein Konflikt zwischen Mobile-Show/Hide und Desktop-Collapse, da beide
  unterschiedliche Alpine-Properties (`sidebarOpen` vs. `railCollapsed`) steuern.
- Dark Mode: `.dark`-Klasse manuell gesetzt und verifiziert, dass `body`-Hintergrund
  (`rgb(11,14,20)`) und Card-Border (`rgb(38,44,58)`) exakt den bestehenden Dark-Tokens
  entsprechen – keine invertierte/unpassende Darstellung.

## 11. Qualität

- `manage.ps1 check`-Äquivalent (ruff + volle Testsuite) vor jedem der 10 Commits grün.
- `git diff --name-only` über den gesamten M14-Bereich bestätigt: ausschließlich
  `app/templates/**`, `app/static/css/**` und `docs/M14_COMPLETION_REPORT.md` geändert –
  keine `.py`-Datei, keine Migration.
- Jeder Commit wurde live im Browser verifiziert (gerenderter Text, Konsolenfehler-Check,
  bei Formularen die tatsächliche Filter-/Status-Interaktion end-to-end).
- Alle temporären Verifikations-Server-Skripte (`_manual_verify_server.py`, gitignored)
  nach Gebrauch gelöscht, nie committet.
