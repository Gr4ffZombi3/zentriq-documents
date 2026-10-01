"""Universal-Upload: erkennt den Dateityp, bevor etwas verarbeitet wird.

Verarbeitet wird danach ausschliesslich ueber die bestehenden Wege - es gibt hier keine eigene
Import- oder Transkriptionslogik:
- Leipziger Liste (PDF im *WM312-L*-Format) -> /upload (Leipziger-Import, nur Buero-Admin)
- Sprachnachricht (Audioformate aus app/services/memo.py) -> Memo-Transkription"""

from __future__ import annotations

import os
from dataclasses import dataclass

import fitz

from app.services.leipziger_parser import is_wm312_list
from app.services.memo import ALLOWED_AUDIO_EXTENSIONS, MAX_AUDIO_BYTES
from app.utils.validation import InvalidPDFError, validate_pdf

UNSUPPORTED_MESSAGE = "Diese Datei kann Zentriq nicht verarbeiten."
# Fuer die Erkennung genuegen die ersten Seiten (Seitenkopf und erste Vorgangszeilen).
DETECTION_PAGES = 3


@dataclass(frozen=True)
class Detection:
    kind: str | None  # "leipziger" | "memo" | None
    label: str
    allowed: bool = True


def extension(filename: str | None) -> str:
    name = os.path.basename(filename or "")
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _first_page_texts(content: bytes) -> list[str]:
    doc = fitz.open(stream=content, filetype="pdf")
    try:
        return [doc[index].get_text("text") for index in range(min(doc.page_count, DETECTION_PAGES))]
    finally:
        doc.close()


def detect(filename: str | None, content: bytes, *, can_import_lists: bool) -> Detection:
    ext = extension(filename)
    if ext in ALLOWED_AUDIO_EXTENSIONS:
        if not content or len(content) > MAX_AUDIO_BYTES:
            return Detection(None, "Die Sprachnachricht ist leer oder größer als 25 MB.", allowed=False)
        return Detection("memo", "Sprachnachricht erkannt")
    if ext == "pdf":
        try:
            validate_pdf(filename, content)
            is_list = is_wm312_list(_first_page_texts(content))
        except (InvalidPDFError, RuntimeError, ValueError):
            is_list = False
        if not is_list:
            return Detection(None, UNSUPPORTED_MESSAGE, allowed=False)
        if not can_import_lists:
            return Detection("leipziger", "Leipziger Listen kann nur der Büro-Admin hochladen.", allowed=False)
        return Detection("leipziger", "Leipziger Liste erkannt")
    return Detection(None, UNSUPPORTED_MESSAGE, allowed=False)
