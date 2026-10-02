"""Memo: lokale Transkription im Celery-Worker (laeuft nicht im Web-Request, damit auch laengere
Aufnahmen nicht am Proxy-Timeout scheitern). Die temporaere Audiodatei wird in jedem Fall
geloescht. Das Ergebnis liegt nur kurz im Result-Backend und wird beim Abholen entfernt
(dashboard.transcription_status -> AsyncResult.forget())."""

from celery import shared_task

from app.services import memo


# resultrepr_maxsize=1: Celery protokolliert den Rueckgabewert erfolgreicher Tasks - so landet
# der Transkripttext nie im Worker-Log.
@shared_task(ignore_result=False, resultrepr_maxsize=1)
def transcribe_memo(path: str) -> dict:
    try:
        return {"transcript": memo.transcribe_file(path)}
    except memo.MemoError as exc:
        return {"error": str(exc)}
    finally:
        memo.remove_temp_audio(path)
