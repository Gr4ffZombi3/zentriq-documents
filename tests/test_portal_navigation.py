"""Portal-Struktur: Startseite, Hauptnavigation, Bereichsreiter."""


def test_admin_home_is_chat(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/zentriq")


def test_login_redirects_to_portal_home(client, user):
    resp = client.post(
        "/auth/login", data={"login_type": "email", "identifier": user.email, "password": "testpassword123"}
    )
    assert resp.headers["Location"] == "/zentriq"  # direkt zur Startseite der Rolle, kein Umweg ueber "/"


def test_admin_navigation_shows_all_areas(auth_client):
    html = auth_client.get("/leipziger-liste").get_data(as_text=True)
    assert 'aria-label="Hauptnavigation"' in html
    for label in ("Start", "Leipziger Liste", "Memo", "Kunden", "Zeiterfassung", "Mitarbeiter", "Aktivitäten", "Benutzerverwaltung", "Mein Konto", "Abmelden", "Admin"):
        assert label in html
    for tab in ("Zu erledigen", "Mitarbeiter", "Listen &amp; Upload", "Auswertung", "Weitere"):
        assert tab in html


def test_employee_navigation_hides_admin_areas(employee_client):
    html = employee_client.get("/settings/profile").get_data(as_text=True)
    assert "Leipziger Liste" in html
    assert ">Memo<" in html
    assert "Mitarbeiter" in html  # Rollenbezeichnung
    for admin_area in ('href="/mitarbeiter"', 'href="/aktivitaeten"', 'href="/settings/users"'):
        assert admin_area not in html
    html = employee_client.get("/leipziger-liste").get_data(as_text=True)
    for admin_tab in ("Listen &amp; Upload", "Auswertung", "Weitere", "/leipziger-liste/mitarbeiter"):
        assert admin_tab not in html


def test_old_mailbox_url_still_redirects(auth_client):
    resp = auth_client.get("/mailbox")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/sprachnachrichten")


def test_sidebar_groups_by_workflow_and_booking_links_need_permission(app, client):
    """Seitenleiste nach Arbeitsablauf: "Kunden & Termine" mit eigenem Eintrag "Buchungslinks"
    (nur mit Berechtigung - sonst fuehrte der Eintrag auf eine 403-Seite) und "Abarbeiten"."""
    from app.extensions import db
    from app.models import Tenant, User, UserRole
    from app.tenancy import bypass_tenant_scope

    with app.app_context(), bypass_tenant_scope():
        tenant = Tenant(name="Navi-Buero", slug="navi-buero")
        db.session.add(tenant)
        db.session.flush()
        for email, role in (("navi-admin@example.com", UserRole.OFFICE_ADMIN), ("navi-ma@example.com", UserRole.EMPLOYEE)):
            u = User(tenant_id=tenant.id, email=email, role=role, is_active=True)
            u.set_password("testpassword123")
            db.session.add(u)
        db.session.commit()

    def sidebar(email):
        c = app.test_client()
        c.post("/auth/login", data={"login_type": "email", "identifier": email, "password": "testpassword123"})
        return c.get("/zentriq").get_data(as_text=True)

    admin = sidebar("navi-admin@example.com")
    assert "Kunden &amp; Termine" in admin and "Abarbeiten" in admin
    assert 'href="/kalender/buchungslinks"' in admin and ">Buchungslinks<" in admin
    staff = sidebar("navi-ma@example.com")
    assert "Kunden &amp; Termine" in staff and ">Buchungslinks<" not in staff
