from flask import current_app
from openai import OpenAI


def get_openai_client(*, base_url: str | None = None) -> OpenAI:
    """Client fuer alle KI-Funktionen (Leipziger-Liste-Extraktion, Vision-OCR, Analyse, Chat,
    Suche). OPENAI_BASE_URL ist bewusst eine GLOBALE Einstellung; ein abweichender Endpoint
    nur fuer die Mailbox wird ueber `base_url` (MAILBOX_OPENAI_BASE_URL) uebergeben und wirkt
    sich damit nie auf die Leipziger-Liste aus."""
    kwargs = {"api_key": current_app.config["OPENAI_API_KEY"]}
    base_url = base_url or current_app.config.get("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)
