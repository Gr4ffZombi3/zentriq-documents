"""Termine aus Bildern (Screenshot, Kamerafoto) in den Buerokalender uebernehmen.

Ablauf:
1. Bild pruefen (serverseitig): Endung UND tatsaechliches Format (JPEG, PNG, WebP, HEIC/HEIF),
   Groesse, Pixelzahl. HEIC wird mit pillow-heif dekodiert (falls installiert), EXIF-Drehung
   beruecksichtigt, nach RGB konvertiert. Die Datei wird nicht gespeichert.
2. Text lokal per Tesseract (keine KI, kein externer Dienst).
3. Termine lokal erkennen: Datum (09.11.2026, 9.11., "Mo, 9. Nov", "9. November"), Uhrzeit
   ("16:30", "16.30 Uhr", "16 Uhr", Spannen "16:30-17:15"), Titel = Rest der Zeile bzw. naechste
   Zeile. Datumszeilen ohne Uhrzeit gelten als Ueberschrift fuer die folgenden Zeilen.
4. Nur wenn der Text Datums-/Zeitangaben enthaelt, lokal aber nichts eindeutig zuzuordnen ist,
   darf ein Modell helfen - mit ausschliesslich den relevanten Zeilen; das Ergebnis wird wie
   eine lokale Erkennung geprueft.
5. Vorschau: belegt? moegliche Dublette? Speichern ueber booking.book (Sperre, erneute
   Verfuegbarkeitspruefung, gleiche Regeln wie im Eingabefeld), Ergebnis je Termin."""

from __future__ import annotations

import io
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, time, timedelta

from flask import current_app

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGES = 3
MAX_PIXELS = 40_000_000
EXTENSIONS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "heic": "HEIF", "heif": "HEIF"}
FORMATS = {"JPEG", "PNG", "WEBP", "HEIF", "HEIC", "MPO"}  # MPO = JPEG mit Zusatzbild (manche Kameras)
MONTHS = {"jan": 1, "januar": 1, "feb": 2, "februar": 2, "mär": 3, "märz": 3, "maerz": 3, "mrz": 3, "apr": 4, "april": 4,
          "mai": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
          "okt": 10, "oktober": 10, "nov": 11, "november": 11, "dez": 12, "dezember": 12}
# Befehl "Termine aus dem Bild uebernehmen" - Muster gilt identisch in JavaScript (Template-Attribut
# data-appt-pattern), daher ohne \b vor Umlauten und ohne Python-Sonderkonstrukte.
_W = r"[a-zäöü]*"
_END = r"(?![a-zäöü])"
# zusammenhaengende Verben ("uebernehmen", "eintragen") und trennbare ("trage ... ein", "lies ... aus", "lege ... an")
_VERB = rf"(?:eintrag{_W}|übernimm{_W}|übernehm{_W}|uebernimm{_W}|uebernehm{_W}|importier{_W}|speicher{_W}|auslesen|erfass{_W}|anleg{_W}|einles{_W})"
_SPLIT = [(rf"trag{_W}", "ein"), (rf"lies{_W}", "aus"), (rf"les{_W}", "aus"), (rf"leg{_W}", "an"), (rf"nimm{_W}", "auf")]
PATTERN = "|".join(
    [rf"termin{_W}(?:\s.*)?\s{_VERB}", rf"{_VERB}(?:\s.*)?\stermin", r"in\s+den\s+kalender"]
    + [rf"(?:^|\s){verb}\s(?:.*\s)?termin{_W}(?:\s.*)?\s{particle}{_END}" for verb, particle in _SPLIT]
    + [rf"termin{_W}(?:\s.*)?\s{particle}\s?{verb}" for verb, particle in _SPLIT]
)
REQUEST = re.compile(PATTERN, re.I)
_IMAGE_WORD = re.compile(r"(?:bild|screenshot|bildschirmfoto|foto|anhang|aufnahme|scan|datei)", re.I)


class ImageError(Exception):
    pass


@dataclass
class Candidate:
    day: str | None = None  # JJJJ-MM-TT
    start: str | None = None  # HH:MM
    minutes: int | None = None
    title: str = ""
    source: str = ""  # erkannte Textzeile(n), zur Kontrolle
    problems: list[str] = field(default_factory=list)


def summary_lines(items: list[dict]) -> list[str]:
    """Kurzliste fuer den Chatverlauf (deutsches Datumsformat)."""
    out = []
    for i in items:
        day = date.fromisoformat(i["day"]).strftime("%d.%m.%Y") if i.get("day") else "Datum fehlt"
        out.append(f"- {day}, {i.get('start') or 'Uhrzeit fehlt'} – {i.get('title')}")
    return out


def is_request(text: str | None) -> bool:
    """Befehl zusammen mit angehaengten Bildern."""
    return bool(REQUEST.search(" " + (text or "") + " "))


def is_chat_request(text: str | None) -> bool:
    """Befehl OHNE Anhang, bezogen auf das zuvor im Chat hochgeladene Bild ("Trage die Termine aus dem
    Bild ein"). Verlangt einen Bildbezug - "Termin mit X morgen um 10 eintragen" bleibt ein normaler
    Kalenderbefehl."""
    t = text or ""
    return is_request(t) and bool(_IMAGE_WORD.search(t)) and len(t) <= 200


# --- 1. Bild -------------------------------------------------------------------------------


def _heif_available() -> bool:
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        return True
    except ImportError:
        return False


def load_image(filename: str, data: bytes):
    """Geprueftes RGB-Bild oder ImageError (verstaendlich, ohne technische Details)."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in EXTENSIONS:
        raise ImageError(f"„{filename}“: Bitte ein Foto oder einen Screenshot (JPEG, PNG, WebP, HEIC) wählen.")
    if not data:
        raise ImageError(f"„{filename}“ ist leer.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"„{filename}“ ist zu groß (höchstens {MAX_IMAGE_BYTES // (1024 * 1024)} MB).")
    if EXTENSIONS[extension] == "HEIF" and not _heif_available():
        raise ImageError(f"„{filename}“: HEIC-Fotos werden auf diesem Server noch nicht unterstützt. Bitte als JPEG teilen "
                         "(iPhone: Einstellungen › Kamera › Formate › „Maximale Kompatibilität“).")
    _heif_available()
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        image = Image.open(io.BytesIO(data))
        if image.format not in FORMATS:
            raise ImageError(f"„{filename}“ ist kein unterstütztes Bild (erkannt: {image.format or 'unbekannt'}).")
        if image.width * image.height > MAX_PIXELS:
            raise ImageError(f"„{filename}“ hat zu viele Bildpunkte.")
        image.load()
        image = ImageOps.exif_transpose(image)  # Kamerafotos: Ausrichtung laut EXIF
    except ImageError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError) as exc:
        raise ImageError(f"„{filename}“ kann nicht als Bild gelesen werden.") from exc
    return image.convert("RGB")


def read_text(image) -> str:
    """Tesseract lokal; kleine Screenshots werden vergroessert (bessere Erkennung)."""
    from PIL import Image

    from app.services.ocr import tesseract_ocr

    if image.width < 1400:
        factor = min(3, 1400 / max(1, image.width) + 0.5)
        image = image.resize((int(image.width * factor), int(image.height * factor)), Image.LANCZOS)
    gray = image.convert("L")
    return tesseract_ocr.ocr_image_lines(gray)


# --- 2. Lokale Erkennung -------------------------------------------------------------------

_DATE_NUM = re.compile(r"(?<![\d:])(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(\d{4}|\d{2})(?!\d))?")
_DATE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DATE_WORD = re.compile(r"\b(\d{1,2})\.?\s+(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\.?(?:\s+(\d{4}))?\b", re.I)
_RANGE = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*(?:uhr)?\s*(?:-|–|—|bis)\s*(\d{1,2})[:.](\d{2})\s*(?:uhr)?", re.I)
_TIME = re.compile(r"(?<![\d.])(\d{1,2})[:.](\d{2})(?![.\d])\s*(?:uhr)?|\b(\d{1,2})\s*uhr\b", re.I)
_WEEKDAY = re.compile(r"\b(?:mo|di|mi|do|fr|sa|so|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b\.?,?", re.I)
_NOISE = re.compile(r"^[\W_]+|[\W_]+$")
_SKIP_TITLE = re.compile(r"^(?:ganztägig|ganztaegig|heute|morgen|kalender|termine?|woche|monat|tag)$", re.I)


def _valid_date(y, m, d) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _find_date(line: str, today: date) -> tuple[date | None, str]:
    for rx in (_DATE_ISO, _DATE_WORD, _DATE_NUM):
        m = rx.search(line)
        if not m:
            continue
        if rx is _DATE_ISO:
            value = _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif rx is _DATE_WORD:
            year = int(m.group(3)) if m.group(3) else today.year
            value = _valid_date(year, MONTHS[m.group(2).lower().rstrip(".")], int(m.group(1)))
            if value and not m.group(3) and value < today - timedelta(days=60):
                value = _valid_date(year + 1, value.month, value.day)
        else:
            y = m.group(3)
            year = (int(y) + 2000 if len(y) == 2 else int(y)) if y else today.year
            value = _valid_date(year, int(m.group(2)), int(m.group(1)))
            if value and not y and value < today - timedelta(days=60):
                value = _valid_date(year + 1, value.month, value.day)
        if value:
            return value, line[: m.start()] + " " + line[m.end():]
    return None, line


def _find_time(line: str) -> tuple[time | None, int | None, str]:
    m = _RANGE.search(line)
    if m:
        h1, m1, h2, m2 = (int(x) for x in m.groups())
        if h1 < 24 and m1 < 60 and h2 < 24 and m2 < 60:
            minutes = (h2 * 60 + m2) - (h1 * 60 + m1)
            return time(h1, m1), (minutes if 5 <= minutes <= 12 * 60 else None), line[: m.start()] + " " + line[m.end():]
    m = _TIME.search(line)
    if m:
        h, mm = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), 0)
        if h < 24 and mm < 60:
            return time(h, mm), None, line[: m.start()] + " " + line[m.end():]
    return None, None, line


def _clean_title(text: str) -> str:
    text = _WEEKDAY.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    text = _NOISE.sub("", text.strip())
    return "" if (len(text) < 2 or _SKIP_TITLE.match(text)) else text[:120]


def extract(text: str, today: date) -> list[Candidate]:
    lines = [" ".join(line.split()) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    found: list[Candidate] = []
    current_day: date | None = None
    for i, line in enumerate(lines):
        day, rest = _find_date(line, today)
        start, minutes, rest = _find_time(rest)
        if day and start is None:
            current_day = day  # Datumsueberschrift
            title_only = _clean_title(rest)
            if title_only:  # "9.11. Zahnarzt" ohne Uhrzeit -> Rueckfrage in der Vorschau
                found.append(Candidate(day=day.isoformat(), title=title_only, source=line, problems=["Uhrzeit fehlt"]))
            continue
        if start is None:
            continue
        title = _clean_title(rest)
        source = line
        if not title and day is None and current_day is None:
            continue  # nur eine Uhrzeit vor jedem Datum: Statusleiste/Kopfzeile ("09:41"), kein Termin
        if not title and i + 1 < len(lines):
            nxt = lines[i + 1]
            if _find_time(nxt)[0] is None and _find_date(nxt, today)[0] is None:
                title = _clean_title(nxt)
                source = f"{line} / {nxt}"
        use_day = day or current_day
        c = Candidate(day=use_day.isoformat() if use_day else None, start=start.strftime("%H:%M"), minutes=minutes,
                      title=title or "Termin", source=source)
        if use_day is None:
            c.problems.append("Datum fehlt")
        if not title:
            c.problems.append("Titel nicht lesbar")
        found.append(c)
    return found


def relevant_lines(text: str, limit: int = 40) -> list[str]:
    """Nur Zeilen mit Datum/Uhrzeit und ihre direkten Nachbarn (fuer einen Modell-Fallback)."""
    lines = [" ".join(x.split()) for x in (text or "").splitlines() if x.strip()]
    keep = set()
    for i, line in enumerate(lines):
        if _find_time(line)[0] is not None or _find_date(line, date.today())[0] is not None:
            keep.update({i - 1, i, i + 1})
    return [lines[i] for i in sorted(k for k in keep if 0 <= k < len(lines))][:limit]


def _model_extract(user, lines: list[str], today: date) -> list[Candidate]:
    """Modell nur fuer schwer zuzuordnenden Text; Ergebnis wird lokal geprueft."""
    from app.services.ai import router

    if not lines or not router.can_answer():
        return []
    prompt = ("Ordne die folgenden per Texterkennung gelesenen Zeilen Terminen zu. Antworte NUR mit JSON: "
              '[{"datum":"JJJJ-MM-TT","uhrzeit":"HH:MM","dauer_minuten":null,"titel":"..."}]. Nur Termine, die '
              f"im Text stehen; nichts erfinden; fehlende Angaben als null. Heute ist {today.isoformat()}.\n\n" + "\n".join(lines))
    try:
        completion = router.complete(system="Du extrahierst Termine aus Text. Nur JSON.", messages=[{"role": "user", "content": prompt}],
                                     user_id=user.id, tenant_id=user.tenant_id, customer_data=False, purpose="image_appointments",
                                     max_tokens=800)
    except router.AiError:
        return []
    raw = completion.text or ""
    raw = raw[raw.find("["): raw.rfind("]") + 1] if "[" in raw else "[]"
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        c = Candidate(title=_clean_title(str(row.get("titel") or ""))[:120] or "Termin", source="(Zuordnung per KI aus den erkannten Zeilen)")
        try:
            c.day = date.fromisoformat(str(row.get("datum"))).isoformat()
        except ValueError:
            c.problems.append("Datum fehlt")
        if re.fullmatch(r"\d{2}:\d{2}", str(row.get("uhrzeit") or "")) and int(str(row["uhrzeit"])[:2]) < 24:
            c.start = str(row["uhrzeit"])
        else:
            c.problems.append("Uhrzeit fehlt")
        if isinstance(row.get("dauer_minuten"), int) and 5 <= row["dauer_minuten"] <= 720:
            c.minutes = row["dauer_minuten"]
        out.append(c)
    return out


# --- 3. Vorschau und Speichern -------------------------------------------------------------


def _default_minutes() -> int:
    from app.services.calendar_intent import _office_default_minutes

    return _office_default_minutes() or 30


def check(user, c: Candidate, assignee) -> dict:
    """Status fuer die Vorschau: frei / belegt / moegliche Dublette / unvollstaendig."""
    from app.models import CalendarEvent
    from app.services import availability
    from app.services import calendar as cal
    from app.services.timetracking.clock import local_to_utc_naive, utcnow_naive

    row = asdict(c)
    row["minutes"] = c.minutes or _default_minutes()
    row["assignee_id"] = assignee.id if assignee else None
    row["flags"] = []
    if not (c.day and c.start) or assignee is None:
        row["state"] = "incomplete"
        return row
    start_utc = local_to_utc_naive(date.fromisoformat(c.day), time(int(c.start[:2]), int(c.start[3:])))
    end_utc = start_utc + timedelta(minutes=row["minutes"])
    if start_utc < utcnow_naive():
        row["flags"].append("liegt in der Vergangenheit")
    same = CalendarEvent.query.filter(CalendarEvent.status == "scheduled", CalendarEvent.starts_at == start_utc).all()
    if any(_similar(e.title, c.title) for e in same):
        row["flags"].append("mögliche Dublette (gleiche Zeit, ähnlicher Titel bereits im Kalender)")
    busy = cal.conflicts_for(assignee.id, start_utc, end_utc)
    if busy:
        row["flags"].append(f"belegt: {cal.member_name(assignee)} hat dann „{busy[0].title}“")
    s = availability.settings()
    if availability.planning_ready(s) and not busy:
        reason = availability.check(assignee, start_utc, row["minutes"], s, now=min(utcnow_naive(), start_utc))
        if reason and "Vergangenheit" not in reason:
            row["flags"].append(reason)
    row["state"] = "warn" if row["flags"] else "ok"
    return row


def _similar(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9äöüß]", "", (s or "").lower())  # noqa: E731
    x, y = norm(a), norm(b)
    return bool(x and y and (x in y or y in x))


def analyse(user, uploads: list[tuple[str, bytes]], today: date) -> dict:
    """uploads: [(dateiname, bytes)]. Ergebnis fuer die Vorschau inkl. Rueckfragen."""
    if not uploads:
        raise ImageError("Bitte mindestens ein Bild anhängen.")
    if len(uploads) > MAX_IMAGES:
        raise ImageError(f"Höchstens {MAX_IMAGES} Bilder auf einmal.")
    return analyse_texts(user, [(name, read_text(load_image(name, data))) for name, data in uploads], today)


def analyse_texts(user, texts: list[tuple[str, str]], today: date) -> dict:
    """texts: [(dateiname, per OCR erkannter Text)]. Fehlerarten je Bild in "image_status":
    unreadable (Text nicht lesbar) | none (keine Termine erkannt) | ok."""
    from app.services import calendar as cal

    problems, image_status = [], []
    candidates: list[Candidate] = []
    used_model = False
    for name, text in texts:
        local = extract(text, today)
        if not any(c.start for c in local):
            lines = relevant_lines(text)
            if lines:
                via_model = _model_extract(user, lines, today)
                if via_model:
                    used_model = True
                    local = via_model
        readable = len(re.sub(r"\W", "", text or "")) >= 12
        if not local and not readable:
            image_status.append({"name": name, "status": "unreadable"})
            problems.append(f"„{name}“: Text nicht lesbar – bitte ein schärferes Bild oder einen Screenshot verwenden.")
        elif not local:
            image_status.append({"name": name, "status": "none"})
            problems.append(f"„{name}“: Text erkannt, aber keine Termine (Datum und Uhrzeit) gefunden.")
        else:
            image_status.append({"name": name, "status": "ok", "count": len(local)})
        candidates.extend(local)
    # Dubletten innerhalb der Bilder (gleicher Termin auf zwei Screenshots)
    seen, unique = set(), []
    for c in candidates:
        key = (c.day, c.start, re.sub(r"\W", "", c.title.lower()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    me = next((m for m in cal.office_members() if m.id == user.id), None)
    rows = [check(user, c, me) for c in unique]
    for i, row in enumerate(rows):
        row["ref"] = f"bild-{i}"
    return {"items": rows, "problems": problems, "image_status": image_status, "used_model": used_model, "members": cal.member_payload(cal.office_members()),
            "default_minutes": _default_minutes(), "lines": sum(len(t.splitlines()) for _, t in texts)}


def save(user, items: list[dict], batch_ref: str) -> list[dict]:
    """Ausgewaehlte Termine einzeln buchen (erneute Pruefung unter Sperre). Ergebnis je Termin."""
    from app.services import booking
    from app.services import calendar as cal
    from app.services.timetracking.clock import local_to_utc_naive

    members = {m.id: m for m in cal.office_members()}
    results = []
    for i, item in enumerate(items[:30]):
        ref = str(item.get("ref") or i)
        title = " ".join(str(item.get("title") or "").split())[:120] or "Termin"
        try:
            day = date.fromisoformat(str(item.get("day")))
            hh, mm = (int(x) for x in str(item.get("start")).split(":"))
            start = time(hh, mm)
            minutes = int(item.get("minutes") or _default_minutes())
        except (TypeError, ValueError):
            results.append({"ref": ref, "status": "incomplete", "message": "Datum oder Uhrzeit fehlt bzw. ist ungültig."})
            continue
        if not 5 <= minutes <= 720:
            results.append({"ref": ref, "status": "incomplete", "message": "Die Dauer ist unplausibel."})
            continue
        assignee = members.get(int(item.get("assignee_id") or 0))
        if assignee is None:
            results.append({"ref": ref, "status": "incomplete", "message": "Bitte einen Mitarbeiter des Büros auswählen."})
            continue
        try:
            result = booking.book(user, assignee=assignee, start_utc=local_to_utc_naive(day, start), minutes=minutes, title=title,
                                  client_ref=f"{batch_ref}-{ref}"[:64])
        except cal.CalendarError as exc:
            results.append({"ref": ref, "status": "conflict", "message": str(exc)})
            continue
        if result.reason:
            alt = ("Frei wäre: " + "; ".join(a.label() for a in result.alternatives) + ".") if result.alternatives else ""
            results.append({"ref": ref, "status": "conflict", "message": f"Nicht übernommen: {result.reason} {alt}".strip()})
        elif not result.created:
            results.append({"ref": ref, "status": "duplicate", "message": "War bereits im Kalender – nicht doppelt angelegt."})
        else:
            when = cal.local_iso(result.event.starts_at)
            results.append({"ref": ref, "status": "saved", "event_id": result.event.id,
                            "message": f"Übernommen: {day.strftime('%d.%m.%Y')} {when[11:16]} Uhr, {title}, {cal.member_name(assignee)}."})
    current_app.logger.info("image_appointments.saved user=%s items=%s saved=%s", user.id, len(results),
                            sum(1 for r in results if r["status"] == "saved"))
    return results

