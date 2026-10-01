"""Aktive Sitzungen je Benutzer ("Mein Konto -> Aktive Sitzungen").

Beim Login wird ein zufaelliges Token in die (signierte) Flask-Session gelegt und nur dessen
Hash gespeichert. Der user_loader prueft bei jeder Anfrage, ob die Sitzung noch gueltig ist -
eine widerrufene Sitzung ist damit sofort abgemeldet. last_seen_at wird hoechstens alle
LAST_SEEN_INTERVAL aktualisiert, um nicht bei jeder Anfrage zu schreiben."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import has_request_context, request, session

from app.extensions import db
from app.models.user_session import UserSession

SESSION_TOKEN_KEY = "_sid"
LAST_SEEN_INTERVAL = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(user) -> UserSession:
    """Legt nach erfolgreicher Anmeldung eine neue Sitzung an (ohne Commit)."""
    token = secrets.token_urlsafe(32)
    record = UserSession(
        user_id=user.id,
        token_hash=_hash(token),
        ip_address=request.remote_addr if has_request_context() else None,
        user_agent=(request.headers.get("User-Agent") or "")[:255] if has_request_context() else None,
    )
    db.session.add(record)
    session[SESSION_TOKEN_KEY] = token
    return record


def current_session_record(user_id: int) -> UserSession | None:
    token = session.get(SESSION_TOKEN_KEY)
    if not token:
        return None
    return UserSession.query.filter_by(user_id=user_id, token_hash=_hash(token)).first()


def validate_session(user) -> bool:
    """Aufruf aus dem user_loader. Sitzungen ohne Token (vor Einfuehrung dieser Funktion
    angemeldet) werden einmalig uebernommen statt abgemeldet."""
    token = session.get(SESSION_TOKEN_KEY)
    if not token:
        # Nicht bei parallel geladenen statischen Dateien, sonst entstehen mehrere Eintraege.
        if not (has_request_context() and request.endpoint == "static"):
            start_session(user)
            db.session.commit()
        return True
    record = UserSession.query.filter_by(user_id=user.id, token_hash=_hash(token)).first()
    if record is None or record.revoked_at is not None:
        session.pop(SESSION_TOKEN_KEY, None)
        return False
    now = _now()
    if record.last_seen_at is None or now - record.last_seen_at >= LAST_SEEN_INTERVAL:
        record.last_seen_at = now
        if has_request_context():
            record.ip_address = request.remote_addr
        db.session.commit()
    return True


def end_current_session(user_id: int) -> None:
    record = current_session_record(user_id)
    if record is not None and record.revoked_at is None:
        record.revoked_at = _now()
        db.session.commit()
    session.pop(SESSION_TOKEN_KEY, None)


def active_sessions(user_id: int) -> list[UserSession]:
    return (
        UserSession.query.filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .order_by(UserSession.last_seen_at.desc())
        .limit(50)
        .all()
    )


def revoke_other_sessions(user_id: int) -> int:
    """Meldet alle anderen Geraete ab; die aktuelle Sitzung bleibt bestehen."""
    current = current_session_record(user_id)
    query = UserSession.query.filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if current is not None:
        query = query.filter(UserSession.id != current.id)
    now = _now()
    count = 0
    for record in query.all():
        record.revoked_at = now
        count += 1
    db.session.commit()
    return count


def revoke_all_sessions(user_id: int) -> None:
    """Bei Passwortaenderung, Deaktivierung, Loeschung: auth_version erledigt die eigentliche
    Abmeldung, hier wird nur die Anzeige bereinigt (ohne Commit)."""
    now = _now()
    for record in UserSession.query.filter(UserSession.user_id == user_id, UserSession.revoked_at.is_(None)).all():
        record.revoked_at = now


def describe_user_agent(user_agent: str | None) -> str:
    """Grobe, lesbare Geraetebeschreibung ohne zusaetzliche Bibliothek."""
    ua = (user_agent or "").lower()
    if not ua:
        return "Unbekanntes Gerät"
    browser = next(
        (label for key, label in (("edg/", "Edge"), ("firefox", "Firefox"), ("opr/", "Opera"), ("chrome", "Chrome"), ("safari", "Safari")) if key in ua),
        "Browser",
    )
    system = next(
        (label for key, label in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"), ("windows", "Windows"), ("mac os", "macOS"), ("linux", "Linux")) if key in ua),
        "",
    )
    return f"{browser} auf {system}" if system else browser
