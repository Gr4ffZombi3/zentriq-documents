"""ZentriqAI-Orchestrierung: Anfrage -> Absicht -> berechtigte Daten -> kurze Antwort.

1. Direktantwort (kein KI-Aufruf): Eindeutige Standardfragen ("Was ist heute offen?",
   "Meine Arbeitszeiten diese Woche", "Wer ist da?", "Offene Aufgaben") werden ueber das passende
   Werkzeug beantwortet und lokal formatiert. Kostet keine Tokens, funktioniert ohne Anbieter und
   ohne dass Daten den Server verlassen.
2. Sonst: Modell mit den fuer den Benutzer erlaubten Werkzeugen. Das Modell waehlt selbst, welche
   Daten es braucht; Rechte prueft jedes Werkzeug serverseitig. Einfache Anfragen gehen an
   kleine, komplexe an starke Modelle (kostenlose immer zuerst).
3. Datenschutz: Ist ein fuer Kundendaten freigegebener Anbieter verfuegbar, gehen Buerodaten nur
   dorthin. Sonst wird alles Personenbezogene vor der Anfrage pseudonymisiert und erst lokal
   wieder eingesetzt (privacy.Pseudonymizer).
"""

from __future__ import annotations

import re

from app.services.ai import tools

DIRECT_PROVIDER = "zentriq"
DIRECT_MODEL = "direkt"
DIRECT_LABEL = "Direkt aus Zentriq"

_COMPLEX_WORDS = re.compile(
    r"\b(analysier|auswert|vergleich|zusammenfass|fasse|erstell|schreib|formulier|erklär|erklaer|plan|warum|begründ|bewert|entwurf|prüfe|pruefe)",
    re.I,
)
# Woerter, die eine reine Datenabfrage zu einer Bearbeitungsaufgabe machen -> Modell noetig.
_NOT_DIRECT = re.compile(r"\b(warum|wieso|formulier|schreib|erklär|erklaer|vergleich|analysier|zusammenfass|fasse|mail|brief|entwurf)", re.I)


def is_complex(text: str, has_attachments: bool, has_tools: bool) -> bool:
    return has_attachments or has_tools or len(text) > 400 or bool(_COMPLEX_WORDS.search(text))


# --- Direktantworten ------------------------------------------------------------------------


def _period(text: str) -> str:
    if re.search(r"\bheute\b", text):
        return "heute"
    if re.search(r"\bmonat", text):
        return "monat"
    return "woche"


def detect_intent(user, text: str) -> tuple[str, dict] | None:
    """Erkennt nur eindeutige, kurze Datenabfragen. Im Zweifel None -> Modell."""
    t = " ".join((text or "").lower().split())
    if not t or len(t) > 140 or _NOT_DIRECT.search(t):
        return None
    allowed = {tool.name for tool in tools.available_for(user)}

    def ok(name):
        return name in allowed

    if re.search(r"\b(meine|unsere)? ?arbeitszeit(en)?\b|wie (lange|viel) (habe|hab) ich .*gearbeitet|\bmeine stunden\b", t) and ok("meine_arbeitszeiten"):
        return "meine_arbeitszeiten", {"zeitraum": _period(t)}
    if re.search(r"\bwer ist (heute |gerade |jetzt )?(da|anwesend|im büro|im buero|eingestempelt)\b|\banwesenheit\b", t) and ok("team_anwesenheit"):
        return "team_anwesenheit", {}
    if re.search(r"\b(heute )?erledigte aufgaben\b|\bwelche aufgaben (wurden|sind) (heute )?erledigt\b", t) and ok("aufgaben"):
        return "aufgaben", {"status": "heute_erledigt"}
    if re.search(r"\boffenen? aufgaben\b|\bwelche aufgaben sind (noch )?offen\b", t) and ok("aufgaben"):
        return "aufgaben", {"status": "offen"}
    if re.search(
        r"\bwas (ist|steht|muss|liegt) (heute |noch |heute noch )*(offen|an|zu tun|erledigt werden)\b|\btagesüberblick\b|"
        r"\bwichtigsten vorgänge\b|\bwas gibt es heute\b|\bwas ist heute wichtig\b",
        t,
    ) and ok("tagesueberblick"):
        return "tagesueberblick", {}
    return None


def _md_escape(value) -> str:
    return str(value if value not in (None, "") else "–").replace("|", "/")


def _entry_line(e: dict) -> str:
    parts = [e.get("art") or "Vorgang", f"Vertrag {e.get('vertrag')}"]
    if e.get("kunde"):
        parts.append(e["kunde"])
    return " – ".join(parts)


def _task_line(t: dict) -> str:
    from app.services.timetracking.clock import local_today

    parts = [t.get("titel") or "Aufgabe"]
    if t.get("kunde"):
        parts.append(t["kunde"])
    if t.get("faellig"):
        parts.append("heute fällig" if t["faellig"] == local_today().strftime("%d.%m.%Y") else f"fällig {t['faellig']}")
    return " – ".join(parts)


def format_direct(name: str, data: dict) -> str:
    if data.get("fehler"):
        return data["fehler"]
    if data.get("hinweis") and len(data) == 1:
        return data["hinweis"]
    if name == "meine_arbeitszeiten":
        lines = [
            f"**{data['zeitraum']}:** {data['netto_gesamt']} Std. netto (Soll {data['soll_gesamt']}, Pausen {data['pausen_gesamt']}). "
            f"Aktuell {data['status_jetzt']}."
        ]
        if data["tage"]:
            lines.append("\n| Tag | Beginn | Ende | Pausen | Netto |\n|---|---|---|---|---|")
            for d in data["tage"]:
                begin = (d["beginn"] or "–")[-5:]
                end = d["ende"] if d["ende"] == "läuft" else (d["ende"] or "–")[-5:]
                lines.append(f"| {d['datum'][:6]} | {begin} | {end} | {d['pausen']} | {d['netto']} |")
        else:
            lines.append("\nIn diesem Zeitraum gibt es noch keine Buchungen.")
        return "\n".join(lines)
    if name == "team_anwesenheit":
        people = data.get("mitarbeiter") or []
        present = [p for p in people if p["status"] == "anwesend"]
        lines = [f"**{len(present)} von {len(people)}** Mitarbeitern sind anwesend."]
        for p in people:
            lines.append(f"- {p['name']}: {p['status']} ({p['heute_netto']} Std. heute)")
        return "\n".join(lines)
    if name == "aufgaben":
        tasks = data.get("aufgaben") or []
        if not tasks:
            return "Keine Aufgaben gefunden."
        lines = [f"**{data['anzahl']} Aufgaben.**" + (" Die wichtigsten:" if data["anzahl"] > len(tasks) else "")]
        lines += [f"{i}. {_task_line(t)}" for i, t in enumerate(tasks[:10], 1)]
        if data["anzahl"] > 10:
            lines.append(f"\n… und {data['anzahl'] - 10} weitere unter Leipziger Liste → Aufgaben.")
        return "\n".join(lines)
    if name == "tagesueberblick":
        lines = []
        liste = data.get("leipziger_liste")
        aufgaben = data.get("aufgaben")
        if liste:
            text = f"Heute sind noch **{liste['zu_erledigen']} Vorgänge** der Leipziger Liste offen"
            text += f", davon **{liste['klaerungsbedarf']} mit Klärungsbedarf**." if liste["klaerungsbedarf"] else "."
            lines.append(text)
        if aufgaben:
            lines.append(f"**{aufgaben['offen']} Aufgaben** sind offen, {aufgaben['faellig_bis_heute']} davon bis heute fällig.")
        extras = []
        if data.get("offene_korrekturantraege"):
            extras.append(f"{data['offene_korrekturantraege']} offene Korrekturanträge")
        if data.get("sprachnachrichten_heute"):
            extras.append(f"{data['sprachnachrichten_heute']} neue Sprachnachrichten heute")
        if extras:
            lines.append("Außerdem: " + ", ".join(extras) + ".")
        important = []
        if aufgaben:
            important += [_task_line(t) for t in aufgaben["wichtigste"][:3]]
        if liste:
            important += [_entry_line(e) for e in liste["wichtigste"][: 5 - len(important)]]
        if important:
            lines.append("\n**Die wichtigsten:**")
            lines += [f"{i}. {item}" for i, item in enumerate(important, 1)]
        if not liste:
            lines.append("\nEs ist noch keine Leipziger Liste importiert.")
        return "\n".join(lines).strip()
    return tools.to_text(data)


def direct_answer(user, text: str) -> tuple[str, list[str]] | None:
    intent = detect_intent(user, text)
    if intent is None:
        return None
    name, args = intent
    data = tools.run(user, name, args)
    if data.get("fehler"):
        return None  # lieber das Modell (oder eine klare Fehlermeldung) als eine halbe Antwort
    return format_direct(name, data), [name]
