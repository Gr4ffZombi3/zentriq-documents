import fitz

ALLOWED_EXTENSION = ".pdf"
PDF_MAGIC = b"%PDF-"


class InvalidPDFError(ValueError):
    pass


def validate_pdf(filename: str, file_bytes: bytes) -> None:
    if not filename.lower().endswith(ALLOWED_EXTENSION):
        raise InvalidPDFError("Nur PDF-Dateien werden unterstützt.")
    if not file_bytes.startswith(PDF_MAGIC):
        raise InvalidPDFError("Datei ist keine gültige PDF-Datei.")
    try:
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        try:
            if doc.page_count < 1:
                raise InvalidPDFError("PDF enthält keine Seiten.")
        finally:
            doc.close()
    except InvalidPDFError:
        raise
    except Exception as exc:
        raise InvalidPDFError(f"PDF konnte nicht geöffnet werden: {exc}") from exc
