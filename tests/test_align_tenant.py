"""flask align-tenant: Ausrichtung der Datenbasis auf genau einen produktiven Mandanten."""

from datetime import date, datetime

from app.cli import align_tenant_command
from app.models import (
    Customer,
    DocType,
    Document,
    DocumentCustomer,
    Task,
    TaskType,
    Tenant,
    TenantStatus,
    User,
    UserRole,
    WorkSession,
)
from app.tenancy import bypass_tenant_scope


def _invoke(app, *args):
    return app.test_cli_runner().invoke(align_tenant_command, list(args))


def _setup(db, tenant, tmp_path):
    other = Tenant(name="Huk", slug="huk")
    bot = Tenant(name="bot", slug="bot")
    db.session.add_all([other, bot])
    db.session.flush()
    admin = User(tenant_id=other.id, email="admin@example.com", vermittlernummer="08/0950-T", role=UserRole.EMPLOYEE)
    admin.set_password("adminpass123")
    bot_user = User(tenant_id=bot.id, email="bot@example.com", role=UserRole.OFFICE_ADMIN)
    bot_user.set_password("botpass1234")
    upload = tmp_path / "liste.pdf"
    upload.write_bytes(b"%PDF")
    customer = Customer(tenant_id=other.id, name="Alt")
    db.session.add_all([admin, bot_user, customer])
    db.session.flush()
    document = Document(
        tenant_id=other.id,
        filename="liste.pdf",
        original_filename="liste.pdf",
        file_path=str(upload),
        doc_type=DocType.LEIPZIGER_LISTE,
    )
    db.session.add(document)
    db.session.flush()
    db.session.add(DocumentCustomer(tenant_id=other.id, document_id=document.id, customer_id=customer.id, row_data=[]))
    db.session.add(Task(tenant_id=tenant.id, type=list(TaskType)[0], title="Alte Aufgabe", customer_id=customer.id))
    db.session.commit()
    return upload


def test_dry_run_changes_nothing(app, db, tenant, tmp_path):
    upload = _setup(db, tenant, tmp_path)
    result = _invoke(app, "--tenant", tenant.slug, "--admin-email", "admin@example.com")
    assert result.exit_code == 0, result.output
    assert "Vorschau" in result.output
    with bypass_tenant_scope():
        assert Customer.query.count() == 1
        assert User.query.filter_by(email="admin@example.com").one().tenant_id != tenant.id
        assert User.query.filter_by(email="bot@example.com").one().is_active
    assert upload.exists()


def test_execute_aligns_admin_and_removes_business_data(app, db, tenant, tmp_path):
    upload = _setup(db, tenant, tmp_path)
    with bypass_tenant_scope():
        admin_id = User.query.filter_by(email="admin@example.com").one().id
    db.session.add(WorkSession(
            tenant_id=tenant.id,
            user_id=admin_id,
            created_by_user_id=admin_id,
            work_date=date(2026, 10, 1),
            started_at=datetime(2026, 10, 1, 8, 0),
        )
    )
    db.session.commit()

    result = _invoke(app, "--tenant", tenant.slug, "--admin-email", "Admin@Example.com", "--execute")
    assert result.exit_code == 0, result.output

    with bypass_tenant_scope():
        admin = User.query.filter_by(email="admin@example.com").one()
        assert admin.tenant_id == tenant.id
        assert admin.role == UserRole.OFFICE_ADMIN and admin.is_active
        assert admin.vermittlernummer == "08/0950-T"
        assert not User.query.filter_by(email="bot@example.com").one().is_active
        assert Customer.query.count() == 0
        assert Document.query.count() == 0
        assert DocumentCustomer.query.count() == 0
        assert Task.query.count() == 0
        # Zeiterfassung bleibt unangetastet (Buchungen werden nie geloescht).
        assert WorkSession.query.count() == 1
        assert Tenant.query.filter_by(slug="huk").one().status == TenantStatus.SUSPENDED
        assert Tenant.query.filter_by(slug=tenant.slug).one().status == TenantStatus.ACTIVE
    # Upload-Datei wurde archiviert, nicht geloescht.
    assert not upload.exists()


def test_unknown_tenant_or_admin_fails(app, tenant):
    assert _invoke(app, "--tenant", "gibtsnicht", "--admin-email", "test@example.com").exit_code != 0
    assert _invoke(app, "--tenant", tenant.slug, "--admin-email", "nobody@example.com").exit_code != 0
