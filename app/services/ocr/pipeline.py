import fitz
from flask import current_app
from PIL import Image

from app.models import OcrEngine
from app.services.ocr import tesseract_ocr, vision_ocr


def _render_pages(file_path: str, zoom: float = 2.0) -> list[Image.Image]:
    doc = fitz.open(file_path)
    try:
        matrix = fitz.Matrix(zoom, zoom)
        images = []
        for page in doc:
            pixmap = page.get_pixmap(matrix=matrix)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            images.append(image)
        return images
    finally:
        doc.close()


def extract_text(file_path: str) -> tuple[str, OcrEngine, float | None]:
    """Extrahiert Text aus einer PDF: Tesseract primaer, OpenAI Vision als Fallback pro Seite
    bei niedriger Konfidenz oder zu kurzem Text. Gibt (text, engine_used, avg_confidence) zurueck."""
    min_confidence = current_app.config["OCR_MIN_CONFIDENCE"]
    min_text_length = current_app.config["OCR_MIN_TEXT_LENGTH"]

    page_texts = []
    confidences = []
    used_vision = False

    for image in _render_pages(file_path):
        text, confidence = tesseract_ocr.ocr_image(image)
        if len(text.strip()) < min_text_length or confidence < min_confidence:
            text = vision_ocr.ocr_image(image)
            used_vision = True
        else:
            confidences.append(confidence)
        page_texts.append(text)

    engine_used = OcrEngine.VISION if used_vision else OcrEngine.TESSERACT
    avg_confidence = sum(confidences) / len(confidences) if confidences else None
    full_text = "\n\n".join(page_texts)
    return full_text, engine_used, avg_confidence
