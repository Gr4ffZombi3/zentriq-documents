# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Hauptbildschirm (ZentriqAI-Feld): Unterscheidung von freier KI-Frage, Kundensuche und Notiz
anhand der Beispiele aus dem Auftrag - ueber echte HTTP-Requests, synthetische Daten, ohne
externe KI-Aufrufe."""

from tests.test_customer_notes import API, _contacts, _input, _notes, ai_available, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


def test_examples_from_the_main_screen(app, office, ai_available):
    laura = login(app, "laura@example.com")
    assert _input(laura, "Hallo, wobei kannst du mir helfen?")["kind"] == "chat"

    r = _input(laura, "808/123456-e")
    assert r["kind"] == "card" and r["card"]["name"] == "Justin Heller"
    assert _notes() == []

    created = _input(laura, "808/765432-e Max Mustermann HUK24 Wechsel", client_ref="max-1")
    assert created["kind"] == "saved" and created["created"] is True, created
    r = _input(laura, "Was wurde bei Max Mustermann notiert?")
    assert r["kind"] == "card" and [s["text"] for s in r["answer"]["sources"]] == ["HUK24 Wechsel"]

    for phone in ("0171 1234567", "01711234567"):
        r = _input(laura, f"Was hat {phone} gesagt?")
        assert r["kind"] == "card" and r["card"]["name"] == "Justin Heller", r
        assert r["answer"]["text"] == "Zu Justin Heller ist keine Gesprächsnotiz dokumentiert."
    assert len(_notes()) == 1


def test_search_questions_with_selected_customer_save_nothing(app, office, ai_available):
    laura = login(app, "laura@example.com")
    heller, schulz = office.ids["Justin Heller"], office.ids["Maria Schulz"]
    for text in ("Was wurde bei Justin Heller notiert?", "Was ist offen?", "808/111111-B", "Maria Schulz",
                 "Was hat Maria Schulz gesagt?", "Hallo, wobei kannst du mir helfen?"):
        r = _input(laura, text, selected_id=heller)
        assert r["kind"] in ("card", "chat"), (text, r)
    assert _notes() == [] and _contacts() == []
    # Ausdruecklich genannter anderer Kunde als die Auswahl: erst nachfragen, nichts gespeichert;
    # nach der Wahl wird beim gewaehlten Kunden gespeichert.
    r = _input(laura, "808/111111-B ruft morgen zurück", selected_id=heller)
    assert r["kind"] == "conflict" and [c["id"] for c in r["candidates"]] == [schulz, heller]
    assert _notes() == []
    r = _input(laura, r["pending_note"], customer_id=schulz)
    assert r["kind"] == "saved" and r["card"]["id"] == schulz


def test_question_about_unknown_customer_is_not_found_not_ai(app, office, ai_available):
    laura = login(app, "laura@example.com")
    heller = office.ids["Justin Heller"]
    for selected in (None, heller):
        r = _input(laura, "Was wurde bei Erika Unbekannt notiert?", selected_id=selected)
        assert r["kind"] == "not_found" and r["search"] is True and "Erika Unbekannt" in r["message"], r
        assert _input(laura, "Was hat Erika Unbekannt gesagt?", selected_id=selected)["kind"] == "not_found"
    # Allgemeine Fragen bleiben beim KI-Modell, Mitarbeiter-Vornamen sind keine Kunden.
    assert _input(laura, "Was ist eine Teilkaskoversicherung?")["kind"] == "chat"
    assert _input(laura, "Wie spät ist es in New York?")["kind"] == "chat"
    assert _input(laura, "Was hat Dennis notiert?")["kind"] != "not_found"


def test_repeated_note_is_reported_as_already_saved(app, office):
    laura = login(app, "laura@example.com")
    first = _input(laura, "808/123456-e HUK24 Wechsel", client_ref="a")
    again = _input(laura, "808/123456-e HUK24 Wechsel", client_ref="b")
    assert first["message"] == "Bei Justin Heller gespeichert: HUK24 Wechsel"
    assert again["duplicate"] is True and again["message"].startswith("Bereits gespeichert bei Justin Heller")
    assert len(_notes()) == 1


def test_callbacks_report_total(app, office):
    laura = login(app, "laura@example.com")
    _input(laura, "808/123456-e nicht erreicht")
    _input(laura, "808/111111-B ruft zurück wegen Angebot")
    data = laura.get(API + "/rueckrufe").get_json()
    assert data["total"] == 2 and len(data["callbacks"]) == 2


def test_main_screen_markup_is_reduced(app, office):
    html = login(app, "laura@example.com").get("/zentriq").get_data(as_text=True)
    assert "Frage stellen, Kunden suchen oder Notiz erfassen …" in html
    assert "zc-suggestion" not in html
    assert '<details class="zn" name="zn-bar" data-zn' in html and "Offene Rückrufe" in html
