# Anruferkennung über Webex Calling (erster Test)

Stand: 11.11.2026. Eine Durchwahl wird genau einem Zentriq-Benutzer zugeordnet. Klingelt sie,
sieht dieser Benutzer im geöffneten Zentriq-Tab einen Hinweis (die geöffnete Seite/Akte bleibt).
Das bestehende Telefonrouting wird **nicht** verändert – Webex meldet Zentriq nur die Anrufzustände.

## Ablauf

```
Webex Calling ──POST (signiert)──▶ https://www.zentriqai.de/telefonie/webex/webhook
                                     │ Signatur prüfen, actorId → Durchwahl → Benutzer
                                     │ callId einmal speichern (Dubletten ignorieren)
                                     ▼
                         Redis ─▶ zentriq-live (Kanal user:<id>, nur Anruf-ID)
                                     ▼
                Browser-Tab holt Treffer bei Zentriq (angemeldet, eigenes Büro)
```

## Was Webex sendet (offizielle Doku)

Quelle: developer.webex.com → *Webhooks* und *Call Control → Webhook Payload Schemas*.

- Ressource `telephony_calls`, Events `created`, `updated`, `deleted`.
- Eingehender Anruf: `data.personality = "terminator"`, `data.state = "alerting"`
  (`data.eventType = "received"`).
- **Wer angerufen wird:** `actorId` (Webex-Person-ID der Durchwahl). Die eigene Rufnummer steht
  nicht im Payload – deshalb ordnet Zentriq über die Person-ID zu.
- **Wer anruft:** `data.remoteParty.number` (Ziffern oder URI) und `.name`. Bei unterdrückter Nummer
  `privacyEnabled = true`, `number`/`name` fehlen → „Unbekannter Anrufer“.
- Angenommen: `state = "connected"`; aufgelegt: `state = "disconnected"` bzw. Event `deleted` →
  Hinweis wird ausgeblendet.
- Signatur: `X-Webex-Signature: SHA-256=<hex>, SHA-512=<hex>` (bevorzugt) bzw.
  `X-Spark-Signature` (HMAC-SHA1) über den unveränderten Rohtext mit dem `secret` der Registrierung.
- Webex kann eine Meldung mehrfach zustellen; die Envelope-`id` ist die Webhook-ID, kein
  Ereignisschlüssel. Zentriq entdoppelt über `callId`.
- Webex deaktiviert Webhooks nach wiederholten Zustellfehlern. Zentriq quittiert deshalb auch
  ignorierte Meldungen (fremde Durchwahl, ausgehende Anrufe) mit 200.

## Berechtigungen

| Variante | Wer registriert | Scope | Hinweis |
|---|---|---|---|
| **A – Test, eine Durchwahl** | der Webex-Calling-Benutzer selbst | `spark:calls_read` | Webhook gehört dem Benutzer (`ownedBy: creator`); `actorId` = dieser Benutzer |
| **B – Betrieb, Org-weit** | Service App, von einem **Full Admin** in Control Hub freigegeben | `spark-admin:calls_read` | `ownedBy: "org"`, mit Filter `personId=…` auf die Test-Durchwahl begrenzbar |

Telephony-Webhooks gibt es nur für Webex-Calling-Benutzer (Multi-Tenant). Ob der Placetel-Tarif
die Webex-Entwickler-APIs freischaltet, muss Placetel/der Webex-Admin bestätigen.

## Einrichtung

### 1. Zentriq (Server, nach Deploy)

1. In `/root/Zentriqai/.env` ergänzen (macht der Betreiber, nicht Claude):
   `WEBEX_WEBHOOK_SECRET=<zufälliger Wert, mind. 32 Zeichen>` – optional `WEBEX_ORG_ID=<Org-ID>`.
   Danach `zentriq-api` neu starten.
2. Webex-Person-ID der Test-Durchwahl ermitteln (Webex-Admin, siehe unten) und zuordnen:
   ```
   .venv/bin/flask telefonie zuordnen --email <zentriq-benutzer> --person-id <ID> --label "Durchwahl 21"
   ```
3. Ohne Webex testen (fiktiver Anruf, Hinweis erscheint im offenen Tab des Benutzers):
   ```
   .venv/bin/flask telefonie testanruf --email <zentriq-benutzer> --nummer "0228 1234567" --ende-nach 15
   .venv/bin/flask telefonie testanruf --email <zentriq-benutzer> --unterdrueckt
   ```
4. Abschalten: `.venv/bin/flask telefonie abschalten --email <zentriq-benutzer>`.

### 2. Webex (Variante A, ohne Adminrechte)

Wichtig: Als `targetUrl` immer `https://www.zentriqai.de/...` eintragen (mit `www`). Die Adresse ohne
`www` leitet mit 301 um, und Webex folgt Umleitungen bei Webhooks nicht zuverlässig.

1. Als Benutzer der Test-Durchwahl auf developer.webex.com anmelden.
2. *API Reference → People → Get My Own Details* → „Run“ → Feld `id` = Person-ID (für Schritt 1.2).
3. *API Reference → Webhooks → Create a Webhook* mit:
   ```json
   {
     "name": "Zentriq Anruferkennung",
     "targetUrl": "https://www.zentriqai.de/telefonie/webex/webhook",
     "resource": "telephony_calls",
     "event": "all",
     "secret": "<derselbe Wert wie WEBEX_WEBHOOK_SECRET>"
   }
   ```
4. Testanruf von einem Handy auf die Durchwahl → Hinweis im Zentriq-Tab.
5. Prüfen/reaktivieren: *Webhooks → List Webhooks* (`status` muss `active` sein).

### 3. Webex (Variante B, Webex-Admin)

1. developer.webex.com → *My Webex Apps → Create a New App → Service App*, Scope
   `spark-admin:calls_read`.
2. Full Admin: Control Hub → *Apps → Service Apps* → App freigeben (authorize).
3. Token der Org abrufen (Service-App-Seite → *Org Authorizations*).
4. Person-ID: `GET https://webexapis.com/v1/people?email=<adresse>` → `id`.
5. Webhook anlegen (`POST https://webexapis.com/v1/webhooks`):
   ```json
   {
     "name": "Zentriq Anruferkennung",
     "targetUrl": "https://www.zentriqai.de/telefonie/webex/webhook",
     "resource": "telephony_calls",
     "event": "all",
     "ownedBy": "org",
     "filter": "personId=<Person-ID der Test-Durchwahl>",
     "secret": "<WEBEX_WEBHOOK_SECRET>"
   }
   ```

## Datenschutz

- Logs: nur gekürzte Hash-Kennung des Anrufs und das Ergebnis – keine Rufnummern, Namen,
  Person-IDs, Secrets oder Signaturen. Fehler werden nur mit Fehlertyp protokolliert.
- Über Redis/zentriq-live geht nur die Anruf-ID; Name und Treffer holt der Browser angemeldet.
- Die Rückrufnotiz wird über `/arbeitsplatz/telefon/anrufe/<id>/rueckrufnotiz` geöffnet – die
  Nummer steht nicht in der URL (nicht im nginx-Access-Log).
- Gespeichert wird je Anruf: Durchwahl, Benutzer, Anrufernummer, Zeitpunkt, Status
  (Tabelle `telephony_calls`). Aufbewahrung: **7 Tage** (`TELEPHONY_CALL_RETENTION_DAYS`, 0 = nie).
  Ältere Anrufe löscht Zentriq bei jedem neuen Anruf automatisch; zusätzlich von Hand:
  `.venv/bin/flask telefonie aufraeumen` (optional `--tage N`). Rückrufnotizen in der Kundenakte
  bleiben davon unberührt.
