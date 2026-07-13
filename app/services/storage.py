import uuid
from pathlib import Path

from flask import current_app


def save_pdf(file_bytes: bytes) -> tuple[str, str]:
    """Speichert PDF-Bytes unter einem generierten Dateinamen und gibt (filename, file_path) zurück."""
    upload_folder = Path(current_app.config["UPLOAD_FOLDER"])
    upload_folder.mkdir(parents=True, exist_ok=True)

    stored_filename = f"{uuid.uuid4().hex}.pdf"
    file_path = upload_folder / stored_filename
    file_path.write_bytes(file_bytes)
    return stored_filename, str(file_path)


def resolve_document_path(file_path: str) -> Path:
    """Löst einen gespeicherten file_path sicher auf und stellt sicher, dass er im Upload-Ordner liegt."""
    upload_folder = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    resolved = Path(file_path).resolve()
    if upload_folder != resolved and upload_folder not in resolved.parents:
        raise ValueError("Ungültiger Dateipfad außerhalb des Upload-Ordners.")
    return resolved
