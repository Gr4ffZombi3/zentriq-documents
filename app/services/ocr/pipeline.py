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


def _native_page_texts(file_path: str) -> list[str]:
    """Eingebetteter (maschinenlesbarer) Text je Seite - zeilentreu, ohne OCR."""
    doc = fitz.open(file_path)
    try:
        return [page.get_text("text") for page in doc]
    finally:
        doc.close()


def extract_text(file_path: str) -> tuple[str, OcrEngine, float | None, list[str]]:
    """Extrahiert Text aus einer PDF - immer alle Seiten. Pro Seite zuerst der eingebettete
    PDF-Text; nur Seiten ohne ausreichenden Text (Scans) gehen durch Tesseract, bei niedriger
    Konfidenz oder zu kurzem Ergebnis zusaetzlich durch OpenAI Vision.

    Hintergrund: Tesseract liest breite Tabellen spaltenweise (erst alle Nummern, dann alle
    Namen, dann alle Daten) - die Zeilenzuordnung ginge verloren. Der native Text behaelt sie.
    Gibt (text, engine_used, avg_confidence, page_texts) zurueck; engine_used ist NONE, wenn
    keine Seite OCR brauchte."""
    min_confidence = current_app.config["OCR_MIN_CONFIDENCE"]
    min_text_length = current_app.config["OCR_MIN_TEXT_LENGTH"]

    page_texts = _native_page_texts(file_path)
    confidences = []
    used_ocr = False
    used_vision = False

    ocr_pages = [index for index, text in enumerate(page_texts) if len(text.strip()) < min_text_length]
    if ocr_pages:
        images = _render_pages(file_path)
        for index in ocr_pages:
            used_ocr = True
            text, confidence = tesseract_ocr.ocr_image(images[index])
            if len(text.strip()) < min_text_length or confidence < min_confidence:
                text = vision_ocr.ocr_image(images[index])
                used_vision = True
            else:
                confidences.append(confidence)
            page_texts[index] = text

    if used_vision:
        engine_used = OcrEngine.VISION
    elif used_ocr:
        engine_used = OcrEngine.TESSERACT
    else:
        engine_used = OcrEngine.NONE
    avg_confidence = sum(confidences) / len(confidences) if confidences else None
    full_text = "\n\n".join(page_texts)
    return full_text, engine_used, avg_confidence, page_texts
