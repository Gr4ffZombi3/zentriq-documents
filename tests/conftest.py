import pytest

from app import create_app
from app.extensions import db as _db
from app.tenancy import set_current_tenant_id
from config import TestingConfig


@pytest.fixture()
def app(tmp_path):
    application = create_app(TestingConfig)
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    application.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'test.db'}"
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture(autouse=True)
def tenant(app, db):
    """Erstellt einen Tenant und setzt den Tenant-Kontext fuer die Dauer des Tests, damit
    Tests direkte DB-Zugriffe machen koennen, ohne sich erst einzuloggen. Ohne dies
    schlaegt jede Query gegen ein TenantScopedMixin-Modell mit MissingTenantContextError
    fehl - das ist beabsichtigt (fail-closed) und der Beweis, dass die Mandantensperre
    tatsaechlich greift. Fuer Requests ueber den Test-Client wird der Tenant-Kontext
    stattdessen durch den Login (siehe auth_client-Fixture) via user_loader gesetzt."""
    from app.models import Tenant

    default_tenant = Tenant(name="Default Tenant", slug="default")
    db.session.add(default_tenant)
    db.session.commit()
    set_current_tenant_id(default_tenant.id)
    yield default_tenant
    set_current_tenant_id(None)


@pytest.fixture()
def user(db, tenant):
    from app.models import User

    test_user = User(tenant_id=tenant.id, email="test@example.com", is_active=True)
    test_user.set_password("testpassword123")
    db.session.add(test_user)
    db.session.commit()
    return test_user


@pytest.fixture()
def auth_client(client, user):
    """Test-Client, der bereits eingeloggt ist - fuer Requests gegen @login_required-Routen."""
    client.post(
        "/auth/login",
        data={"email": user.email, "password": "testpassword123"},
        follow_redirects=True,
    )
    return client


@pytest.fixture(autouse=True)
def mock_ocr(monkeypatch):
    """Ersetzt Tesseract-OCR durch einen deterministischen Fake, damit Tests ohne echte
    Tesseract-Installation und ohne OpenAI-API-Aufrufe laufen."""

    def fake_ocr_image(image):
        return "Erkannter Testtext aus Tesseract.", 96.0

    monkeypatch.setattr("app.services.ocr.tesseract_ocr.ocr_image", fake_ocr_image)


@pytest.fixture(autouse=True)
def mock_llm_extraction(monkeypatch):
    """Ersetzt den OpenAI-Extraktionsaufruf durch einen deterministischen Fake, damit Tests
    ohne echten API-Key laufen. Einzelne Tests koennen dies ueberschreiben."""
    from app.models.enums import DocType
    from app.services.llm.schemas import DocumentExtraction, ExtractedCustomer

    def fake_extract_document_data(raw_text):
        return DocumentExtraction(
            doc_type=DocType.RECHNUNG,
            customer=ExtractedCustomer(name="Max Mustermann", city="Köln", postal_code="50667"),
            insurer="Testversicherung AG",
            products=["Kfz-Haftpflicht"],
        )

    monkeypatch.setattr(
        "app.services.llm.extraction.extract_document_data", fake_extract_document_data
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data", fake_extract_document_data
    )
