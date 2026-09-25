from datetime import datetime, timezone

from app.extensions import db
from app.models import MailboxCase
from app.models.enums import MailboxStatus


def make_case(tenant_id, status=MailboxStatus.NEW, **overrides):
    defaults = dict(
        tenant_id=tenant_id,
        source_key=f"key-{status.value}-{overrides.get('source_subject', 'x')}",
        source_subject="Mailbox-Nachricht",
        source_sender="mailbox@placetel.de",
        received_at=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        status=status,
    )
    defaults.update(overrides)
    case = MailboxCase(**defaults)
    db.session.add(case)
    db.session.commit()
    return case


def test_dashboard_route_renders_mailbox_overview(auth_client, tenant):
    make_case(tenant.id, status=MailboxStatus.NEW, source_key="k1")
    make_case(tenant.id, status=MailboxStatus.REVIEW, source_key="k2")
    make_case(tenant.id, status=MailboxStatus.CALLBACK_REQUESTED, source_key="k3")
    make_case(tenant.id, status=MailboxStatus.FAILED, source_key="k4")

    response = auth_client.get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Rückruf-Automation" in html
    assert "Dry Run aktiv" in html
    assert "Mailbox-Nachricht" in html


def test_dashboard_route_filters_by_status(auth_client, tenant):
    make_case(tenant.id, status=MailboxStatus.NEW, source_key="k1", source_subject="Neuer Anruf")
    make_case(tenant.id, status=MailboxStatus.FAILED, source_key="k2", source_subject="Fehlgeschlagener Anruf")

    response = auth_client.get("/?status=failed")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Fehlgeschlagener Anruf" in html
    assert "Neuer Anruf" not in html


def test_dashboard_route_ignores_invalid_status_filter(auth_client, tenant):
    make_case(tenant.id, status=MailboxStatus.NEW, source_key="k1", source_subject="Sichtbarer Anruf")

    response = auth_client.get("/?status=not-a-real-status")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Sichtbarer Anruf" in html
