"""Automatische Vermittler-Zuordnung: Vorgaenge der Leipziger Liste gehoeren dem Mitarbeiter,
dessen hinterlegte Vermittlernummer (in beliebiger Schreibweise) in der Zeile steht."""

import pytest

from app.models import LeipzigerEntry, UserRole
from app.utils.vermittlernummer import vermittlernummer_key
from tests.test_global_search import _login, _make_list, _make_user, _row


@pytest.mark.parametrize(
    "spelling",
    ["08/4205-M", "8/4205-M", "08/4205M", "084205-M", "084205M", "08 4205 m", "08-4205-M", " 08 / 4205 - M ", "08/4205 M"],
)
def test_spellings_of_the_same_broker_number_share_one_key(spelling):
    assert vermittlernummer_key(spelling) == vermittlernummer_key("08/4205-M") == "084205M"


@pytest.mark.parametrize("other", ["08/4205-N", "08/4206-M", "09/4205-M", "", None, "4205"])
def test_different_or_missing_numbers_do_not_match(other):
    assert vermittlernummer_key(other) != "084205M"


def test_entries_are_assigned_by_normalized_broker_number(app, db, tenant):
    _make_user(db, tenant.id, "admin@example.org", UserRole.OFFICE_ADMIN, "08/0001-A")
    dennis = _make_user(db, tenant.id, "dennis@example.org", UserRole.EMPLOYEE, "08/4205-M")
    _make_user(db, tenant.id, "laura@example.org", UserRole.EMPLOYEE, "08/0950-T")
    document = _make_list(
        db,
        tenant.id,
        [
            _row("1/1-A", "08/4205-M", "Kunde Eins"),
            _row("1/2-A", "084205 m", "Kunde Zwei"),
            _row("1/3-A", "8/4205-M", "Kunde Drei", start="2026-07-01", status="NEU"),
            _row("1/4-A", "08/0950-T", "Kunde Vier"),
        ],
    )

    own = LeipzigerEntry.query.filter_by(document_id=document.id, broker_key=vermittlernummer_key(dennis.vermittlernummer)).all()
    assert sorted(entry.contract_number for entry in own) == ["1/1-A", "1/2-A", "1/3-A"]
    # Einheitliche Anzeige der Nummer unabhaengig von der Schreibweise in der Liste.
    assert {entry.broker_number for entry in own} == {"08/4205-M"}

    # Mitarbeiter sieht ausschliesslich die eigenen Vorgaenge.
    html = _login(app, "dennis@example.org").get("/leipziger-liste?tab=ohne-datum").get_data(as_text=True)
    assert "1/1-A" in html and "1/2-A" in html
    assert "1/4-A" not in html
    html = _login(app, "dennis@example.org").get("/leipziger-liste?tab=mit-datum").get_data(as_text=True)
    assert "1/3-A" in html

    # Buero-Admin sieht das ganze Buero, inklusive Zuordnung zum Namen.
    html = _login(app, "admin@example.org").get("/leipziger-liste?tab=ohne-datum").get_data(as_text=True)
    assert all(number in html for number in ("1/1-A", "1/2-A", "1/4-A"))


def test_broker_number_change_takes_effect_without_reimport(app, db, tenant):
    """Die Zuordnung wird beim Lesen ueber die Nummer ermittelt: wird die Vermittlernummer
    eines Benutzers nachgetragen, sieht er seine Vorgaenge sofort."""
    late = _make_user(db, tenant.id, "spaet@example.org", UserRole.EMPLOYEE, None)
    _make_list(db, tenant.id, [_row("2/1-A", "08/7777-K", "Kunde Spät")])
    client = _login(app, "spaet@example.org")
    assert "2/1-A" not in client.get("/leipziger-liste?tab=ohne-datum").get_data(as_text=True)

    late.vermittlernummer = "087777K"
    db.session.commit()
    assert "2/1-A" in client.get("/leipziger-liste?tab=ohne-datum").get_data(as_text=True)
