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


_EARNED = re.compile(r"\b(?:verdien\w*|verdienst|provision\w*|abrechnung\w*|courtage|einnahmen|10[- ]?(?:€|euro)[- ]?positionen)\b", re.I)


def detect(text: str) -> str | None:
    """'settlement' | 'leipziger' | 'callbacks' | None. Nur eindeutige, kurze Zaehl-/Bestandsfragen."""
    t = " ".join((text or "").split())
    if not t or len(t) > 200:
        return None
    if _EARNED.search(t) and re.search(r"\b(?:wie\s*viel\w*|was\s+hat|wieviel|summe|zeige?|übersicht|uebersicht)\b", t, re.I):
        return "settlement"
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


def _month_from(text: str):
    from app.services import settlements

    available = settlements.periods()
    m = re.search(r"\b(" + "|".join(settlements.MONTHS) + r")\b(?:\s+(\d{4}))?", text, re.I)
    if m:
        month = settlements.MONTHS[m.group(1).lower()]
        if m.group(2):
            return int(m.group(2)), month
        years = [y for y, mo in available if mo == month]
        return (years[0], month) if years else None
    if re.search(r"\b(?:letzten|vorigen|vergangenen)\s+monat", text, re.I):
        from datetime import date

        today = date.today()
        return (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return available[0] if available else None


def _settlement(user, text: str) -> dict:
    """Abrechnungsdaten: ausschliesslich fuer den Buero-Admin, direkt aus der Datenbank."""
    if not user.is_office_admin:
        return {"kind": "facts", "message": "Abrechnungsdaten sieht ausschließlich der Büro-Admin.", "lines": []}
    from app.services import calendar as cal
    from app.services import settlements
    from app.services.calendar_intent import _members_matching

    period = _month_from(text)
    if period is None:
        return {"kind": "facts", "message": "Für diesen Zeitraum liegen keine ausgewerteten Abrechnungen vor.", "lines": []}
    year, month = period
    members = cal.office_members()
    named = []
    for word in re.findall(r"\b[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?\b", text):
        found = _members_matching(word, members)
        if len(found) == 1 and found[0] not in named:
            named.append(found[0])
    exclude_ten = bool(re.search(r"\bohne\s+10", text, re.I))
    rows = settlements.summary(year, month, exclude_ten=exclude_ten)
    if named:
        rows = [r for r in rows if r.user_id in {u.id for u in named}]
    label = settlements.month_label(year, month)
    if not rows:
        who = ", ".join(cal.member_name(u) for u in named) or "das Büro"
        return {"kind": "facts", "message": f"Für {who} liegen für {label} keine ausgewerteten Abrechnungen vor.", "lines": []}

    def money(v):
        return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")

    lines = []
    for r in rows:
        orders = "Aufträge aus der Abrechnung nicht ermittelbar" if r.orders is None else f"{r.orders} Aufträge"
        lines.append(f"{r.name}: {money(r.total)} gesamt (regulär {money(r.regular)}, 10-€-Positionen {money(r.ten_sum)}; "
                     f"Stornos {money(r.storno_sum)}) · {orders}" + (f" · {r.unclear} unklare Zeile(n) offen" if r.unclear else ""))
    total = sum((r.total for r in rows), start=0)
    message = f"Verdient laut Abrechnung, {label}" + (" (ohne 10-€-Positionen)" if exclude_ten else "") + f": {money(total)}."
    return {
        "kind": "facts", "message": message, "rule": "Summe der gezählten Buchungen (unklare Zeilen erst nach Prüfung).",
        "lines": lines, "link": {"url": url_for("settlements.index", monat=f"{year:04d}-{month:02d}"), "label": "Abrechnungen öffnen"},
        "count": len(rows),
    }


def answer(user, text: str, *, selected_id=None) -> dict | None:
    kind = detect(text)
    if kind is None or not (user.is_office_admin or user.is_employee):
        return None
    if kind == "settlement":
        return _settlement(user, text)
    if selected_id not in (None, ""):
        return None  # Frage bezieht sich auf den ausgewaehlten Kunden (customer_notes)
    from app.services.customer_notes import _by_name

    if _by_name(text):
        return None  # "Wie viele Rueckrufe hat Justin Heller offen?" -> kundenbezogene Antwort
    return _leipziger(user) if kind == "leipziger" else _callbacks(user)
