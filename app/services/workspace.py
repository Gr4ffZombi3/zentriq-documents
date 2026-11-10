"""Persoenlicher Arbeitsplatz: Startansicht, Tagesbegruessung, "Was ist neu?", gespeicherte
Ansichten, Formularentwuerfe und Schnellsuche. Alles ohne KI-Aufrufe.

Rechte: Alle Daten sind je Benutzer gespeichert (user_id) und mandantengebunden. Gespeicherte
Ansichten und Suchtreffer verweisen nur auf Seiten, die der Benutzer ohnehin oeffnen darf -
die Zielseite prueft beim Oeffnen wie immer selbst (app/auth/permissions.py)."""

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app, url_for
from sqlalchemy import update

from app.auth.permissions import is_endpoint_allowed
from app.extensions import db
from app.models import FormDraft, OfficeNews, SavedView, UserPreference
from app.release_notes import RELEASE_NOTES

# Buerozeitzone fuer "erster Login pro Kalendertag" - fest, unabhaengig vom Server.
OFFICE_TZ = ZoneInfo("Europe/Berlin")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def office_today(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(OFFICE_TZ).date()


def is_office_member(user) -> bool:
    return bool(getattr(user, "is_authenticated", False) and (user.is_office_admin or user.is_employee))


def preferences(user, *, create: bool = False) -> UserPreference | None:
    pref = UserPreference.query.filter_by(user_id=user.id).first()
    if pref is None and create:
        pref = UserPreference(tenant_id=user.tenant_id, user_id=user.id)
        db.session.add(pref)
    return pref


# --- Startansicht ---------------------------------------------------------------------------

START_VIEWS = (
    ("zentriq.index", "Assistent"),
    ("portal.overview", "Übersicht"),
    ("calendar.index", "Kalender"),
    ("leipziger.index", "Leipziger Liste"),
    ("customers.list_customers", "Kunden"),
    ("timetracking.index", "Zeiterfassung"),
    ("huk.index", "Kfz-Angebote"),
)


def start_view_choices(user) -> list[tuple[str, str]]:
    """Nur Seiten, die die Rolle erreicht (Mitarbeiter z. B. keinen Kundenstamm)."""
    if not is_office_member(user):
        return []
    views = current_app.view_functions
    return [
        (endpoint, label)
        for endpoint, label in START_VIEWS
        if endpoint in views and is_endpoint_allowed(user, endpoint, endpoint.split(".")[0])
        and (endpoint != "zentriq.index" or current_app.config.get("ZENTRIQ_AI_ENABLED", True))
    ]


def start_endpoint(user) -> str | None:
    """Gewaehlte Startansicht, sofern (noch) erlaubt - sonst None (Standard)."""
    if not is_office_member(user):
        return None
    pref = preferences(user)
    if pref is None or not pref.start_view:
        return None
    return pref.start_view if pref.start_view in dict(start_view_choices(user)) else None


def set_start_view(user, endpoint: str) -> bool:
    if endpoint and endpoint not in dict(start_view_choices(user)):
        return False
    pref = preferences(user, create=True)
    pref.start_view = endpoint or None
    db.session.commit()
    return True


# --- Tagesbegruessung -----------------------------------------------------------------------


def record_login(user, now: datetime | None = None) -> None:
    """Nach jedem ERFOLGREICHEN Login: beim ersten Login eines Kalendertags (Europe/Berlin) wird
    die Begruessung faellig. Weitere Logins am selben Tag aendern nichts."""
    if not is_office_member(user):
        return
    today = office_today(now)
    pref = preferences(user, create=True)
    if pref.last_login_day != today:
        pref.last_login_day = today
        pref.greeting_due_on = today


def first_name(user) -> str:
    """Vorname aus dem hinterlegten Anzeigenamen - nie aus der E-Mail-Adresse."""
    profile = user.employee_profile
    name = (profile.display_name or "").strip() if profile is not None else ""
    if not name or "@" in name:
        return ""
    return name.split()[0]


GREETING_NEWS = 2  # hoechstens zwei Neuerungen in der Begruessung, der Rest unter "Was ist neu?"
APPOINTMENT_HORIZON = timedelta(days=14)
_WEEKDAYS = ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So.")


def next_appointment(user, now: datetime | None = None) -> dict | None:
    """Naechster eigener Termin (zustaendig, nicht abgesagt) in den kommenden 14 Tagen - nur fuer
    Rollen, die den Kalender oeffnen duerfen. Keine Kundendaten, nur Zeitpunkt."""
    if not is_office_member(user) or "calendar.index" not in current_app.view_functions:
        return None
    if not is_endpoint_allowed(user, "calendar.index", "calendar"):
        return None
    from app.models import CalendarEvent

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc).replace(tzinfo=None)
    event = (
        CalendarEvent.query.filter(
            CalendarEvent.assignee_user_id == user.id,
            CalendarEvent.status == "scheduled",
            CalendarEvent.starts_at >= now_utc,
            CalendarEvent.starts_at < now_utc + APPOINTMENT_HORIZON,
        )
        .order_by(CalendarEvent.starts_at, CalendarEvent.id)
        .first()
    )
    if event is None:
        return None
    local = event.starts_at.replace(tzinfo=timezone.utc).astimezone(OFFICE_TZ)
    today = office_today(now)
    clock = local.strftime("%H:%M Uhr")
    if local.date() == today:
        day = ""
    elif local.date() == today + timedelta(days=1):
        day = "morgen"
    else:
        day = f"am {_WEEKDAYS[local.weekday()]} {local.strftime('%d.%m.')}"
    return {"day": day, "time": clock, "url": url_for("calendar.index")}


def consume_greeting(user, endpoint: str | None, now: datetime | None = None) -> dict | None:
    """Begruessung genau einmal: nur auf der Startansicht und nur, wenn sie heute faellig ist.
    Das Leeren geschieht atomar (UPDATE ... WHERE greeting_due_on = heute) - ein Neuladen oder
    ein zweiter Tab, der gleichzeitig laedt, bekommt sie deshalb nicht noch einmal."""
    if not is_office_member(user) or endpoint is None:
        return None
    from app.navigation import home_endpoint_for

    if endpoint != home_endpoint_for(user):
        return None
    today = office_today(now)
    result = db.session.execute(
        update(UserPreference)
        .where(UserPreference.user_id == user.id, UserPreference.greeting_due_on == today)
        .values(greeting_due_on=None)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.session.rollback()
        return None
    pref = preferences(user)
    db.session.refresh(pref)
    since = pref.news_seen_at
    news = news_for(user, today)
    if since is None:
        fresh = news[:GREETING_NEWS]
    else:
        fresh = [item for item in news if item["published_at"] > since]
    pref.news_seen_at = _utcnow()
    db.session.commit()
    name = first_name(user)
    return {
        "text": f"Hallo {name}, wie geht’s dir heute?" if name else "Hallo, wie geht’s dir heute?",
        "appointment": next_appointment(user, now),
        "news": fresh[:GREETING_NEWS],
        "more": max(0, len(fresh) - GREETING_NEWS),
    }


# --- Was ist neu? ---------------------------------------------------------------------------

AUDIENCES = {"alle": "Alle", "admin": "Nur Büro-Admins", "mitarbeiter": "Nur Mitarbeiter"}


def _relevant(audience: str, user) -> bool:
    if audience == "admin":
        return user.is_office_admin
    if audience == "mitarbeiter":
        return user.is_employee
    return True


def news_for(user, today: date | None = None) -> list[dict]:
    """Veroeffentlichte Eintraege (Release + Buero), neueste zuerst, nur fuer die Rolle relevante.
    Eintraege mit Datum in der Zukunft erscheinen erst an diesem Tag."""
    if not is_office_member(user):
        return []
    today = today or office_today()
    items = [
        {
            "id": None,
            "date": note["date"],
            "title": note["title"],
            "text": note["text"],
            "audience": note["audience"],
            "office": False,
            "feature": note.get("feature"),
            "hint": note.get("hint"),
        }
        for note in RELEASE_NOTES
        if note["date"] <= today and _relevant(note["audience"], user)
    ]
    for row in OfficeNews.query.filter(OfficeNews.published_on <= today).all():
        if _relevant(row.audience, user):
            items.append(
                {"id": row.id, "date": row.published_on, "title": row.title, "text": row.body, "audience": row.audience, "office": True, "feature": None, "hint": None}
            )
    for item in items:
        # Kurzfassung fuer die Begruessung: erster Satz (vollstaendiger Text unter "Was ist neu?").
        first = re.split(r"(?<=[.!?])\s", item["text"].strip(), maxsplit=1)[0]
        item["summary"] = first
        # Beginn des Veroeffentlichungstags (Ortszeit) als UTC - Vergleich mit news_seen_at.
        start = datetime.combine(item["date"], datetime.min.time(), OFFICE_TZ)
        item["published_at"] = start.astimezone(timezone.utc).replace(tzinfo=None)
    items.sort(key=lambda item: (item["date"], item["office"]), reverse=True)
    return items


def mark_news_seen(user) -> None:
    if not is_office_member(user):
        return
    pref = preferences(user, create=True)
    pref.news_seen_at = _utcnow()
    db.session.commit()


# --- "Neu"-Markierung direkt an der Funktion ------------------------------------------------
# Dieselben Eintraege wie Begruessung und "Was ist neu?": ein Release-Eintrag mit "feature"
# (Schluessel im Template: new_badge("...")) und "hint" (ein Satz) markiert die Funktion, bis
# der Benutzer die Markierung einmal geoeffnet hat - laengstens FEATURE_BADGE_DAYS Tage.

FEATURE_BADGE_DAYS = 30
FEATURE_KEY = re.compile(r"^[a-z0-9-]{1,40}$")


def _seen_features(user) -> list[str]:
    pref = preferences(user)
    value = pref.seen_features if pref is not None else None
    return list(value) if isinstance(value, list) else []


def feature_news(user, key: str, today: date | None = None) -> dict | None:
    """Eintrag zur Markierung, falls sie fuer diesen Benutzer (noch) sichtbar ist."""
    if not is_office_member(user) or not FEATURE_KEY.fullmatch(key or ""):
        return None
    today = today or office_today()
    for item in news_for(user, today):
        if item["feature"] == key:
            if item["date"] < today - timedelta(days=FEATURE_BADGE_DAYS) or key in _seen_features(user):
                return None
            return item
    return None


def mark_feature_seen(user, key: str) -> bool:
    """Nur Schluessel bekannter Release-Eintraege; je Benutzer gespeichert (alle Geraete)."""
    if not is_office_member(user) or not any(note.get("feature") == key for note in RELEASE_NOTES):
        return False
    pref = preferences(user, create=True)
    seen = _seen_features(user)
    if key not in seen:
        pref.seen_features = (seen + [key])[-100:]
    db.session.commit()
    return True


# --- Listendichte ---------------------------------------------------------------------------

DENSITIES = {"geraeumig": "Geräumig", "kompakt": "Kompakt"}


def list_density(user) -> str:
    if not is_office_member(user):
        return "geraeumig"
    pref = preferences(user)
    return pref.list_density if pref is not None and pref.list_density in DENSITIES else "geraeumig"


def set_list_density(user, value: str) -> bool:
    if not is_office_member(user) or value not in DENSITIES:
        return False
    pref = preferences(user, create=True)
    pref.list_density = value
    db.session.commit()
    return True


class NewsError(ValueError):
    pass


def save_office_news(user, data, news: OfficeNews | None = None) -> OfficeNews:
    title = " ".join((data.get("title") or "").split())
    body = (data.get("body") or "").strip()
    audience = data.get("audience") or "alle"
    if not title or not body:
        raise NewsError("Bitte Überschrift und Text angeben.")
    if len(title) > 160 or len(body) > 2000:
        raise NewsError("Überschrift (160) oder Text (2000 Zeichen) ist zu lang.")
    if audience not in AUDIENCES:
        raise NewsError("Ungültige Zielgruppe.")
    try:
        published = date.fromisoformat(data.get("published_on") or "") if data.get("published_on") else office_today()
    except ValueError as exc:
        raise NewsError("Bitte ein gültiges Datum angeben.") from exc
    if news is None:
        news = OfficeNews(tenant_id=user.tenant_id, created_by_user_id=user.id)
        db.session.add(news)
    news.title, news.body, news.audience, news.published_on = title, body, audience, published
    db.session.commit()
    return news


# --- Gespeicherte Ansichten -----------------------------------------------------------------


def _leipziger_tabs():
    from app.services.leipziger_todo import TABS

    return TABS


# Seite -> (Endpoint, Bezeichnung, erlaubte Parameter mit Pruefung)
VIEW_PAGES = {
    "leipziger": (
        "leipziger.index",
        "Leipziger Liste",
        {
            "document_id": lambda v: v if v.isdigit() else None,
            "tab": lambda v: v if v in _leipziger_tabs() else None,
            "q": lambda v: v.strip()[:60] or None,
            "storno": lambda v: v if v == "ohne" else None,
        },
    ),
    "kunden": (
        "customers.list_customers",
        "Kunden",
        {
            "q": lambda v: v.strip()[:60] or None,
            "per_page": lambda v: v if v.isdigit() else None,
        },
    ),
}
MAX_VIEWS_PER_PAGE = 20


class ViewError(ValueError):
    pass


def page_allowed(user, page: str) -> bool:
    spec = VIEW_PAGES.get(page)
    return bool(spec and is_office_member(user) and is_endpoint_allowed(user, spec[0], spec[0].split(".")[0]))


def clean_params(page: str, raw) -> dict:
    allowed = VIEW_PAGES[page][2]
    params = {}
    for key, check in allowed.items():
        value = raw.get(key)
        if isinstance(value, str) and value:
            checked = check(value)
            if checked:
                params[key] = checked
    return params


def views_for(user, page: str) -> list[SavedView]:
    if not page_allowed(user, page):
        return []
    return SavedView.query.filter_by(user_id=user.id, page=page).order_by(SavedView.name).all()


def save_view(user, page: str, name: str, raw_params) -> SavedView:
    if not page_allowed(user, page):
        raise ViewError("Für diese Seite können keine Ansichten gespeichert werden.")
    name = " ".join((name or "").split())[:120]
    if not name:
        raise ViewError("Bitte einen Namen für die Ansicht angeben.")
    params = clean_params(page, raw_params)
    view = SavedView.query.filter_by(user_id=user.id, page=page, name=name).first()
    if view is None:
        if SavedView.query.filter_by(user_id=user.id, page=page).count() >= MAX_VIEWS_PER_PAGE:
            raise ViewError(f"Es können höchstens {MAX_VIEWS_PER_PAGE} Ansichten je Seite gespeichert werden.")
        view = SavedView(tenant_id=user.tenant_id, user_id=user.id, page=page, name=name, params=params)
        db.session.add(view)
    else:
        view.params = params  # gleicher Name: Ansicht aktualisieren
    db.session.commit()
    return view


def own_view(user, view_id: int) -> SavedView | None:
    return SavedView.query.filter_by(id=view_id, user_id=user.id).first()


def view_url(view: SavedView) -> str:
    endpoint = VIEW_PAGES[view.page][0]
    return url_for(endpoint, **clean_params(view.page, view.params or {}))


# --- Formularentwuerfe ----------------------------------------------------------------------

# Kfz-Angebot: neuer Vorgang bzw. bestehender Entwurf eines Auftrags
DRAFT_KEY = re.compile(r"^huk:(neu|\d{1,10})$")
MAX_DRAFT_BYTES = 20_000
DRAFT_MAX_AGE = timedelta(days=30)


class DraftError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _check_key(user, key: str) -> None:
    if not is_office_member(user) or not DRAFT_KEY.fullmatch(key or ""):
        raise DraftError("Unbekanntes Formular.", 404)
    job_part = key.split(":", 1)[1]
    if job_part != "neu":
        from app.services.huk import jobs

        try:
            jobs.get_job(user, int(job_part))
        except Exception as exc:  # fremder oder unbekannter Auftrag
            raise DraftError("Unbekanntes Formular.", 404) from exc


def get_draft(user, key: str) -> FormDraft | None:
    _check_key(user, key)
    draft = FormDraft.query.filter_by(user_id=user.id, form_key=key).first()
    if draft is not None and draft.updated_at < _utcnow() - DRAFT_MAX_AGE:
        db.session.delete(draft)
        db.session.commit()
        return None
    return draft


def save_draft(user, key: str, data) -> FormDraft:
    """Speichert nur die Eingaben (Feldname -> Text bzw. Liste von Texten). Startet nichts."""
    _check_key(user, key)
    if not isinstance(data, dict):
        raise DraftError("Ungültiger Entwurf.")
    clean = {}
    for name, value in data.items():
        if not isinstance(name, str) or len(name) > 60 or name in ("csrf_token", "aktion"):
            continue
        if isinstance(value, str):
            clean[name] = value[:2000]
        elif isinstance(value, list):
            clean[name] = [v[:200] for v in value if isinstance(v, str)][:20]
    import json

    if len(json.dumps(clean)) > MAX_DRAFT_BYTES:
        raise DraftError("Der Entwurf ist zu groß.", 413)
    draft = FormDraft.query.filter_by(user_id=user.id, form_key=key).first()
    if draft is None:
        draft = FormDraft(tenant_id=user.tenant_id, user_id=user.id, form_key=key, data=clean)
        db.session.add(draft)
    else:
        draft.data = clean
        draft.updated_at = _utcnow()
    db.session.commit()
    return draft


def delete_draft(user, key: str) -> bool:
    if not is_office_member(user) or not DRAFT_KEY.fullmatch(key or ""):
        return False
    deleted = FormDraft.query.filter_by(user_id=user.id, form_key=key).delete(synchronize_session=False)
    db.session.commit()
    return bool(deleted)


def serialize_draft(draft: FormDraft) -> dict:
    stamp = draft.updated_at.replace(tzinfo=timezone.utc).astimezone(OFFICE_TZ)
    return {"key": draft.form_key, "data": draft.data or {}, "updated_at": stamp.isoformat(), "updated_label": stamp.strftime("%d.%m.%Y, %H:%M Uhr")}


# --- Schnellsuche ---------------------------------------------------------------------------

# Zusaetzliche Suchbegriffe je Seite (Navigationseintraege kommen aus app/navigation.py).
_PAGE_ALIASES = {
    "settlements": "abrechnung abrechnungen provision",
    "calendar": "kalender termine",
    "leipziger": "leipziger liste vorgänge zu erledigen",
    "huk": "kfz angebot auto versicherung huk",
    "time": "zeiterfassung stempeln arbeitszeit",
    "customers": "kunden kundenstamm",
    "customer_search": "kunden suche",
    "voice": "memo sprachnachricht",
}


def _fold(text: str) -> str:
    return (text or "").lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")


def page_entries(nav_groups: list[dict]) -> list[dict]:
    pages = [{"label": "Assistent", "url": url_for("zentriq.index"), "terms": "assistent chat ki"}] if "zentriq.index" in current_app.view_functions else []
    for group in nav_groups:
        for entry in group["entries"]:
            pages.append({"label": entry["label"], "url": entry["url"], "terms": _PAGE_ALIASES.get(entry["key"], "")})
    pages.append({"label": "Was ist neu?", "url": url_for("workspace.news"), "terms": "neuigkeiten neuerungen release"})
    pages.append({"label": "Mein Konto", "url": url_for("settings.profile"), "terms": "profil konto einstellungen startansicht"})
    return pages


def search_pages(nav_groups: list[dict], query: str) -> list[dict]:
    needle = _fold(query.strip())
    if not needle:
        return []
    hits = []
    for page in page_entries(nav_groups):
        hay = _fold(page["label"] + " " + page["terms"])
        if needle in hay or any(word.startswith(needle) for word in hay.split()):
            hits.append({"label": page["label"], "url": page["url"]})
    seen, unique = set(), []
    for hit in hits:
        if hit["url"] not in seen:
            seen.add(hit["url"])
            unique.append(hit)
    return unique[:6]


def search_customer_hits(user, query: str) -> list[dict]:
    """Dieselbe Kundensuche wie Kopfzeile und Kalender (eigenes Buero). Buero-Admins oeffnen die
    Kundenakte, Mitarbeiter die Kundenkarte im Assistenten (sie haben keine Kundenakte)."""
    from app.services.global_search import MIN_QUERY_LENGTH, normalize_query, search_customers

    query = normalize_query(query)
    if len(query) < MIN_QUERY_LENGTH:
        return []
    rows, _more = search_customers(query)
    hits = []
    for row in rows[:8]:
        if user.is_office_admin:
            url = url_for("customers.detail", customer_id=row["id"])
        else:
            url = url_for("zentriq.index", kunde=row["id"])
        detail = " · ".join(str(v) for v in (row.get("customer_number"), row.get("city")) if v)
        hits.append({"label": row["name"], "detail": detail, "url": url})
    return hits
