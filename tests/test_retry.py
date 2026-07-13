from app.models import DocStatus, Document


def test_retry_reenqueues_failed_document(auth_client, db, tenant):
    document = Document(
        filename="failed.pdf",
        original_filename="failed.pdf",
        file_path="/does/not/exist.pdf",
        status=DocStatus.FAILED,
        error_message="OCR fehlgeschlagen: irgendwas",
        tenant_id=tenant.id,
    )
    db.session.add(document)
    db.session.commit()

    resp = auth_client.post(f"/documents/{document.id}/retry")
    assert resp.status_code == 302

    db.session.refresh(document)
    # Celery laeuft eager -> die (fehlschlagende OCR wegen fehlender Datei) ist bereits durch.
    assert document.retry_count == 1
    assert document.status == DocStatus.FAILED
    assert "OCR fehlgeschlagen" in document.error_message
