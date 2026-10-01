"""Schreibt Eintraege ins technische Fehlerprotokoll (app/models/system_error.py).

Eigene Verbindung und Transaktion: die Session der fehlgeschlagenen Anfrage ist oft in einem
ungueltigen Zustand und darf durch das Protokollieren weder committet noch zurueckgerollt
werden. Das Protokollieren selbst darf nie einen Folgefehler ausloesen."""

from datetime import datetime, timezone

from flask import current_app, has_request_context, request

from app.extensions import db
from app.models.system_error import SystemErrorEvent
from app.tenancy import get_current_tenant_id


def record_system_error(source: str, error_type: str, *, location: str | None = None, tenant_id: int | None = None) -> None:
    try:
        if tenant_id is None:
            tenant_id = get_current_tenant_id()
        with db.engine.begin() as connection:
            connection.execute(
                SystemErrorEvent.__table__.insert().values(
                    occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    source=source[:20],
                    location=(location or "")[:255] or None,
                    error_type=(error_type or "Fehler")[:120],
                    tenant_id=tenant_id,
                )
            )
    except Exception:
        current_app.logger.warning("system_error.record_failed source=%s", source, exc_info=True)


def record_request_exception(sender, exception, **extra) -> None:
    """Signal-Handler fuer flask.got_request_exception. Pfad ohne Query-String."""
    location = None
    if has_request_context():
        location = f"{request.method} {request.url_rule.rule if request.url_rule else request.path}"
    record_system_error("request", type(exception).__name__, location=location)


def latest_errors(limit: int = 20) -> list[SystemErrorEvent]:
    return (
        SystemErrorEvent.query.order_by(SystemErrorEvent.occurred_at.desc(), SystemErrorEvent.id.desc())
        .limit(limit)
        .all()
    )
