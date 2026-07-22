from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.enums import DocStatus

STEP_DEFINITIONS = [
    {
        "key": "uploaded",
        "label": "Dokument hochgeladen",
        "description": "Die PDF wurde gespeichert und in die Queue aufgenommen.",
    },
    {
        "key": "ocr",
        "label": "OCR gestartet",
        "description": "Der Dokumenttext wird aus dem PDF gelesen.",
    },
    {
        "key": "parser",
        "label": "Vertragsdaten werden erkannt",
        "description": "Layout, Tabellen und Struktur werden vorbereitet.",
    },
    {
        "key": "ai",
        "label": "KI erstellt Auswertung",
        "description": "Inhalte, Felder und Signale werden extrahiert.",
    },
    {
        "key": "grouping",
        "label": "Kunden werden gruppiert",
        "description": "Empfehlungen, Aufgaben und Zuordnungen werden gespeichert.",
    },
    {
        "key": "done",
        "label": "Fertig",
        "description": "Die Ergebnisse stehen in der Oberflaeche bereit.",
    },
]

ACTIVE_STATUS_VALUES = {
    DocStatus.PENDING.value,
    DocStatus.OCR_PROCESSING.value,
    DocStatus.OCR_DONE.value,
    DocStatus.AI_PROCESSING.value,
}

STEP_KEYS = [step["key"] for step in STEP_DEFINITIONS]


def is_document_active_status(status: DocStatus | str | None) -> bool:
    if status is None:
        return False
    status_value = status.value if hasattr(status, "value") else str(status)
    return status_value in ACTIVE_STATUS_VALUES


def make_progress_snapshot(
    *,
    completed: list[str] | tuple[str, ...] | None = None,
    active: str | None = None,
    failed: str | None = None,
    percent: int = 0,
    headline: str,
    detail: str | None = None,
    state: str = "running",
    stage_durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    completed_set = set(completed or [])
    steps: list[dict[str, Any]] = []

    for definition in STEP_DEFINITIONS:
        step_state = "pending"
        if definition["key"] in completed_set:
            step_state = "done"
        if active and definition["key"] == active:
            step_state = "active"
        if failed and definition["key"] == failed:
            step_state = "failed"
        steps.append(
            {
                "key": definition["key"],
                "label": definition["label"],
                "description": definition["description"],
                "state": step_state,
            }
        )

    return {
        "state": state,
        "percent": max(0, min(int(percent), 100)),
        "headline": headline,
        "detail": detail or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stage_durations": dict(stage_durations or {}),
        "steps": steps,
    }


def merge_progress_into_extra_data(extra_data: dict | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    merged = dict(extra_data or {})
    merged["analysis_progress"] = snapshot
    return merged


def build_document_progress(document) -> dict[str, Any]:
    stored = (document.extra_data or {}).get("analysis_progress")
    if isinstance(stored, dict):
        return _normalize_snapshot(stored, document)
    return _fallback_progress(document)


def _normalize_snapshot(snapshot: dict[str, Any], document) -> dict[str, Any]:
    step_by_key = {step["key"]: step for step in STEP_DEFINITIONS}
    normalized_steps = []
    for key in STEP_KEYS:
        incoming = next((step for step in snapshot.get("steps", []) if step.get("key") == key), None) or {}
        definition = step_by_key[key]
        normalized_steps.append(
            {
                "key": key,
                "label": incoming.get("label") or definition["label"],
                "description": incoming.get("description") or definition["description"],
                "state": incoming.get("state", "pending"),
            }
        )

    return {
        "state": snapshot.get("state", "done" if getattr(document.status, "value", document.status) == "done" else "running"),
        "percent": max(0, min(int(snapshot.get("percent", 0)), 100)),
        "headline": snapshot.get("headline") or _fallback_progress(document)["headline"],
        "detail": snapshot.get("detail", ""),
        "updated_at": snapshot.get("updated_at"),
        "stage_durations": dict(snapshot.get("stage_durations") or {}),
        "steps": normalized_steps,
    }


def _fallback_progress(document) -> dict[str, Any]:
    status = document.status.value if hasattr(document.status, "value") else str(document.status)
    error_message = getattr(document, "error_message", None)

    if status == DocStatus.PENDING.value:
        return make_progress_snapshot(
            completed=["uploaded"],
            active="ocr",
            percent=12,
            headline="Upload abgeschlossen",
            detail="Die Analyse wartet auf den Worker-Start.",
        )
    if status == DocStatus.OCR_PROCESSING.value:
        return make_progress_snapshot(
            completed=["uploaded"],
            active="ocr",
            percent=28,
            headline="OCR laeuft",
            detail="Der Dokumenttext wird gerade erkannt.",
        )
    if status == DocStatus.OCR_DONE.value:
        return make_progress_snapshot(
            completed=["uploaded", "ocr"],
            active="parser",
            percent=52,
            headline="OCR abgeschlossen",
            detail="Die Struktur des Dokuments wird vorbereitet.",
        )
    if status == DocStatus.AI_PROCESSING.value:
        return make_progress_snapshot(
            completed=["uploaded", "ocr", "parser"],
            active="ai",
            percent=76,
            headline="KI-Auswertung laeuft",
            detail="Felder, Kunden und Signale werden extrahiert.",
        )
    if status == DocStatus.FAILED.value:
        return make_progress_snapshot(
            completed=["uploaded"],
            failed="ai",
            percent=100,
            headline="Analyse fehlgeschlagen",
            detail=error_message or "Die Verarbeitung konnte nicht abgeschlossen werden.",
            state="failed",
        )
    return make_progress_snapshot(
        completed=STEP_KEYS,
        percent=100,
        headline="Analyse abgeschlossen",
        detail="Das Dokument steht fuer Review und Folgearbeit bereit.",
        state="done",
        stage_durations=((document.extra_data or {}).get("analysis_progress") or {}).get("stage_durations"),
    )
