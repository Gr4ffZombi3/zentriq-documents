# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Buerologo: Upload nur fuer Buero-Admins, nur Rasterbilder (neu als PNG gespeichert), Anzeige auf
Buchungsseite und Linkvorschau, strikt je Buero, Entfernen."""

import io
import re

from PIL import Image

from app.extensions import db as _db
from app.models.office_settings import OfficeSettings
from app.tenancy import bypass_tenant_scope
from tests.test_booking_links import _create
from tests.test_customer_notes import office  # noqa: F401
from tests.test_office_booking import _enable, setup  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


def _png(w=900, h=300, fmt="PNG", exif=False):
    img = Image.new("RGB", (w, h), (31, 95, 174))
    buf = io.BytesIO()
    kwargs = {}
    if exif:
        ex = Image.Exif()
        ex[0x010E] = "Geheime Beschreibung"
        kwargs["exif"] = ex.tobytes()
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()


def _upload(client, data, name="logo.png"):
    return client.post("/kalender/einstellungen/logo", data={"logo": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def test_admin_uploads_logo_shown_on_booking_page_and_preview(app, setup):
    _enable(app, setup)
    admin = login(app, "admin-a@example.com")
    r = _upload(admin, _png(fmt="JPEG", exif=True), "logo.jpg")
    assert r.status_code == 302 and r.headers["Location"].endswith("#s-logo")
    html = app.test_client().get("/termin/heller").get_data(as_text=True)
    m = re.search(r'<img class="logo" src="(/termin/logo/([0-9a-f]{16})\.png)" alt="Logo Agentur Heller"', html)
    assert m and "data-booking-mono" not in html
    assert f'<meta property="og:image" content="http://localhost{m.group(1)}">' in html
    logo = app.test_client().get(m.group(1))
    assert logo.status_code == 200 and logo.mimetype == "image/png"
    assert "immutable" in logo.headers["Cache-Control"] and logo.headers["X-Content-Type-Options"] == "nosniff"
    img = Image.open(io.BytesIO(logo.data))
    assert img.format == "PNG" and img.width <= 600 and img.height <= 240  # verkleinert
    assert b"Geheime Beschreibung" not in logo.data  # Metadaten entfernt
    # Einstellungen zeigen die Vorschau und "Logo entfernen"
    page = admin.get("/kalender/einstellungen").get_data(as_text=True)
    assert m.group(1) in page and "data-logo-remove" in page


def test_logo_also_on_personal_link_page(app, setup):
    admin = login(app, "admin-a@example.com")
    _upload(admin, _png())
    _admin, token, _day = _create(app, setup, customer=False)
    html = app.test_client().get(f"/termin/buchen/{token}").get_data(as_text=True)
    assert "data-booking-logo" in html


def test_rejects_non_images_svg_and_oversized(app, setup):
    admin = login(app, "admin-a@example.com")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    for data, name, text in ((svg, "logo.svg", "kein lesbares Bild"), (b"GIF89a" + b"\0" * 50, "x.gif", ""),
                             (b"%PDF-1.4 nope", "x.pdf", "kein lesbares Bild"), (b"", "leer.png", "Bilddatei auswählen")):
        r = _upload(admin, data, name)
        assert r.status_code == 422, name
        assert "data-logo-error" in r.get_data(as_text=True)
        if text:
            assert text in r.get_data(as_text=True)
    big = _upload(admin, b"\x89PNG" + b"0" * (3 * 1024 * 1024 + 10))
    assert big.status_code == 422 and "zu groß" in big.get_data(as_text=True)
    gif = io.BytesIO()
    Image.new("RGB", (100, 40)).save(gif, "GIF")
    r = _upload(admin, gif.getvalue(), "logo.gif")
    assert r.status_code == 422 and "PNG, JPG oder WebP" in r.get_data(as_text=True)
    with bypass_tenant_scope():
        assert not OfficeSettings.query.filter(OfficeSettings.logo_rev.isnot(None)).count()


def test_only_office_admin_may_upload_and_logo_is_per_office(app, setup):
    staff = login(app, "laura@example.com")
    assert _upload(staff, _png()).status_code == 403
    admin_a = login(app, "admin-a@example.com")
    _upload(admin_a, _png())
    with bypass_tenant_scope():
        rev_a = OfficeSettings.query.filter(OfficeSettings.tenant_id == setup.tenant_a.id).one().logo_rev
        others = OfficeSettings.query.filter(OfficeSettings.tenant_id != setup.tenant_a.id).all()
    assert rev_a and all(o.logo_rev is None for o in others)
    # Anderes Buero: eigene Einstellungen ohne Logo
    admin_b = login(app, "admin-b@example.com")
    assert f"/termin/logo/{rev_a}.png" not in admin_b.get("/kalender/einstellungen").get_data(as_text=True)


def test_remove_logo_and_unknown_rev_404(app, setup):
    _enable(app, setup)
    admin = login(app, "admin-a@example.com")
    _upload(admin, _png())
    with bypass_tenant_scope():
        rev = OfficeSettings.query.filter(OfficeSettings.tenant_id == setup.tenant_a.id).one().logo_rev
    r = admin.post("/kalender/einstellungen/logo", data={"aktion": "entfernen"})
    assert r.status_code == 302
    client = app.test_client()
    assert client.get(f"/termin/logo/{rev}.png").status_code == 404
    assert client.get("/termin/logo/zzzz.png").status_code == 404
    html = client.get("/termin/heller").get_data(as_text=True)
    assert "data-booking-logo" not in html and "data-booking-mono" in html
    with bypass_tenant_scope():
        _db.session.expire_all()
        assert OfficeSettings.query.filter(OfficeSettings.tenant_id == setup.tenant_a.id).one().logo_rev is None
