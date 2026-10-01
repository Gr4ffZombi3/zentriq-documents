"""Gemeinsame Pruefung, ob sich ein Konto anmelden bzw. eine bestehende Session behalten darf.
Wird vom Login, vom 2FA-Schritt, vom Passwort-Reset und vom user_loader verwendet."""

from app.extensions import db
from app.models import Tenant, TenantStatus, User


def account_login_block_reason(user: User) -> str | None:
    if user.deleted_at is not None:
        return "deleted"
    if not user.is_active:
        return "inactive"
    # Der Plattformbetreiber bleibt auch dann handlungsfaehig, wenn sein eigenes Buero
    # deaktiviert wird.
    if not user.is_super_admin:
        tenant = db.session.get(Tenant, user.tenant_id)
        if tenant is None or tenant.status != TenantStatus.ACTIVE:
            return "tenant_suspended"
    return None
