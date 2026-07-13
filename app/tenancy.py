"""Mandantentrennungs-Engine: macht Cross-Tenant-Datenzugriff strukturell unmoeglich statt
optional. Jede SELECT-Query gegen ein `TenantScopedMixin`-Modell wird automatisch um
`tenant_id == aktueller_tenant` ergaenzt (SQLAlchemy `do_orm_execute` + `with_loader_criteria`).
Ist kein Tenant-Kontext gesetzt, schlaegt die Query fehl (fail-closed), statt ungefiltert
Daten zurueckzugeben (fail-open). Bestehende Aufrufstellen wie `Document.query.filter(...)`
funktionieren unveraendert weiter, da Flask-SQLAlchemys `.query`-Property intern ueber
`Session.execute()` laeuft."""

from contextlib import contextmanager
from contextvars import ContextVar

from flask import abort
from sqlalchemy import event
from sqlalchemy.orm import declared_attr, with_loader_criteria
from sqlalchemy.orm.session import Session

from app.extensions import db

_current_tenant_id: ContextVar[int | None] = ContextVar("_current_tenant_id", default=None)
_bypass_tenant_scope: ContextVar[bool] = ContextVar("_bypass_tenant_scope", default=False)


class MissingTenantContextError(RuntimeError):
    """Wird ausgeloest, wenn eine Query gegen ein mandantengebundenes Modell ohne
    gesetzten Tenant-Kontext ausgefuehrt wird."""


class TenantScopedMixin:
    """Mixin fuer alle mandantengebundenen Modelle. Jede Unterklasse bekommt eine eigene
    `tenant_id`-Spalte (via `declared_attr`, da Column-Objekte nicht zwischen Tabellen
    geteilt werden koennen)."""

    @declared_attr
    def tenant_id(cls):
        return db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)


def set_current_tenant_id(tenant_id: int | None) -> None:
    _current_tenant_id.set(tenant_id)


def get_current_tenant_id() -> int | None:
    return _current_tenant_id.get()


@contextmanager
def use_tenant_id(tenant_id: int | None):
    """Setzt den Tenant-Kontext fuer die Dauer des Blocks und stellt danach den vorherigen
    Wert wieder her (token-basiert), statt ihn hart zurueckzusetzen. Wichtig fuer Celery-Tasks:
    im Eager-Modus (Tests/Dev ohne Worker) laeuft ein Task im selben contextvars-Kontext wie
    sein Aufrufer - ein hartes `set_current_tenant_id(None)` am Taskende wuerde sonst den
    Tenant-Kontext des aufrufenden HTTP-Requests zerstoeren."""
    token = _current_tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _current_tenant_id.reset(token)


@contextmanager
def bypass_tenant_scope():
    """Erlaubt bewusste Cross-Tenant-Operationen (Plattform-Admin-Aggregate, Celery-
    Task-Einstiegspunkt). Nur aus klar benannten, dokumentierten Stellen verwenden."""
    token = _bypass_tenant_scope.set(True)
    try:
        yield
    finally:
        _bypass_tenant_scope.reset(token)


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(execute_state):
    if not execute_state.is_select or _bypass_tenant_scope.get():
        return

    touches_tenant_scoped = any(
        issubclass(mapper.class_, TenantScopedMixin) for mapper in execute_state.all_mappers
    )
    if not touches_tenant_scoped:
        return

    tenant_id = _current_tenant_id.get()
    if tenant_id is None:
        raise MissingTenantContextError(
            "Query gegen ein mandantengebundenes Modell ohne gesetzten Tenant-Kontext. "
            "set_current_tenant_id() vorher aufrufen, oder bewusst bypass_tenant_scope() "
            "verwenden, falls dies eine gewollte Cross-Tenant-Operation ist."
        )

    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScopedMixin,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
        )
    )


def get_or_404_scoped(model, object_id):
    """Primaerschluessel-Lookup mit explizitem Tenant-Filter - zusaetzliche Absicherung
    neben dem globalen Listener, da `with_loader_criteria`-Verhalten bei reinen PK-Lookups
    versionsabhaengig sein kann."""
    if _bypass_tenant_scope.get():
        instance = model.query.filter(model.id == object_id).first()
    else:
        tenant_id = _current_tenant_id.get()
        if tenant_id is None:
            raise MissingTenantContextError("get_or_404_scoped() ohne gesetzten Tenant-Kontext aufgerufen.")
        instance = model.query.filter(model.id == object_id, model.tenant_id == tenant_id).first()

    if instance is None:
        abort(404)
    return instance
