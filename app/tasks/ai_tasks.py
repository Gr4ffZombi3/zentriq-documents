"""ZentriqAI im Celery-Worker: Antworten erzeugen (kein blockierender KI-Aufruf im Web-Request)
und lange Gespraeche zusammenfassen. Beides laeuft im Mandanten-Kontext des Gespraechs.

resultrepr_maxsize=1: Rueckgabewerte landen nie im Worker-Log."""

from celery import shared_task

from app.tenancy import bypass_tenant_scope, use_tenant_id


@shared_task(ignore_result=True, soft_time_limit=1200, time_limit=1260, resultrepr_maxsize=1)
def answer_message(message_id: int, tenant_id: int, user_id: int, audio_jobs=None) -> None:
    from app.extensions import db
    from app.models import User
    from app.models.ai import AiMessage
    from app.services.ai import chat

    with bypass_tenant_scope():
        user = db.session.get(User, user_id)
    with use_tenant_id(tenant_id):
        if user is None or user.tenant_id != tenant_id or not user.is_active:
            message = AiMessage.query.filter(AiMessage.id == message_id).first()
            if message is not None and message.status == "pending":
                chat._fail(message, "Die Anfrage konnte nicht verarbeitet werden.")
            return
        try:
            chat.process_pending(user, message_id, audio_jobs)
        except Exception:
            db.session.rollback()
            message = AiMessage.query.filter(AiMessage.id == message_id).first()
            if message is not None and message.status == "pending":
                chat._fail(message, "Bei der Verarbeitung ist ein Fehler aufgetreten. Bitte erneut senden.")
            raise


@shared_task(ignore_result=True, resultrepr_maxsize=1)
def summarize_conversation(conversation_id: int, tenant_id: int) -> None:
    from app.models.ai import AiConversation
    from app.services.ai import chat, router

    with use_tenant_id(tenant_id):
        conversation = AiConversation.query.filter(AiConversation.id == conversation_id).first()
        if conversation is None:
            return
        try:
            chat.summarize(conversation)
        except router.AiError:
            # Kein Modell verfuegbar: beim naechsten Mal erneut; der Chat funktioniert weiter.
            return
