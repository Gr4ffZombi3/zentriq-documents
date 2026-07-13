import base64
import io

from flask import current_app
from PIL import Image

from app.services.llm.client import get_openai_client

VISION_OCR_PROMPT = (
    "Extrahiere den gesamten sichtbaren Text dieser Dokumentenseite wortgetreu "
    "als Klartext. Keine Kommentare, keine Zusammenfassung, keine Formatierung."
)


def _image_to_data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def ocr_image(image: Image.Image) -> str:
    """Nutzt OpenAI Vision als OCR-Fallback fuer ein einzelnes Seitenbild."""
    client = get_openai_client()
    response = client.chat.completions.create(
        model=current_app.config["OPENAI_VISION_MODEL"],
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": _image_to_data_url(image)}},
                ],
            }
        ],
        max_tokens=4096,
    )
    return response.choices[0].message.content or ""
