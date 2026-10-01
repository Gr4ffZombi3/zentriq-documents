"""Hilfsfunktionen fuer Tests mit Zwei-Faktor-Authentifizierung."""

import time

import pyotp

from app.extensions import db
from app.services import two_factor
from app.tenancy import use_tenant_id


def enable_two_factor(user) -> tuple[str, list[str]]:
    """Richtet 2FA fuer `user` ein wie ueber die Oberflaeche und liefert (Secret, Recovery Codes)."""
    with use_tenant_id(user.tenant_id):
        secret = two_factor.begin_setup(user)
        codes = two_factor.confirm_setup(user, totp_code(user, secret))
    assert codes is not None
    return secret, codes


def totp_code(user, secret: str) -> str:
    """Ein noch nicht verbrauchter, gueltiger Code (Zeitschritt nach dem zuletzt verwendeten,
    innerhalb der +-1-Toleranz)."""
    db.session.refresh(user) if user in db.session else None
    current = int(time.time()) // two_factor.TOTP_INTERVAL
    last = user.totp_last_counter
    counter = current - 1 if last is None else max(last + 1, current - 1)
    assert counter <= current + 1, "Alle Codes im Toleranzfenster sind verbraucht."
    return pyotp.TOTP(secret).at(counter * two_factor.TOTP_INTERVAL)


def login(client, identifier: str, password: str, secret: str | None = None, user=None, login_type: str = "email"):
    resp = client.post(
        "/auth/login", data={"login_type": login_type, "identifier": identifier, "password": password}
    )
    if secret is not None:
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/2fa"), resp.status_code
        resp = client.post("/auth/2fa", data={"code": totp_code(user, secret)})
    return resp
