"""Erkennt automatisch, ob ein Leipziger-Liste-Dokument nur die Daten des hochladenden
Vermittlers (OWN) oder die komplette Geschaeftsstellen-Liste mehrerer Vermittler
(GESCHAEFTSSTELLE) enthaelt: ueber die Anzahl unterschiedlicher broker_number-Werte
(M12-Feld, "VM-Nummer im Dokument") ueber alle Zeilen des Dokuments. 0 oder 1 eindeutiger
Wert -> OWN, 2 oder mehr -> GESCHAEFTSSTELLE. Rein heuristisch (siehe Document.list_scope
Docstring: bei OCR-Rauschen oder einem Ein-Personen-Buero kann das fehlschlagen) - deshalb
zusaetzlich beim Upload manuell uebersteuerbar, siehe app/blueprints/upload/routes.py."""

from app.models import Document
from app.models.enums import ListScope


def detect_list_scope(document: Document) -> ListScope:
    broker_numbers: set[str] = set()
    for doc_customer in document.document_customers:
        for row in doc_customer.row_data or []:
            broker_number = row.get("broker_number")
            if broker_number:
                broker_numbers.add(broker_number)
    return ListScope.GESCHAEFTSSTELLE if len(broker_numbers) >= 2 else ListScope.OWN
