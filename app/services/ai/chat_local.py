"""Assistent-Anfragen auf dem eigenen Rechner (Auftragsart "assistant_chat", app/services/local_ai.py).

Die Anfrage geht mit dem Gespraechsverlauf (inkl. ausgelesener Anhaenge) an das lokale Modell -
ohne Werkzeuge, denn der Rechner hat keinen Zugriff auf die Zentriq-Daten. Die Antwort landet in
der wartenden Assistent-Nachricht. Es gibt keinen Rueckfall auf Cloud-Modelle."""

from __future__ import annotations

from app.extensions import db
from app.models.ai import AiMessage

LOCAL_PROMPT = (
    "Du läufst lokal auf dem Rechner des Büros und hast keinen Zugriff auf Kundenakten, Kalender oder andere "
    "Zentriq-Daten. Antworte nur auf Grundlage des Gesprächs und der Anhänge. Erfinde keine Daten."
)
MAX_TOKENS = 1500
PROVIDER = "local"


def build_payload(system: str, messages: list[dict]) -> dict:
    return {"system": system, "messages": [{"role": m["role"], "content": m["content"]} for m in messages], "max_tokens": MAX_TOKENS}


def _message(job) -> AiMessage | None:
    if job.origin_type != "ai_message" or not job.origin_id:
        return None
    return AiMessage.query.filter(AiMessage.id == job.origin_id).first()


def on_done(job) -> None:
    from app.services.ai import chat

    message = _message(job)
    if message is None or message.status != "pending" or message.conversation is None:
        return
    text = ((job.result or {}).get("text") or "").strip() or "Das lokale Modell hat keine Antwort geliefert."
    chat._finish(message, message.conversation, text, provider=PROVIDER, model=(job.result or {}).get("model") or "lokales Modell",
                 tools_used=["Eigener Rechner"])


def on_fail(job) -> None:
    from app.services.ai import chat

    message = _message(job)
    if message is None or message.status != "pending":
        return
    reason = job.message or ("abgebrochen" if job.status == "cancelled" else "unbekannter Fehler")
    chat._fail(message, f"Dein Rechner hat die Anfrage nicht bearbeitet ({reason}). Sie wurde nicht an ein Cloud-Modell weitergegeben.")


def on_requeue(job) -> None:
    message = _message(job)
    if message is not None and message.status == "error":
        message.status, message.error, message.progress = "pending", None, "Wartet auf deinen Rechner"
        db.session.commit()
