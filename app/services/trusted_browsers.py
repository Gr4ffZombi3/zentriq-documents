"""Vertrauenswuerdige Browser: "Diesem Browser 30 Tage vertrauen" bei der 2FA-Abfrage.

- Nach erfolgreicher 2FA mit ausgewaehlter Option wird ein kryptografisch zufaelliges Token im
  Cookie TRUST_COOKIE abgelegt (HttpOnly, Secure, SameSite=Strict, nur Pfad /auth); in der
  Datenbank steht nur dessen SHA-256-Hash. Passwort, 2FA-Geheimnis und Wiederherstellungscodes
  kommen nie in den Browser.
- Bei einer Anmeldung mit korrektem Passwort entfaellt der 2FA-Code, wenn der Browser ein
  gueltiges Token dieses Benutzers mitschickt (serverseitig geprueft: nicht widerrufen, nicht
  abgelaufen, gleiche auth_version, 2FA weiterhin eingerichtet).
- Verlaengert wird nie automatisch - nur durch eine erneute erfolgreiche 2FA mit der Option.
- Widerruf: einzeln ("Abmelden und diesem Browser nicht mehr vertrauen"), alle Browser eines
  Benutzers ("Alle Geraete abmelden", Sperrung des Bueros) sowie automatisch ueber auth_version
  (Passwortaenderung/-reset, 2FA-Reset, Deaktivierung, Loeschung - User.invalidate_sessions()).
- Ein Browser kann mehreren Benutzern vertrauen (geteilter Buerorechner): das Cookie enthaelt
  bis zu MAX_TOKENS_PER_COOKIE Tokens; jedes gehoert genau einem Benutzer."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app, has_request_context, request

from app.extensions import db
from app.models.trusted_browser import TrustedBrowser

TRUST_COOKIE = "zentriq_trusted"
COOKIE_PATH = "/auth"
MAX_TOKENS_PER_COOKIE = 5
MAX_TOKEN_LENGTH = 100


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def trust_days() -> int:
    return int(current_app.config.get("TWO_FACTOR_TRUST_DAYS", 30))


def _cookie_tokens() -> list[str]:
    if not has_request_context():
        return []
    raw = request.cookies.get(TRUST_COOKIE) or ""
    tokens = [t for t in raw.split(".") if t and len(t) <= MAX_TOKEN_LENGTH]
    return tokens[:MAX_TOKENS_PER_COOKIE]


def _set_cookie(response, tokens: list[str]) -> None:
    if not tokens:
        response.delete_cookie(TRUST_COOKIE, path=COOKIE_PATH, secure=_secure(), httponly=True, samesite="Strict")
        return
    response.set_cookie(
        TRUST_COOKIE,
        ".".join(tokens[-MAX_TOKENS_PER_COOKIE:]),
        max_age=trust_days() * 24 * 3600,
        path=COOKIE_PATH,
        secure=_secure(),
        httponly=True,
        samesite="Strict",
    )


def _secure() -> bool:
    return bool(current_app.config.get("TRUSTED_BROWSER_COOKIE_SECURE", True))


def _records_for(user, tokens: list[str]) -> list[TrustedBrowser]:
    if not tokens:
        return []
    return TrustedBrowser.query.filter(
        TrustedBrowser.user_id == user.id, TrustedBrowser.token_hash.in_([_hash(t) for t in tokens])
    ).all()


def _is_valid(record: TrustedBrowser, user, now: datetime) -> bool:
    return (
        record.revoked_at is None
        and record.expires_at > now
        and record.auth_version == (user.auth_version or 0)
        and user.two_factor_enabled
    )


def trusted_record(user) -> TrustedBrowser | None:
    """Gueltiges Vertrauen dieses Browsers fuer `user` - oder None."""
    now = _now()
    for record in _records_for(user, _cookie_tokens()):
        if _is_valid(record, user, now):
            return record
    return None


def mark_used(record: TrustedBrowser) -> None:
    """Nur zur Anzeige; verlaengert das Vertrauen nicht (ohne Commit)."""
    record.last_used_at = _now()
    if has_request_context():
        record.ip_address = request.remote_addr


def trust_this_browser(user, response) -> TrustedBrowser:
    """Nach erfolgreicher 2FA: neues Token fuer 30 Tage. Ein frueheres Token dieses Benutzers in
    diesem Browser wird dabei widerrufen und ersetzt (committet)."""
    tokens = _cookie_tokens()
    now = _now()
    own = {r.token_hash for r in _records_for(user, tokens)}
    for record in _records_for(user, tokens):
        if record.revoked_at is None:
            record.revoked_at = now
    # Fremde (andere Benutzer) bleiben im Cookie; ungueltige raeumt die naechste Pruefung nicht
    # auf - sie schaden nicht, weil der Server jedes Token einzeln prueft.
    tokens = [t for t in tokens if _hash(t) not in own]
    token = secrets.token_urlsafe(32)
    record = TrustedBrowser(
        user_id=user.id,
        token_hash=_hash(token),
        auth_version=user.auth_version or 0,
        created_at=now,
        expires_at=now + timedelta(days=trust_days()),
        ip_address=request.remote_addr if has_request_context() else None,
        user_agent=(request.headers.get("User-Agent") or "")[:255] if has_request_context() else None,
    )
    db.session.add(record)
    db.session.commit()
    _set_cookie(response, [*tokens, token])
    return record


def forget_this_browser(user, response) -> int:
    """Widerruft das Vertrauen dieses Browsers fuer `user` und entfernt dessen Token aus dem Cookie."""
    tokens = _cookie_tokens()
    records = _records_for(user, tokens)
    now = _now()
    count = 0
    for record in records:
        if record.revoked_at is None:
            record.revoked_at = now
            count += 1
    db.session.commit()
    own = {r.token_hash for r in records}
    _set_cookie(response, [t for t in tokens if _hash(t) not in own])
    return count


def revoke_all(user_id: int) -> int:
    """Widerruft alle Vertrauens-Tokens eines Benutzers (ohne Commit)."""
    return TrustedBrowser.query.filter(TrustedBrowser.user_id == user_id, TrustedBrowser.revoked_at.is_(None)).update(
        {TrustedBrowser.revoked_at: _now()}, synchronize_session=False
    )


def active_count(user_id: int) -> int:
    return TrustedBrowser.query.filter(
        TrustedBrowser.user_id == user_id, TrustedBrowser.revoked_at.is_(None), TrustedBrowser.expires_at > _now()
    ).count()
