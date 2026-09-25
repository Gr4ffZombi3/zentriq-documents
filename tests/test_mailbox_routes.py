from app.extensions import db
from app.models import MailboxCase, MailboxCaseEvent
from app.models.enums import MailboxStatus


def make_case(tenant_id, **overrides):
    defaults = dict(
        tenant_id=tenant_id,
        source_key="route-case",
        source_subject="Mailbox-Nachricht",
        status=MailboxStatus.REVIEW,
        review_reason="Die Rückrufnummer ist nicht sicher genug erkannt.",
    )
    defaults.update(overrides)
    case = MailboxCase(**defaults)
    db.session.add(case)
    db.session.commit()
    return case


def test_detail_route_renders_case(auth_client, tenant):
    case = make_case(tenant.id, transcript="Ein Testtranskript.")
    response = auth_client.get(f"/mailbox/{case.id}")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Ein Testtranskript." in html


def test_detail_route_labels_transcript_sourced_phone_correctly(auth_client, tenant):
    case = make_case(
        tenant.id,
        callback_phone="+495211234567",
        phone_source="transcript_explicit",
        review_reason=None,
    )
    html = auth_client.get(f"/mailbox/{case.id}").get_data(as_text=True)
    assert "Erkannt aus: gesprochener Nachricht" in html
    assert "übermittelter Anrufernummer" not in html


def test_detail_route_labels_caller_id_sourced_phone_correctly(auth_client, tenant):
    case = make_case(
        tenant.id,
        callback_phone="+495211234567",
        phone_source="email_caller_id",
        review_reason=None,
    )
    html = auth_client.get(f"/mailbox/{case.id}").get_data(as_text=True)
    assert "Erkannt aus: übermittelter Anrufernummer" in html
    assert "gesprochener Nachricht" not in html


def test_dashboard_route_labels_transcript_sourced_phone_correctly(auth_client, tenant):
    make_case(
        tenant.id,
        source_key="dash-phone-source",
        callback_phone="+495211234567",
        phone_source="transcript_explicit",
        review_reason=None,
    )
    html = auth_client.get("/").get_data(as_text=True)
    assert "Quelle: Nachricht" in html
    assert "Quelle: Anruferkennung" not in html


def test_update_route_accepts_valid_correction(auth_client, tenant):
    case = make_case(tenant.id)
    response = auth_client.post(
        f"/mailbox/{case.id}/update",
        data={"callback_phone": "0521 1234567", "damage_type": "Kfz-Versicherung"},
        follow_redirects=True,
    )
    assert response.status_code == 200

    updated = db.session.get(MailboxCase, case.id)
    assert updated.callback_phone == "+495211234567"
    assert updated.damage_type == "Kfz-Versicherung"
    assert updated.concern == "Schadenanliegen"
    assert updated.status == MailboxStatus.NEW
    assert updated.review_reason is None

    events = MailboxCaseEvent.query.filter_by(mailbox_case_id=case.id).all()
    assert any(event.event_type == "manually_corrected" for event in events)


def test_update_route_rejects_invalid_phone(auth_client, tenant):
    case = make_case(tenant.id)
    response = auth_client.post(
        f"/mailbox/{case.id}/update",
        data={"callback_phone": "not-a-phone", "damage_type": "Kfz-Versicherung"},
        follow_redirects=True,
    )
    assert response.status_code == 200

    updated = db.session.get(MailboxCase, case.id)
    assert updated.callback_phone is None
    assert updated.status == MailboxStatus.REVIEW


def test_update_route_rejects_damage_type_not_in_huk_form(auth_client, tenant):
    case = make_case(tenant.id)
    response = auth_client.post(
        f"/mailbox/{case.id}/update",
        data={"callback_phone": "0521 1234567", "damage_type": "Erfundene Schadenart"},
        follow_redirects=True,
    )
    assert response.status_code == 200

    updated = db.session.get(MailboxCase, case.id)
    assert updated.damage_type is None


def test_update_route_selecting_sonstiges_keeps_case_in_review(auth_client, tenant):
    case = make_case(tenant.id)
    auth_client.post(
        f"/mailbox/{case.id}/update",
        data={"callback_phone": "0521 1234567", "damage_type": "Sonstiges"},
        follow_redirects=True,
    )

    updated = db.session.get(MailboxCase, case.id)
    assert updated.damage_type == "Sonstiges"
    assert updated.status == MailboxStatus.REVIEW
    assert "Sonstiges" in updated.review_reason


def test_update_route_blocked_after_callback_requested(auth_client, tenant):
    case = make_case(
        tenant.id,
        status=MailboxStatus.CALLBACK_REQUESTED,
        callback_phone="+495211234567",
        damage_type="Kfz-Versicherung",
        review_reason=None,
    )
    auth_client.post(
        f"/mailbox/{case.id}/update",
        data={"callback_phone": "0521 9999999", "damage_type": "Haftpflichtversicherung"},
        follow_redirects=True,
    )

    updated = db.session.get(MailboxCase, case.id)
    assert updated.callback_phone == "+495211234567"
    assert updated.damage_type == "Kfz-Versicherung"


def test_retry_route_blocked_without_clear_data(auth_client, tenant):
    case = make_case(tenant.id, callback_phone=None, damage_type=None)
    response = auth_client.post(f"/mailbox/{case.id}/retry", follow_redirects=True)
    assert response.status_code == 200

    updated = db.session.get(MailboxCase, case.id)
    assert updated.status == MailboxStatus.REVIEW


def test_retry_route_blocked_after_callback_requested(auth_client, tenant):
    case = make_case(
        tenant.id,
        status=MailboxStatus.CALLBACK_REQUESTED,
        callback_phone="+495211234567",
        damage_type="Kfz-Versicherung",
        review_reason=None,
    )
    response = auth_client.post(f"/mailbox/{case.id}/retry", follow_redirects=True)
    assert response.status_code == 200

    updated = db.session.get(MailboxCase, case.id)
    assert updated.status == MailboxStatus.CALLBACK_REQUESTED


def test_retry_route_reruns_ready_case_in_dry_run(auth_client, tenant, app):
    app.config["MAILBOX_DRY_RUN"] = True
    case = make_case(
        tenant.id,
        status=MailboxStatus.FAILED,
        callback_phone="+495211234567",
        damage_type="Kfz-Versicherung",
        review_reason=None,
        last_error="vorheriger Fehler",
    )
    response = auth_client.post(f"/mailbox/{case.id}/retry", follow_redirects=True)
    assert response.status_code == 200

    updated = db.session.get(MailboxCase, case.id)
    assert updated.status == MailboxStatus.NEW
    assert updated.last_error is None


def test_sync_route_requires_mailbox_configured(auth_client, app):
    app.config["PLACETEL_MAILBOX_ENABLED"] = False
    response = auth_client.post("/mailbox/sync", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "noch nicht konfiguriert" in html


def test_detail_route_requires_login(client, tenant):
    case = make_case(tenant.id)
    response = client.get(f"/mailbox/{case.id}")
    assert response.status_code in (302, 401)
