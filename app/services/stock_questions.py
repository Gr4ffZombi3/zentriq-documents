"""Bestandsfragen direkt aus der Datenbank - ohne KI-Modell.

"Wie viele Kunden sind laut Leipziger Liste noch zurückzurufen?", "Wie viele Rückrufe sind
offen?", "Was ist in der Leipziger Liste noch zu erledigen?"

Regeln (dieselben wie in der Oberflaeche):
- Leipziger Liste "Zu erledigen" (app/services/leipziger_todo.py): Zeilen der aktuellen Liste
  OHNE Beginn-Datum, nicht storniert. Mitarbeiter sehen nur Zeilen ihrer Vermittlernummer,
  Buero-Admins alle Zeilen des Bueros. Gezaehlt werden Vorgaenge und verschiedene Kunden.
- Offene Rueckrufe (Kundennotizen): CustomerContact mit Status "open" im eigenen Buero.
Ergebnis: Anzahl, kurze Liste der ersten Eintraege und Link auf die passende Ansicht."""

from __future__ import annotations

import re

from flask import url_for

_COUNT = re.compile(r"\b(?:wie\s*viele|wieviele|anzahl|wie\s+viel)\b", re.I)
_OPEN_WORDS = re.compile(r"\b(?:zur(?:ü|ue)ck(?:zu)?ruf\w*|r(?:ü|ue)ckruf\w*|anzurufen|offen\w*|zu\s+erledigen|ausstehend\w*|noch\s+zu\s+tun)\b", re.I)
_LEIPZIGER = re.compile(r"\bleipziger\b|\bliste\b|\bvorg(?:ä|ae)ng\w*\b", re.I)
_CALLBACK = re.compile(r"\br(?:ü|ue)ckruf\w*|\bzur(?:ü|ue)ck(?:zu)?ruf\w*|\banzurufen\b", re.I)
_LIST_WHAT = re.compile(r"^\s*(?:was|welche)\b.*\b(?:leipziger|liste)\b.*\b(?:offen|zu\s+erledigen|zur(?:ü|ue)ck)", re.I)
MAX_LINES = 8


def detect(text: str) -> str | None:
    """'leipziger' | 'callbacks' | None. Nur eindeutige, kurze Zaehl-/Bestandsfragen."""
    t = " ".join((text or "").split())
    if not t or len(t) > 200:
        return None
    asks = bool(_COUNT.search(t)) or bool(_LIST_WHAT.search(t))
    if not asks or not _OPEN_WORDS.search(t):
        return None
    if _LEIPZIGER.search(t):
        return "leipziger"
    if _CALLBACK.search(t):
        return "callbacks"
    return None


def _leipziger(user) -> dict:
    from app.services.ai.tools import _todo_sorted, _visible_entries

    document, visible = _visible_entries(user)
    if document is None:
        return {"kind": "facts", "message": "Es ist noch keine Leipziger Liste importiert.", "lines": []}
    todo = _todo_sorted(visible)
    customers = {(e.customer or e.number).strip().lower() for e in todo}
    scope = "Ihrer Vermittlernummer" if not user.is_office_admin else "des Büros"
    n_customers, n_entries = len(customers), len(todo)
    message = (
        f"Leipziger Liste ({document.original_filename}): {n_customers} {'Kunde' if n_customers == 1 else 'Kunden'} mit "
        f"{n_entries} {'offenem Vorgang' if n_entries == 1 else 'offenen Vorgängen'} „Zu erledigen“ ({scope})."
    )
    return {
        "kind": "facts",
        "message": message,
        "rule": "Gezählt: Zeilen ohne Beginn-Datum, nicht storniert.",
        "lines": [f"{e.customer or '–'} · {e.number} · {e.label}" for e in todo[:MAX_LINES]],
        "more": max(0, len(todo) - MAX_LINES),
        "link": {"url": url_for("leipziger.index", tab="zu-erledigen"), "label": "Alle in der Leipziger Liste öffnen"},
        "count": len(customers),
        "entries": len(todo),
    }


def _callbacks(user) -> dict:
    from app.services.customer_notes import open_callback_count, open_callbacks

    total = open_callback_count()
    items = open_callbacks(limit=MAX_LINES)
    return {
        "kind": "facts",
        "message": f"Offene Rückrufe: {total}." if total else "Es sind keine Rückrufe offen.",
        "rule": "Gezählt: Rückrufvermerke aus Kundennotizen mit Status „offen“ (Büro).",
        "lines": [f"{c['customer']['name']} · {c.get('topic') or 'Anlass nicht angegeben'}" for c in items],
        "more": max(0, total - len(items)),
        "callbacks": True,
        "count": total,
    }


def answer(user, text: str, *, selected_id=None) -> dict | None:
    kind = detect(text)
    if kind is None or not (user.is_office_admin or user.is_employee):
        return None
    if selected_id not in (None, ""):
        return None  # Frage bezieht sich auf den ausgewaehlten Kunden (customer_notes)
    from app.services.customer_notes import _by_name

    if _by_name(text):
        return None  # "Wie viele Rueckrufe hat Justin Heller offen?" -> kundenbezogene Antwort
    return _leipziger(user) if kind == "leipziger" else _callbacks(user)
