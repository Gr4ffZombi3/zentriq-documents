"""Buerologo: Hochladen (Kalender-Einstellungen, nur Buero-Admin) und Ausliefern fuer die
oeffentlichen Buchungsseiten.

Sicherheit: Es werden nur Rasterbilder (PNG, JPEG, WebP) angenommen - kein SVG, weil SVG Skripte
enthalten kann. Jedes Bild wird mit Pillow vollstaendig dekodiert, auf hoechstens 600 x 240 px
verkleinert und als frisches PNG gespeichert: Metadaten (EXIF/GPS), eingebettete Fremdinhalte und
"Polyglot"-Dateien gehen dabei verloren. Ausgeliefert wird es unter /termin/logo/<rev>.png mit
festem Bildtyp, nosniff und eigener CSP."""

import hashlib
import io

from flask import url_for

from app.extensions import db
from app.models.office_settings import OfficeSettings

MAX_UPLOAD_BYTES = 3 * 1024 * 1024
MAX_SOURCE_PIXELS = 40_000_000  # Schutz vor "Dekompressionsbomben"
BOX = (600, 240)
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}


class LogoError(ValueError):
    pass


def process(data: bytes) -> bytes:
    """Bilddaten pruefen und als verkleinertes PNG zurueckgeben. Wirft LogoError."""
    from PIL import Image, UnidentifiedImageError

    if not data:
        raise LogoError("Bitte eine Bilddatei auswählen.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise LogoError("Das Logo ist zu groß (höchstens 3 MB).")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            width, height = probe.size
        if fmt not in ALLOWED_FORMATS:
            raise LogoError("Bitte ein Logo als PNG, JPG oder WebP hochladen.")
        if width * height > MAX_SOURCE_PIXELS or width < 16 or height < 16:
            raise LogoError("Das Bild hat ungeeignete Abmessungen.")
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image = image.convert("RGBA")
    except LogoError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise LogoError("Die Datei ist kein lesbares Bild. Bitte PNG, JPG oder WebP verwenden.") from None
    image.thumbnail(BOX, Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def save(user, data: bytes) -> OfficeSettings:
    png = process(data)
    record = OfficeSettings.query.first()
    if record is None:
        record = OfficeSettings(tenant_id=user.tenant_id)
        db.session.add(record)
    record.logo_png = png
    record.logo_rev = hashlib.sha256(png).hexdigest()[:16]
    record.updated_by_user_id = user.id
    db.session.commit()
    return record


def remove(user) -> None:
    record = OfficeSettings.query.first()
    if record is not None and record.logo_rev:
        record.logo_png = None
        record.logo_rev = None
        record.updated_by_user_id = user.id
        db.session.commit()


def url_of(settings: OfficeSettings | None, *, external: bool = False) -> str | None:
    if settings is None or not settings.logo_rev:
        return None
    return url_for("public_booking.logo", rev=settings.logo_rev, _external=external)
