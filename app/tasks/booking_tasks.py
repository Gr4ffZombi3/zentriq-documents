from celery import shared_task


@shared_task(name="app.tasks.booking_tasks.send_booking_confirmation")
def send_booking_confirmation(confirmation_id: int):
    """Bestaetigungs-E-Mail fuer eine oeffentliche Buchung (app/services/booking_confirmation.py)."""
    from app.services.booking_confirmation import deliver

    return deliver(confirmation_id)
