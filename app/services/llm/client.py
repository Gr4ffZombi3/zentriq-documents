from flask import current_app
from openai import OpenAI


def get_openai_client() -> OpenAI:
    return OpenAI(api_key=current_app.config["OPENAI_API_KEY"])
