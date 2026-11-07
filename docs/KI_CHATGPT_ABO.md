# ChatGPT-Abo ("Sign in with ChatGPT") für Zentriq – Prüfung 07.11.2026

Quelle: offizielle OpenAI-Entwicklerdokumentation unter https://developers.openai.com/siwc
(Übersicht, Request a client ID, ChatGPT plan usage / open-source, Preview limitations,
Accounts and sessions).

## Ergebnis

Zentriq kann das ChatGPT-Plus-Abo derzeit **nicht** offiziell für Modellaufrufe nutzen.
Es wurde deshalb keine Integration gebaut, und es wurden keine Zugangsdaten angelegt.

| Frage | Stand laut Dokumentation |
|---|---|
| Reine Anmeldung (Identität) | „Sign in with ChatGPT is currently offered to a select group of commercial partners.“ Zugang nur über die Warteliste (Interessenformular). |
| Nutzung des ChatGPT-Plans für KI-Anfragen | Dokumentiert für Open-Source- und lokal betriebene Apps. Bezahlte oder entfernt gehostete Apps sollen das Interessenformular ausfüllen. |
| Unsere Lage | Zentriq ist proprietär, läuft auf einem Server und wird gewerblich von mehreren Mitarbeitern genutzt. Damit fällt es in die Gruppe „remotely hosted / paid“ und ist **nicht selbst registrierbar**. |
| Ein Abo für alle Mitarbeiter | Nicht vorgesehen. Ein Client ist an „the authenticated user and the workspace“ gebunden, und jeder Nutzer stimmt selbst zu. Ein persönliches Plus-Abo für das ganze Büro wäre eine gemeinsame Nutzung einer persönlichen Anmeldung. |
| Technische Einschränkungen (Vorschau) | Nur Responses API mit `store: false` und `stream: true`. Explizite Systemnachrichten werden abgelehnt. Keine Dateien, kein Audio, keine Transkription, keine gehosteten Werkzeuge. Für Plus-Nutzer gilt ein gemeinsames 5-Stunden-Limit über alle Apps. |

## Falls eine Anbindung gewünscht ist

1. Interessenformular ausfüllen: https://openai.com/form/sign-in-with-chatgpt-interest/
   (Das kann nur der Inhaber tun; es wurde nichts eingereicht.)
2. Erst nach einer Zusage von OpenAI: Client-ID und Callback-URL (`https://www.zentriqai.de/...`)
   registrieren. Jeder Mitarbeiter meldet sich dann mit seinem **eigenen** ChatGPT-Konto an.
3. Dafür wäre ein neuer Anbieter-Adapter für die Responses API nötig (Vorgaben siehe oben). Er
   würde je Benutzer einen eigenen OAuth-Token verschlüsselt speichern, Scopes und Zustimmung
   prüfen und ein Trennen über die ChatGPT-Einstellungen unterstützen.

Ausdrücklich ausgeschlossen: Browser-Cookies, kopierte Sitzungstokens und eine gemeinsame
Anmeldung für alle Mitarbeiter.

## Offiziell nutzbare Alternative (kostenpflichtig, nicht aktiviert)

Den OpenAI-API-Zugang (Plattform, Abrechnung pro Token) unterstützt Zentriq bereits als Anbieter
„openai“ (`OPENAI_API_KEY`). Kostenpflichtige Modelle bleiben gesperrt, solange
`ZENTRIQ_AI_DAILY_BUDGET_USD` und `ZENTRIQ_AI_MONTHLY_BUDGET_USD` auf 0 stehen. Das wurde nicht
geändert.
