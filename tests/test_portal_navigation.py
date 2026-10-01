"""Portal-Struktur: Startseite, Hauptnavigation, Bereichsreiter."""


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
    for label in ("Leipziger Liste", "Memo", "Zeiterfassung", "Abmelden", "Einstellungen", "Admin"):
        assert label in html
    for tab in ("Zu erledigen", "Mitarbeiter", "Listen &amp; Upload", "Auswertung", "Weitere"):
        assert tab in html


def test_employee_navigation_hides_admin_areas(employee_client):
    html = employee_client.get("/settings/profile").get_data(as_text=True)
    assert "Leipziger Liste" in html
    assert ">Memo<" not in html
    assert "Mitarbeiter" in html  # Rollenbezeichnung
    html = employee_client.get("/leipziger-liste").get_data(as_text=True)
    for admin_tab in ("Listen &amp; Upload", "Auswertung", "Weitere", "/leipziger-liste/mitarbeiter"):
        assert admin_tab not in html


def test_old_mailbox_url_still_redirects(auth_client):
    resp = auth_client.get("/mailbox")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/sprachnachrichten")
