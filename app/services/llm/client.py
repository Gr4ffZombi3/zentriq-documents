from flask import current_app
from openai import OpenAI


def get_openai_client() -> OpenAI:
    kwargs = {"api_key": current_app.config["OPENAI_API_KEY"]}
    if current_app.config.get("OPENAI_BASE_URL"):
        kwargs["base_url"] = current_app.config["OPENAI_BASE_URL"]
    return OpenAI(**kwargs)
