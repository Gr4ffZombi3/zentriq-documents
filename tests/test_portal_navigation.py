"""Portal-Struktur: Startseite, Hauptnavigation, Leipziger-Liste-Uebersicht."""

from app.models import ComparisonKind, DocStatus, DocType, Document, ListComparison, ListType


def test_admin_home_is_leipziger_liste(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/leipziger-liste")


def test_login_redirects_to_portal_home(client, user):
    resp = client.post(
        "/auth/login", data={"login_type": "email", "identifier": user.email, "password": "testpassword123"}
    )
    assert resp.headers["Location"] == "/"


def test_admin_navigation_shows_all_areas(auth_client):
    html = auth_client.get("/leipziger-liste").get_data(as_text=True)
    assert 'aria-label="Hauptnavigation"' in html
    for label in ("Leipziger Liste", "Sprachnachrichten", "Abmelden", "Einstellungen", "Admin"):
        assert label in html


def test_employee_navigation_hides_admin_areas(employee_client):
    html = employee_client.get("/settings/profile").get_data(as_text=True)
    assert "Leipziger Liste" not in html
    assert "Sprachnachrichten" not in html
    assert "Mitarbeiter" in html


def test_old_mailbox_url_still_redirects(auth_client):
    resp = auth_client.get("/mailbox")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/sprachnachrichten")


def _make_list(db, tenant, name, list_type=ListType.OWN, status=DocStatus.DONE):
    document = Document(
        filename=name,
        original_filename=name,
        file_path=f"/tmp/{name}",
        status=status,
        doc_type=DocType.LEIPZIGER_LISTE if status == DocStatus.DONE else None,
        list_type=list_type,
        tenant_id=tenant.id,
    )
    db.session.add(document)
    db.session.commit()
    return document


def test_leipziger_overview_shows_lists_and_comparison(auth_client, db, tenant):
    previous = _make_list(db, tenant, "liste-juli.pdf")
    current = _make_list(db, tenant, "liste-august.pdf")
    _make_list(db, tenant, "liste-neu.pdf", status=DocStatus.PENDING)
    db.session.add(
        ListComparison(
            tenant_id=tenant.id,
            document_id=current.id,
            previous_document_id=previous.id,
            comparison_kind=ComparisonKind.TEMPORAL,
            new_customer_count=3,
            storno_count=1,
        )
    )
    db.session.commit()

    resp = auth_client.get("/leipziger-liste")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "liste-august.pdf" in html
    assert "liste-neu.pdf" in html
    assert "Neue Kunden" in html and "Storno" in html and "Neue Produktsparten" in html
    assert "Noch kein Vergleich vorhanden" in html  # kein OWN_VS_GS


def test_leipziger_overview_is_tenant_isolated(auth_client, db, tenant):
    from app.models import Tenant
    from app.tenancy import use_tenant_id

    other = Tenant(name="Fremd", slug="fremd-ll")
    db.session.add(other)
    db.session.commit()
    with use_tenant_id(other.id):
        _make_list(db, other, "fremde-liste.pdf")
    html = auth_client.get("/leipziger-liste").get_data(as_text=True)
    assert "fremde-liste.pdf" not in html
