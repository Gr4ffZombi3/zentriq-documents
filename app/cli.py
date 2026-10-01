"""CLI-Befehle (`flask <befehl>`), z. B. fuer die Anlage von Benutzern ohne offene Registrierung."""

import getpass
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from email_validator import EmailNotValidError, validate_email
from flask import current_app

from app.extensions import db
from app.models import (
    AnalysisRun,
    Customer,
    CustomerTimelineEvent,
    Document,
    DocumentCustomer,
    ListComparison,
    ListComparisonEntry,
    Recommendation,
    RecommendationFeedback,
    Task,
    Tenant,
    TenantStatus,
    User,
    UserRole,
)
from app.services.user_admin import find_user_by_vermittlernummer
from app.tenancy import bypass_tenant_scope
from app.utils.slugs import unique_tenant_slug
from app.utils.vermittlernummer import format_vermittlernummer

MIN_PASSWORD_LENGTH = 8


def _prompt_password(from_stdin: bool = False) -> str:
    # getpass statt click-Option: Das Passwort taucht weder in der Shell-History noch in der
    # Prozessliste auf und wird nirgends ausgegeben oder geloggt. --password-stdin erlaubt
    # die nicht-interaktive Uebergabe per Pipe (ebenfalls ohne Kommandozeilenargument).
    if from_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein.")
        return password
    password = getpass.getpass("Passwort: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise click.ClickException(f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen lang sein.")
    if getpass.getpass("Passwort wiederholen: ") != password:
        raise click.ClickException("Die Passwörter stimmen nicht überein.")
    return password


@click.command("create-user")
@click.option("--email", required=True, help="E-Mail-Adresse (Login) des neuen Benutzers.")
@click.option("--company", default=None, help="Firmenname; dafuer wird ein neuer Mandant angelegt.")
@click.option("--tenant", "tenant_slug", default=None, help="Slug eines bestehenden Mandanten (statt --company).")
@click.option(
    "--role",
    type=click.Choice([role.value for role in UserRole]),
    default=None,
    help="Rolle des Benutzers. Neuer Mandant: Standard office_admin. Bestehender Mandant: Pflicht.",
)
@click.option("--vermittlernummer", default=None, help="Optional: Vermittlernummer fuer den Login.")
def create_user_command(
    email: str, company: str | None, tenant_slug: str | None, role: str | None, vermittlernummer: str | None
):
    """Legt einen Benutzer an - entweder mit neuem Mandanten (--company) oder in einem
    bestehenden Mandanten (--tenant). Das Passwort wird verdeckt abgefragt. Hauptweg fuer
    weitere Benutzer ist die Admin-Seite Einstellungen -> Benutzer."""
    try:
        email = validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise click.ClickException(f"Ungültige E-Mail-Adresse: {exc}") from exc
    if bool(company) == bool(tenant_slug):
        raise click.ClickException("Genau eine der Optionen --company oder --tenant angeben.")
    if company is not None:
        company = company.strip()
        if not company or len(company) > 255:
            raise click.ClickException("--company darf nicht leer und hoechstens 255 Zeichen lang sein.")
        user_role = UserRole(role) if role else UserRole.OFFICE_ADMIN
    else:
        if not role:
            raise click.ClickException(
                "Fuer einen bestehenden Mandanten ist --role (office_admin/employee/super_admin) Pflicht."
            )
        user_role = UserRole(role)
        existing_tenant = Tenant.query.filter_by(slug=tenant_slug.strip()).first()
        if existing_tenant is None:
            raise click.ClickException(f"Mandant '{tenant_slug}' nicht gefunden.")
    vermittlernummer = _clean_vermittlernummer(vermittlernummer)

    with bypass_tenant_scope():
        if User.query.filter_by(email=email).first() is not None:
            raise click.ClickException("Diese E-Mail-Adresse ist bereits registriert.")
    if vermittlernummer and find_user_by_vermittlernummer(vermittlernummer) is not None:
        raise click.ClickException("Diese Vermittlernummer ist bereits registriert.")

    password = _prompt_password()

    with bypass_tenant_scope():
        if company is not None:
            tenant = Tenant(name=company, slug=unique_tenant_slug(company))
            db.session.add(tenant)
            db.session.flush()
        else:
            tenant = existing_tenant

        user = User(
            tenant_id=tenant.id, email=email, vermittlernummer=vermittlernummer, is_active=True, role=user_role
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        user_id, tenant_id, tenant_name, slug = user.id, tenant.id, tenant.name, tenant.slug

    click.echo(
        f"Benutzer {email} (ID {user_id}, Rolle {user_role.label}) im Mandanten '{tenant_name}' "
        f"(ID {tenant_id}, {slug}) angelegt."
    )


def _clean_vermittlernummer(value: str | None) -> str | None:
    value = format_vermittlernummer(value)
    if value is not None and len(value) > 50:
        raise click.ClickException("--vermittlernummer darf hoechstens 50 Zeichen lang sein.")
    return value


@click.command("set-admin")
@click.option("--email", required=True, help="E-Mail-Adresse (Login) des Admins.")
@click.option("--vermittlernummer", default=None, help="Vermittlernummer (wird einheitlich formatiert).")
@click.option("--tenant", "tenant_slug", default=None, help="Mandant (Slug) - nur noetig, wenn der Benutzer neu angelegt wird.")
@click.option("--password-stdin", is_flag=True, help="Passwort als eine Zeile von stdin lesen statt verdeckt abzufragen.")
def set_admin_command(email: str, vermittlernummer: str | None, tenant_slug: str | None, password_stdin: bool):
    """Legt einen Buero-Admin an oder aktualisiert einen bestehenden Benutzer mit dieser E-Mail
    (Rolle OFFICE_ADMIN, aktiv, Vermittlernummer, neues Passwort) - ohne Duplikat. Der Mandant eines
    bestehenden Benutzers bleibt unveraendert."""
    try:
        email = validate_email(email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as exc:
        raise click.ClickException(f"Ungültige E-Mail-Adresse: {exc}") from exc
    vermittlernummer = _clean_vermittlernummer(vermittlernummer)

    with bypass_tenant_scope():
        user = User.query.filter(db.func.lower(User.email) == email).first()
        tenant = None
        if user is None:
            if not tenant_slug:
                raise click.ClickException("Benutzer existiert nicht - fuer die Neuanlage --tenant angeben.")
            tenant = Tenant.query.filter_by(slug=tenant_slug.strip()).first()
            if tenant is None:
                raise click.ClickException(f"Mandant '{tenant_slug}' nicht gefunden.")
    if vermittlernummer:
        owner = find_user_by_vermittlernummer(vermittlernummer)
        if owner is not None and (user is None or owner.id != user.id):
            raise click.ClickException("Diese Vermittlernummer ist bereits einem anderen Benutzer zugeordnet.")

    password = _prompt_password(from_stdin=password_stdin)

    with bypass_tenant_scope():
        created = user is None
        if created:
            user = User(tenant_id=tenant.id, email=email)
            db.session.add(user)
        user.email = email
        user.role = UserRole.OFFICE_ADMIN
        user.is_active = True
        user.deleted_at = None
        if vermittlernummer:
            user.vermittlernummer = vermittlernummer
        user.set_password(password)
        user.invalidate_sessions()
        db.session.commit()
        user_id, tenant_id = user.id, user.tenant_id

    action = "angelegt" if created else "aktualisiert"
    click.echo(f"Admin {email} (ID {user_id}, Mandant-ID {tenant_id}) {action}.")


# Fachdaten (Importe und daraus abgeleitete Daten), die align-tenant entfernt - in
# Loeschreihenfolge (abhaengige Tabellen zuerst). Benutzer, Mandanten, Audit-Log, Zeiterfassung
# (Buchungen werden nie geloescht) und Sprachnachrichten/Postfach bleiben unangetastet.
BUSINESS_DATA_MODELS = (
    RecommendationFeedback,
    CustomerTimelineEvent,
    ListComparisonEntry,
    ListComparison,
    DocumentCustomer,
    AnalysisRun,
    Task,
    Recommendation,
    Document,
    Customer,
)


def _resolve_upload_path(file_path: str) -> Path:
    path = Path(file_path)
    if path.is_absolute():
        return path
    return Path(current_app.root_path).parent / path


@click.command("align-tenant")
@click.option("--tenant", "tenant_slug", required=True, help="Slug des einzigen produktiven Mandanten.")
@click.option("--admin-email", required=True, help="Admin, der diesem Mandanten zugeordnet wird.")
@click.option("--execute", is_flag=True, help="Aenderungen wirklich ausfuehren (ohne: nur Vorschau).")
def align_tenant_command(tenant_slug: str, admin_email: str, execute: bool):
    """Richtet die Datenbasis auf genau einen produktiven Mandanten aus: ordnet den Admin
    diesem Mandanten zu, entfernt alle importierten Fachdaten (Kunden, Dokumente, Aufgaben,
    Empfehlungen, Listen-Vergleiche - in allen Mandanten), deaktiviert alle Benutzer anderer
    Mandanten und setzt diese Mandanten auf SUSPENDED. Upload-Dateien entfernter Dokumente
    werden nach storage/backups/ verschoben, nicht geloescht. Ohne --execute nur Vorschau."""
    admin_email = admin_email.strip().lower()
    with bypass_tenant_scope():
        tenant = Tenant.query.filter_by(slug=tenant_slug.strip()).first()
        if tenant is None:
            raise click.ClickException(f"Mandant '{tenant_slug}' nicht gefunden.")
        admin = User.query.filter(db.func.lower(User.email) == admin_email).first()
        if admin is None:
            raise click.ClickException(f"Benutzer '{admin_email}' nicht gefunden.")

        other_users = User.query.filter(User.tenant_id != tenant.id, User.id != admin.id).all()
        other_tenants = Tenant.query.filter(Tenant.id != tenant.id).all()
        documents = Document.query.all()
        counts = {model.__tablename__: model.query.count() for model in BUSINESS_DATA_MODELS}

        click.echo(f"Ziel-Mandant: {tenant.slug} (ID {tenant.id})")
        click.echo(f"Admin {admin.email}: Mandant-ID {admin.tenant_id} -> {tenant.id}, Rolle ADMIN, aktiv")
        for table, count in counts.items():
            click.echo(f"  entferne {count:>5} Zeilen aus {table}")
        for user in other_users:
            click.echo(f"  deaktiviere Benutzer {user.email} (Mandant-ID {user.tenant_id})")
        for other in other_tenants:
            click.echo(f"  setze Mandant {other.slug} (ID {other.id}) auf SUSPENDED")
        if not execute:
            click.echo("Vorschau - nichts geaendert. Mit --execute ausfuehren.")
            return

        upload_paths = [_resolve_upload_path(document.file_path) for document in documents]

        admin.tenant_id = tenant.id
        admin.role = UserRole.OFFICE_ADMIN
        admin.is_active = True
        for user in other_users:
            user.is_active = False
        for other in other_tenants:
            other.status = TenantStatus.SUSPENDED
        for model in BUSINESS_DATA_MODELS:
            db.session.execute(model.__table__.delete())
        db.session.commit()

    archive_dir = (
        Path(current_app.root_path).parent
        / "storage"
        / "backups"
        / f"uploads-archiv-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    )
    moved = 0
    for path in upload_paths:
        if path.is_file():
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), archive_dir / path.name)
            moved += 1
    click.echo(f"Ausgefuehrt. {moved} Upload-Datei(en) nach {archive_dir} verschoben.")


@click.command("grant-super-admin")
@click.option("--email", default=None, help="E-Mail-Adresse des Kontos.")
@click.option("--vermittlernummer", default=None, help="Alternativ: Vermittlernummer des Kontos.")
@click.option("--execute", is_flag=True, help="Ohne diese Option nur Vorschau.")
def grant_super_admin_command(email: str | None, vermittlernummer: str | None, execute: bool):
    """Macht ein bestehendes Konto zum SUPER_ADMIN (Plattformbetreiber). Passwort, Login und
    2FA bleiben unveraendert. Achtung: ein SUPER_ADMIN hat danach KEINEN Zugriff mehr auf die
    fachlichen Daten seines bisherigen Bueros."""
    from app.models.audit_log import AuditEventType
    from app.services.audit import log_audit_event

    if bool(email) == bool(vermittlernummer):
        raise click.ClickException("Genau eine der Optionen --email oder --vermittlernummer angeben.")
    with bypass_tenant_scope():
        if email:
            user = User.query.filter(db.func.lower(User.email) == email.strip().lower()).first()
        else:
            user = find_user_by_vermittlernummer(vermittlernummer)
        if user is None or user.deleted_at is not None:
            raise click.ClickException("Konto nicht gefunden.")
        click.echo(f"Konto ID {user.id}, Mandant-ID {user.tenant_id}, Rolle {user.role.label}, aktiv={user.is_active}")
        if user.role == UserRole.SUPER_ADMIN:
            click.echo("Konto ist bereits SUPER_ADMIN - nichts zu tun.")
            return
        remaining_admins = User.query.filter(
            User.tenant_id == user.tenant_id,
            User.id != user.id,
            User.role == UserRole.OFFICE_ADMIN,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        ).count()
        click.echo(f"Verbleibende aktive Buero-Admins im bisherigen Mandanten: {remaining_admins}")
        if remaining_admins == 0:
            click.echo("WARNUNG: Das Buero haette danach keinen aktiven Buero-Admin mehr.")
        if not execute:
            click.echo("Vorschau - nichts geaendert. Mit --execute ausfuehren.")
            return
        old_role = user.role
        user.role = UserRole.SUPER_ADMIN
        user.is_active = True
        user.invalidate_sessions()
        db.session.commit()
        log_audit_event(
            AuditEventType.USER_UPDATED,
            tenant_id=user.tenant_id,
            details={
                "target_user_id": user.id,
                "changes": {"role": {"old": old_role.value, "new": UserRole.SUPER_ADMIN.value}},
                "source": "cli grant-super-admin",
            },
        )
    click.echo(f"Konto ID {user.id} ist jetzt SUPER_ADMIN.")


def register_cli(app) -> None:
    app.cli.add_command(create_user_command)
    app.cli.add_command(set_admin_command)
    app.cli.add_command(align_tenant_command)
    app.cli.add_command(grant_super_admin_command)
