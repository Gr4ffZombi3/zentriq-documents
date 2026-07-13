import pytesseract
from flask import current_app
from PIL import Image


def _configure_tesseract():
    cmd = current_app.config.get("TESSERACT_CMD")
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd


def ocr_image(image: Image.Image) -> tuple[str, float]:
    """Fuehrt Tesseract-OCR auf einem Seitenbild aus. Gibt (text, confidence 0-100) zurueck."""
    _configure_tesseract()
    data = pytesseract.image_to_data(image, lang="deu+eng", output_type=pytesseract.Output.DICT)

    words = []
    confidences = []
    for text, conf in zip(data["text"], data["conf"]):
        if text.strip():
            words.append(text)
            conf_value = float(conf)
            if conf_value >= 0:
                confidences.append(conf_value)

    full_text = " ".join(words)
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return full_text, avg_confidence
