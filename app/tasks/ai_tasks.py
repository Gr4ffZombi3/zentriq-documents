"""ZentriqAI: Zusammenfassung langer Gespraeche im Hintergrund (spart Tokens, ohne die Antwort
im Web-Request zu verzoegern). Laeuft im Mandanten-Kontext des Gespraechs."""

from celery import shared_task

from app.tenancy import use_tenant_id


@shared_task(ignore_result=True)
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
