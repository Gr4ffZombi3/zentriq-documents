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

    response = auth_client.get("/sprachnachrichten")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Sprachnachrichten" in html
    assert "Testbetrieb" in html
    for label in ("In Bearbeitung", "Manuelle Prüfung nötig", "Erledigt", "Fehler"):
        assert label in html
    # Technische Begriffe erscheinen nicht in der Buero-Oberflaeche.
    for term in ("Dry Run", "LLM", "Pipeline"):
        assert term not in html


def test_dashboard_route_filters_by_status(auth_client, tenant):
    make_case(tenant.id, status=MailboxStatus.NEW, source_key="k1", callback_phone="+4952111111")
    make_case(tenant.id, status=MailboxStatus.FAILED, source_key="k2", callback_phone="+4952122222", last_error="Formular nicht erreichbar")

    response = auth_client.get("/sprachnachrichten?status=failed")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "+4952122222" in html
    assert "Formular nicht erreichbar" in html
    assert "+4952111111" not in html


def test_dashboard_route_ignores_invalid_status_filter(auth_client, tenant):
    make_case(tenant.id, status=MailboxStatus.NEW, source_key="k1", callback_phone="+4952133333")

    response = auth_client.get("/sprachnachrichten?status=not-a-real-status")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "+4952133333" in html


def test_callback_state_distinguishes_prepared_and_submitted(auth_client, tenant):
    from app.models import CallbackAttemptStatus, MailboxCallbackAttempt

    prepared = make_case(tenant.id, status=MailboxStatus.CALLBACK_REQUESTED, source_key="p1")
    submitted = make_case(tenant.id, status=MailboxStatus.CALLBACK_REQUESTED, source_key="s1")
    for case, status, dry_run in ((prepared, CallbackAttemptStatus.PREPARED, True), (submitted, CallbackAttemptStatus.SUBMITTED, False)):
        db.session.add(
            MailboxCallbackAttempt(
                tenant_id=tenant.id, mailbox_case_id=case.id, status=status, dry_run=dry_run, form_url="https://example.invalid", request_data={}
            )
        )
    db.session.commit()

    html = auth_client.get("/sprachnachrichten").get_data(as_text=True)
    assert "Vorbereitet (Testbetrieb)" in html
    assert "Übermittelt" in html
