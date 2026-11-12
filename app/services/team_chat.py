"""Interner 1:1-Teamchat des Bueros.

Regeln (jede Anfrage prueft sie serverseitig erneut):
- Teilnehmer sind aktive Buero-Admins bzw. Mitarbeiter DESSELBEN Bueros (Tenant-Filter + Rolle).
- Laden, Senden und Lesestand nur fuer Unterhaltungen, an denen der angemeldete Benutzer beteiligt ist
  (sonst 404 - auch keine Auskunft, ob es die Unterhaltung gibt).
- Genau eine Unterhaltung je Paar (user_low_id < user_high_id, eindeutig je Buero).
- Live: ueber zentriq-live geht nur {"type": "chat", "chat": <id>} an die persoenlichen Kanaele beider
  Teilnehmer - den Text holt der Browser angemeldet ueber die API.
Logs enthalten keine Nachrichtentexte."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import TeamChat, TeamChatMessage, User
from app.models.enums import UserRole
from app.services import live
from app.services.calendar import member_name
from app.services.timetracking.clock import to_local

MAX_TEXT = 2000
PAGE = 50


class ChatError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def colleagues(user) -> list[User]:
    """Aktive Kollegen des eigenen Bueros (ohne den Benutzer selbst)."""
    return (
        User.query.filter(
            User.tenant_id == user.tenant_id,
            User.id != user.id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
            User.role.in_((UserRole.OFFICE_ADMIN, UserRole.EMPLOYEE)),
        )
        .order_by(User.id)
        .all()
    )


def _colleague(user, other_id) -> User:
    try:
        other_id = int(other_id)
    except (TypeError, ValueError):
        raise ChatError("Kollege nicht gefunden.", 404) from None
    other = next((c for c in colleagues(user) if c.id == other_id), None)
    if other is None:
        raise ChatError("Kollege nicht gefunden.", 404)
    return other


def partner_id(chat: TeamChat, user) -> int:
    return chat.user_high_id if chat.user_low_id == user.id else chat.user_low_id


def _read_id(chat: TeamChat, user) -> int:
    return chat.low_read_id if chat.user_low_id == user.id else chat.high_read_id


def get_chat(user, chat_id) -> TeamChat:
    """Unterhaltung, an der der Benutzer beteiligt ist, im eigenen Buero - sonst 404."""
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        raise ChatError("Unterhaltung nicht gefunden.", 404) from None
    chat = TeamChat.query.filter(
        TeamChat.id == chat_id,
        TeamChat.tenant_id == user.tenant_id,
        or_(TeamChat.user_low_id == user.id, TeamChat.user_high_id == user.id),
    ).first()
    if chat is None:
        raise ChatError("Unterhaltung nicht gefunden.", 404)
    return chat


def _active_partner(user, chat: TeamChat) -> User | None:
    other_id = partner_id(chat, user)
    return next((c for c in colleagues(user) if c.id == other_id), None)


def open_chat(user, other_id) -> TeamChat:
    other = _colleague(user, other_id)
    low, high = sorted((user.id, other.id))
    chat = TeamChat.query.filter_by(tenant_id=user.tenant_id, user_low_id=low, user_high_id=high).first()
    if chat is not None:
        return chat
    chat = TeamChat(tenant_id=user.tenant_id, user_low_id=low, user_high_id=high)
    db.session.add(chat)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # gleichzeitig angelegt: die vorhandene nehmen
        chat = TeamChat.query.filter_by(tenant_id=user.tenant_id, user_low_id=low, user_high_id=high).first()
    return chat


def unread_count(chat: TeamChat, user) -> int:
    return TeamChatMessage.query.filter(
        TeamChatMessage.chat_id == chat.id,
        TeamChatMessage.sender_id != user.id,
        TeamChatMessage.id > _read_id(chat, user),
    ).count()


def serialize_chat(chat: TeamChat, user, names: dict | None = None) -> dict:
    other_id = partner_id(chat, user)
    if names is None or other_id not in names:
        other = db.session.get(User, other_id)
        name = member_name(other) if other is not None else "Ehemaliger Kollege"
    else:
        name = names[other_id]
    return {"id": chat.id, "partner": {"id": other_id, "name": name}, "unread": unread_count(chat, user),
            "last_at": chat.last_message_at.isoformat() if chat.last_message_at else None}


def chats_for(user) -> list[dict]:
    names = {c.id: member_name(c) for c in colleagues(user)}
    chats = TeamChat.query.filter(
        TeamChat.tenant_id == user.tenant_id, or_(TeamChat.user_low_id == user.id, TeamChat.user_high_id == user.id)
    ).order_by(TeamChat.last_message_at.desc().nullslast(), TeamChat.id.desc()).all()
    return [serialize_chat(c, user, names) for c in chats if partner_id(c, user) in names]


def _when(value) -> str:
    local = to_local(value)
    today = to_local(_now()).date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%d.%m. %H:%M")


def serialize_message(message: TeamChatMessage, user) -> dict:
    return {"id": message.id, "mine": message.sender_id == user.id, "text": message.text, "when": _when(message.created_at)}


def messages(user, chat: TeamChat, after: int | None = None, before: int | None = None) -> list[dict]:
    query = TeamChatMessage.query.filter(TeamChatMessage.chat_id == chat.id)
    if after:
        rows = query.filter(TeamChatMessage.id > int(after)).order_by(TeamChatMessage.id).limit(PAGE).all()
    else:
        if before:
            query = query.filter(TeamChatMessage.id < int(before))
        rows = list(reversed(query.order_by(TeamChatMessage.id.desc()).limit(PAGE).all()))
    return [serialize_message(m, user) for m in rows]


def _notify(chat: TeamChat, kind: str = "chat") -> None:
    for uid in (chat.user_low_id, chat.user_high_id):
        live.publish(chat.tenant_id, f"user:{uid}", {"type": kind, "chat": chat.id})


def send(user, chat: TeamChat, text: str, client_ref: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise ChatError("Bitte eine Nachricht eingeben.")
    if len(text) > MAX_TEXT:
        raise ChatError(f"Die Nachricht ist zu lang (höchstens {MAX_TEXT} Zeichen).")
    if _active_partner(user, chat) is None:
        raise ChatError("Dieser Kollege ist nicht mehr im Büro aktiv – die Nachricht wurde nicht gesendet.", 409)
    ref = (client_ref or "").strip()[:64] or None
    if ref:
        existing = TeamChatMessage.query.filter_by(sender_id=user.id, client_ref=ref).first()
        if existing is not None and existing.chat_id == chat.id:
            return serialize_message(existing, user)
    message = TeamChatMessage(tenant_id=chat.tenant_id, chat_id=chat.id, sender_id=user.id, text=text, client_ref=ref)
    db.session.add(message)
    chat.last_message_at = _now()
    try:
        db.session.flush()
        _mark_read(chat, user, message.id)  # eigene Nachricht gilt als gelesen
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = TeamChatMessage.query.filter_by(sender_id=user.id, client_ref=ref).first()
        if existing is None:
            raise
        return serialize_message(existing, user)
    _notify(chat)
    return serialize_message(message, user)


def _mark_read(chat: TeamChat, user, message_id: int) -> bool:
    if chat.user_low_id == user.id:
        if message_id > chat.low_read_id:
            chat.low_read_id = message_id
            return True
    elif message_id > chat.high_read_id:
        chat.high_read_id = message_id
        return True
    return False


def mark_read(user, chat: TeamChat, last_id) -> int:
    """Lesestand bis last_id (hoechstens bis zur neuesten Nachricht dieser Unterhaltung)."""
    try:
        last_id = int(last_id)
    except (TypeError, ValueError):
        raise ChatError("Ungültiger Lesestand.") from None
    newest = db.session.query(db.func.max(TeamChatMessage.id)).filter(TeamChatMessage.chat_id == chat.id).scalar() or 0
    if _mark_read(chat, user, min(last_id, newest)):
        db.session.commit()
        live.publish(chat.tenant_id, f"user:{user.id}", {"type": "chat_read", "chat": chat.id})  # andere Tabs
    return unread_count(chat, user)


def live_access(user) -> dict | None:
    if not live.enabled():
        return None
    from app.services import telephony

    return {"token": telephony.token_for(user), "stream": "/live/stream"}
