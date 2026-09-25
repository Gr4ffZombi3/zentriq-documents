"""CLI-Befehle (`flask <befehl>`), z. B. fuer die Anlage von Benutzern ohne offene Registrierung."""

import getpass

import click
from email_validator import EmailNotValidError, validate_email

from app.extensions import db
from app.models import Tenant, User, UserRole
from app.tenancy import bypass_tenant_scope
from app.utils.slugs import unique_tenant_slug

MIN_PASSWORD_LENGTH = 8


def _prompt_password() -> str:
    # getpass statt click-Option: Das Passwort taucht weder in der Shell-History noch in der
    # Prozessliste auf und wird nirgends ausgegeben oder geloggt.
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
    help="Rolle des Benutzers. Neuer Mandant: Standard admin. Bestehender Mandant: Pflicht.",
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
        user_role = UserRole(role) if role else UserRole.ADMIN
    else:
        if not role:
            raise click.ClickException("Fuer einen bestehenden Mandanten ist --role (admin/mitarbeiter) Pflicht.")
        user_role = UserRole(role)
        existing_tenant = Tenant.query.filter_by(slug=tenant_slug.strip()).first()
        if existing_tenant is None:
            raise click.ClickException(f"Mandant '{tenant_slug}' nicht gefunden.")
    vermittlernummer = (vermittlernummer or "").strip() or None
    if vermittlernummer is not None and len(vermittlernummer) > 50:
        raise click.ClickException("--vermittlernummer darf hoechstens 50 Zeichen lang sein.")

    with bypass_tenant_scope():
        if User.query.filter_by(email=email).first() is not None:
            raise click.ClickException("Diese E-Mail-Adresse ist bereits registriert.")
        if vermittlernummer and User.query.filter_by(vermittlernummer=vermittlernummer).first() is not None:
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


def register_cli(app) -> None:
    app.cli.add_command(create_user_command)
