# ruff: noqa: F811
"""Webrecherche: Erkennung, Datenschutz der Suchanfrage, Quellen in der Antwort, fehlende Suche.
Netzwerkzugriffe sind ersetzt (echte HUK-Pruefung: manuell/Oberflaechentest)."""

from types import SimpleNamespace

from app.services import web_research as w
from app.tenancy import use_tenant_id
from tests.test_customer_notes import office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


def test_detection():
    assert w.is_question("Zahlt die HUK Hausratversicherung bei Fahrraddiebstahl?")
    assert w.is_question("Recherchiere die aktuelle Kündigungsfrist für Kfz-Versicherungen")
    assert not w.is_question("Wie viel hat Laura im Oktober verdient?")
    assert not w.is_question("Justin Heller")
    assert not w.is_question("Was ist in der Leipziger Liste noch offen?")


def test_public_query_removes_internal_data(app, office):
    from app.models import User

    with use_tenant_id(office.tenant_a.id):
        admin = User.query.filter_by(email="admin-a@example.com").one()
        q = w.public_query(admin, "Ist bei Justin Heller (808/123456-E-12, Tel. 0171 1234567, justin@example.de) Fahrraddiebstahl in der HUK Hausrat versichert? Laura fragt, 85,40 €")
    for leaked in ("Justin", "Heller", "808", "0171", "@", "Laura", "85,40"):
        assert leaked not in q, (leaked, q)
    assert "Fahrraddiebstahl" in q and "Hausrat" in q


def test_answer_links_opened_sources_and_distinguishes_contract(app, office, monkeypatch):
    from app.models import User
    from app.services.ai import router

    sources = [w.Source("https://www.huk.de/haus-haftung-recht/haus-wohnung/hausratversicherung.html", "Hausratversicherung", "Fahrraddiebstahl ist mit Baustein versichert."),
               w.Source("https://static.c.huk.de/avb_hausrat.pdf", "avb_hausrat.pdf", "§ 5 Fahrraddiebstahl ...", kind="Bedingungen (PDF)")]
    seen = {}
    monkeypatch.setattr(w, "search_huk", lambda q: seen.setdefault("q", q) and sources)
    monkeypatch.setattr(router, "can_answer", lambda: True)
    monkeypatch.setattr(router, "complete", lambda **k: SimpleNamespace(text="Nur mit Fahrrad-Baustein versichert [1][2]. Individueller Vertrag kann abweichen."))
    with use_tenant_id(office.tenant_a.id):
        admin = User.query.filter_by(email="admin-a@example.com").one()
        reply = w.handle(admin, SimpleNamespace(tenant_id=office.tenant_a.id, model_choice=None, id=1), "Zahlt die HUK Hausrat bei Fahrraddiebstahl von Justin Heller?")
    assert "**Geprüfte Quellen**" in reply and "(https://www.huk.de/haus-haftung-recht/haus-wohnung/hausratversicherung.html)" in reply
    assert "Bedingungen (PDF)" in reply and "Heller" not in seen["q"]


def test_without_model_shows_source_excerpts(app, office, monkeypatch):
    from app.models import User

    monkeypatch.setattr(w, "search_huk", lambda q: [w.Source("https://www.huk.de/x.html", "X", "Der Fahrraddiebstahl ist ab 1.000 Euro über den Baustein Fahrrad versichert.")])
    with use_tenant_id(office.tenant_a.id):
        admin = User.query.filter_by(email="admin-a@example.com").one()
        reply = w.handle(admin, SimpleNamespace(tenant_id=office.tenant_a.id, model_choice=None, id=1), "Ist Fahrraddiebstahl in der HUK Hausratversicherung versichert?")
    assert "Kein KI-Modell" in reply and "Fahrraddiebstahl" in reply and "https://www.huk.de/x.html" in reply


def test_general_search_not_configured_is_reported(app, office):
    from app.models import User

    app.config["GOOGLE_SEARCH_API_KEY"] = None
    with use_tenant_id(office.tenant_a.id):
        admin = User.query.filter_by(email="admin-a@example.com").one()
        reply = w.handle(admin, SimpleNamespace(tenant_id=office.tenant_a.id, model_choice=None, id=1), "Recherchiere die aktuelle Regelung zur Grundsteuer")
    assert "nicht eingerichtet" in reply and "keine Antwort aus dem Internet" in reply


def test_no_sources_without_explicit_request_falls_back_to_chat(app, office, monkeypatch):
    """Produktionsfall 09.11.2026: Fachfrage ohne gefundene Quelle endete mit einer Warnung statt
    einer normalen Chat-Antwort."""
    from app.models import User

    app.config["GOOGLE_SEARCH_API_KEY"] = None
    monkeypatch.setattr(w, "search_huk", lambda query: [])
    with use_tenant_id(office.tenant_a.id):
        admin = User.query.filter_by(email="admin-a@example.com").one()
        reply = w.handle(admin, SimpleNamespace(tenant_id=office.tenant_a.id, model_choice=None, id=1),
                         "Was ist der Unterschied zwischen Teilkasko und Vollkasko?")
    assert reply is None
