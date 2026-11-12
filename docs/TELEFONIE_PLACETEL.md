# Anruferkennung über Placetel Notify

Stand: 12.11.2026. Placetel meldet Zentriq jeden Anruf („Anruf an externe API melden“). Zentriq zeigt
dem zugeordneten Benutzer im geöffneten Tab, wer anruft (ein Treffer, Auswahl, Rückrufnotiz,
„Unbekannter Anrufer“). Die geöffnete Seite/Akte bleibt.

**Das Routing bleibt unverändert:** Im Notify-Modus wertet Placetel die Antwort von Zentriq nicht aus –
das Sprachmenü kommt weiterhin bei jedem Anruf zuerst. Zentriq antwortet zusätzlich nur mit reinem
Text „OK“ (nie mit Routing-XML). Die Routing-Art „Externe API“ (Call Control) wird **nicht** benutzt.

Quelle: offizielle Doku <https://github.com/Placetel/call-control-notify-api>.

## Empfangs-URL

```
https://www.zentriqai.de/telefonie/placetel/notify
```

Mit `www` eintragen – ohne `www` leitet der Server um, und Weiterleitungen verlieren den POST.

## Was Placetel sendet

`POST`, `application/x-www-form-urlencoded`, je Ereignis:

| event | Felder | Zentriq |
|---|---|---|
| `IncomingCall` | `from` (Anrufer oder `anonymous`), `to` (angerufene Nummer), `call_id`, `direction=in` | Hinweis für alle Benutzer, denen `to` zugeordnet ist |
| `CallAccepted` | zusätzlich `peer` (annehmender SIP-Benutzer) | Hinweis bleibt beim annehmenden Benutzer (bzw. erscheint dort, wenn nur der SIP-Benutzer zugeordnet ist); bei den übrigen „Anruf beendet“ |
| `HungUp` | `type` (missed, accepted, voicemail …), `duration` | alle Hinweise des Anrufs ausblenden |
| `OutgoingCall` | – | ignoriert |

Signatur: Header `X-PLACETEL-SIGNATURE` = HMAC-SHA256 (hex) über den unveränderten Rohtext mit dem
gemeinsamen Secret aus *Einstellungen → Externe APIs*. Ohne gültige Signatur antwortet Zentriq 401,
ohne konfiguriertes Secret 503. Doppelte Meldungen lösen keinen zweiten Hinweis aus.

**Sprachmenü:** Placetel meldet nicht, welche Durchwahl hinter dem Sprachmenü klingelt. Deshalb
zwei Zuordnungsarten (einzeln oder kombiniert):

- **angerufene Nummer** (`to`): Hinweis schon während das Sprachmenü läuft bzw. es klingelt – an alle
  Benutzer, denen diese Nummer zugeordnet ist (Sammelnummer möglich);
- **SIP-Benutzer** (`peer`): Hinweis spätestens, wenn dieser Benutzer annimmt.

## Einrichtung

### 1. Zentriq (Server)

1. In `/root/Zentriqai/.env` ergänzen (macht der Betreiber, nicht Claude):
   `PLACETEL_NOTIFY_SECRET=<zufälliger Wert, mind. 32 Zeichen>` – danach `zentriq-api` neu starten.
2. Benutzer zuordnen (Nummer wie in Placetel, SIP-Benutzer aus den SIP-Einstellungen):
   ```
   .venv/bin/flask telefonie zuordnen-placetel --email <zentriq-benutzer> --nummer "0228 …" --sip "777…@fpbx.de" --label "Durchwahl 21"
   .venv/bin/flask telefonie liste
   ```
3. Ohne Placetel testen (fiktiver, signierter Anruf durch denselben Endpunkt):
   ```
   .venv/bin/flask telefonie testanruf-placetel --nummer-angerufen "0228 …" --anrufer "0228 1234567" --sip "777…@fpbx.de" --ende-nach 15
   ```
4. Abschalten: `.venv/bin/flask telefonie abschalten --email <zentriq-benutzer>`.

### 2. Placetel-Webportal

1. *Einstellungen → Externe APIs*: Feld **URL** = `https://www.zentriqai.de/telefonie/placetel/notify`,
   **Secret** = derselbe Wert wie `PLACETEL_NOTIFY_SECRET`. Speichern.
2. Pro Rufnummer aktivieren: dort die Checkbox der Nummer setzen **oder** in den Routing-Einstellungen der
   Nummer im Reiter **Sonstiges** „Anruf an externe API melden“ anhaken.
   Die Routing-Art der Nummer (Sprachmenü) **nicht** auf „Externe API“ umstellen.
3. Testanruf vom Handy → Hinweis im Zentriq-Tab. Placetel zeigt unter *Externe APIs* die letzten
   Antworten – erwartet: HTTP 200, Text „OK“.

Hinweis laut Placetel: Notify gehört zur PROFI-Produktlinie.

## Datenschutz

Wie bei Webex (docs/TELEFONIE_WEBEX.md): Logs nur mit gekürzten Hash-Kennungen, nie Rufnummern,
SIP-Benutzer, call_id oder Secrets; über den Live-Dienst geht nur die Anruf-ID; Anrufe werden nach
`TELEPHONY_CALL_RETENTION_DAYS` (7 Tage) gelöscht.
