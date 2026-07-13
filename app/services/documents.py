from app.extensions import db
from app.models import DocStatus, Document


def create_document(original_filename: str, stored_filename: str, file_path: str) -> Document:
    document = Document(
        filename=stored_filename,
        original_filename=original_filename,
        file_path=file_path,
        status=DocStatus.PENDING,
    )
    db.session.add(document)
    db.session.commit()
    return document
