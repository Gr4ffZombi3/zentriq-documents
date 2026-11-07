"""Abrechnungen im Celery-Worker einlesen (Texterkennung kann bei Scans dauern).
resultrepr_maxsize=1: keine Inhalte im Worker-Log."""

from celery import shared_task

from app.tenancy import use_tenant_id


@shared_task(ignore_result=True, soft_time_limit=600, time_limit=660, resultrepr_maxsize=1)
def process_settlement(settlement_id: int, tenant_id: int) -> None:
    from app.extensions import db
    from app.models import Settlement
    from app.services import settlements

    with use_tenant_id(tenant_id):
        record = Settlement.query.filter(Settlement.id == settlement_id).first()
        if record is None or record.status != "processing":
            return
        try:
            settlements.process(record)
        except Exception:
            db.session.rollback()
            record = Settlement.query.filter(Settlement.id == settlement_id).first()
            if record is not None:
                record.status, record.message = "failed", "Der Beleg konnte nicht verarbeitet werden."
                db.session.commit()
            raise
