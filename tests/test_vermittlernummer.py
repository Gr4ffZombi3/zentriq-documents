"""Zentrale Normalisierung der Vermittlernummer und ihre Verwendung bei Login/Eindeutigkeit."""

import pytest

from app.utils.vermittlernummer import format_vermittlernummer, same_vermittlernummer, vermittlernummer_key


@pytest.mark.parametrize("value", ["08/0950-T", "080950-T", "080950T", " 08/0950-t ", "08 / 0950 - T", "8/0950-T"])
def test_spellings_of_same_number_are_equal(value):
    assert vermittlernummer_key(value) == "080950T"
    assert format_vermittlernummer(value) == "08/0950-T"
    assert same_vermittlernummer(value, "08/0950-T")


def test_different_numbers_do_not_match():
    assert not same_vermittlernummer("08/0950-T", "08/0950-A")
    assert not same_vermittlernummer("08/0950-T", "08/0951-T")
    assert not same_vermittlernummer("08/0950-T", "80/9050-T")


@pytest.mark.parametrize("value", [None, "", "   ", "-", 42])
def test_empty_values_never_match(value):
    assert vermittlernummer_key(value) is None
    assert not same_vermittlernummer(value, value)


def test_unknown_formats_are_kept_readable():
    assert format_vermittlernummer(" VM-1001 ") == "VM-1001"
    assert vermittlernummer_key("VM-1001") == "VM1001"


def test_login_with_other_spelling(client, db, tenant):
    from app.models import User, UserRole

    user = User(tenant_id=tenant.id, email="vm@example.com", vermittlernummer="08/0950-T", role=UserRole.ADMIN)
    user.set_password("geheimespasswort1")
    db.session.add(user)
    db.session.commit()

    resp = client.post(
        "/auth/login",
        data={"login_type": "vermittlernummer", "identifier": "080950-T", "password": "geheimespasswort1"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


def test_user_admin_rejects_duplicate_in_other_spelling(auth_client, db, tenant):
    from app.models import User

    other = User(tenant_id=tenant.id, email="laura@example.com", vermittlernummer="08/1777-B")
    other.set_password("irgendeinpasswort")
    db.session.add(other)
    db.session.commit()

    resp = auth_client.post(
        "/settings/users/new",
        data={
            "email": "neu@example.com",
            "vermittlernummer": "081777-B",
            "role": "mitarbeiter",
            "is_active": "y",
            "password": "startpasswort123",
            "password_confirm": "startpasswort123",
            "weekly_hours": "40",
            "workdays": ["1", "2", "3", "4", "5"],
        },
    )
    assert "bereits vergeben" in resp.get_data(as_text=True)
    assert User.query.filter_by(email="neu@example.com").first() is None


def test_user_admin_stores_uniform_format(auth_client, db, tenant):
    from app.models import User

    auth_client.post(
        "/settings/users/new",
        data={
            "email": "dennis@example.com",
            "vermittlernummer": "081234a",
            "role": "mitarbeiter",
            "is_active": "y",
            "password": "startpasswort123",
            "password_confirm": "startpasswort123",
            "weekly_hours": "40",
            "workdays": ["1", "2", "3", "4", "5"],
        },
    )
    assert User.query.filter_by(email="dennis@example.com").one().vermittlernummer == "08/1234-A"
