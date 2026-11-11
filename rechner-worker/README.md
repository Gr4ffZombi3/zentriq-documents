# Zentriq-Rechner (lokale KI auf dem eigenen Windows-Laptop)

Der Zentriq-Rechner holt KI-Aufträge bei Zentriq ab, bearbeitet sie mit einem lokalen Modell und
meldet das Ergebnis zurück. Die Verbindung geht immer vom Laptop aus (HTTPS zu Zentriq) – zu Hause
müssen keine Ports freigegeben werden, am HUK-PC wird nichts installiert.

Bearbeitet werden nur fest definierte Aufträge des eigenen Büros:

| Auftragsart          | Was passiert                                                     |
|----------------------|------------------------------------------------------------------|
| `kfz_policy_extract` | Kfz-Versicherungsschein (Seitenbilder) auslesen → JSON           |
| `assistant_chat`     | Assistent-Anfrage (Text, ausgelesene Anhänge) ohne Datenzugriff  |

Ist der Laptop aus, warten die Aufträge in Zentriq (bis zu 7 Tage) und laufen automatisch weiter,
sobald der Rechner wieder läuft. Sie werden nie automatisch an Cloud-Modelle gegeben.

## Einrichtung unter Windows

1. **Python installieren** (3.10 oder neuer): https://www.python.org/downloads/windows/ –
   bei der Installation „Add python.exe to PATH“ ankreuzen. Weitere Pakete sind nicht nötig.
2. **Ollama installieren**: https://ollama.com/download/windows. Ollama läuft danach im
   Hintergrund unter `http://127.0.0.1:11434`.
3. **Modell laden** (Eingabeaufforderung):
   `ollama pull <modell>` – welches Modell passt, hängt von RAM, GPU und GPU-Speicher ab
   (siehe unten). Für Versicherungsscheine wird ein Modell mit Bildverständnis gebraucht.
4. **Ordner anlegen**, z. B. `C:\Zentriq\rechner`, und die Dateien aus diesem Ordner hineinkopieren:
   `zentriq_rechner.py`, `zentriq-rechner.ini.beispiel`, `starten.cmd`, `pruefen.cmd`.
5. **Zugang anlegen**: in Zentriq › Verwaltung › **Mein Rechner** › „Zugang anlegen“. Der
   Schlüssel (`zqw_…`) wird nur einmal angezeigt.
6. `zentriq-rechner.ini.beispiel` nach **`zentriq-rechner.ini`** kopieren und eintragen:
   ```ini
   [zentriq]
   adresse = https://www.zentriqai.de
   zugang = zqw_…            ; Schlüssel aus Schritt 5

   [modell]
   schnittstelle = ollama
   adresse = http://127.0.0.1:11434
   modell = <name aus "ollama list">
   ```
   Die Datei enthält den Schlüssel: nicht weitergeben und nicht in OneDrive/Dropbox ablegen.
   Alternativ den Schlüssel als Benutzer-Umgebungsvariable `ZENTRIQ_RECHNER_ZUGANG` setzen und
   `zugang =` leer lassen.
7. **Prüfen**: `pruefen.cmd` doppelklicken. Erwartet:
   `OK: Zentriq erreichbar …` und `OK: Modell bereit: …`.
8. **Starten**: `starten.cmd`. Das Fenster zeigt Auftragsnummer, Art und Dauer (keine Inhalte).
   In Zentriq › Mein Rechner steht dann „Erreichbar“.
9. **Automatisch beim Anmelden starten** (optional): Aufgabenplanung › Einfache Aufgabe erstellen ›
   Trigger „Bei Anmeldung“ › Aktion „Programm starten“ › `C:\Zentriq\rechner\starten.cmd`.

Ohne Modell lässt sich der Ablauf mit `schnittstelle = test` prüfen (Test-Worker: liefert feste
Testantworten, liest nichts aus).

## Modellwahl (erst nach Hardware-Check)

In der Eingabeaufforderung: `systeminfo | findstr /C:"Gesamter physischer Speicher"` und
`dxdiag` › Anzeige › „Anzeigespeicher“ (dedizierter GPU-Speicher). Faustregel: Das Modell muss in
den GPU-Speicher passen, sonst rechnet die CPU (Bilder dauern dann mehrere Minuten – dafür ist
`zeitlimit_sekunden` hoch eingestellt). Das konkrete Modell wird festgelegt, wenn diese Werte
bekannt sind.

## Widerruf

Zentriq › Mein Rechner › „Widerrufen“. Der Rechner kann sofort keine Aufträge mehr abholen und
beendet sich mit der Meldung „Zugang abgelehnt“.
