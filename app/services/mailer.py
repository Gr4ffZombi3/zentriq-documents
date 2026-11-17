import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from flask import current_app


class MailNotConfiguredError(RuntimeError):
    pass


def is_mail_configured() -> bool:
    config = current_app.config
    return bool(config.get("SMTP_HOST") and config.get("MAIL_FROM"))


def _one_line(value: str | None) -> str:
    return " ".join((value or "").split())


def send_email(to: str, subject: str, body: str, *, reply_to: str | None = None, sender_name: str | None = None) -> None:
    """Versendet eine Klartext-Mail per SMTP. Fail-closed: ohne SMTP_HOST/MAIL_FROM wird
    MailNotConfiguredError geworfen statt stillschweigend nichts zu tun.

    sender_name ersetzt nur den Anzeigenamen (z. B. Buero-Name); die Absenderadresse bleibt MAIL_FROM,
    weil der SMTP-Zugang nur dafuer senden darf (SPF/DKIM). reply_to: Antworten gehen dorthin."""
    if not is_mail_configured():
        raise MailNotConfiguredError("SMTP_HOST oder MAIL_FROM ist nicht konfiguriert.")

    config = current_app.config
    message = EmailMessage()
    if _one_line(sender_name):
        message["From"] = formataddr((_one_line(sender_name)[:120], parseaddr(config["MAIL_FROM"])[1]))
    else:
        message["From"] = config["MAIL_FROM"]
    if _one_line(reply_to):
        message["Reply-To"] = _one_line(reply_to)
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = make_msgid()
    message.set_content(body)

    context = ssl.create_default_context()
    timeout = config.get("SMTP_TIMEOUT_SECONDS", 30)
    if config.get("SMTP_USE_SSL"):
        smtp = smtplib.SMTP_SSL(config["SMTP_HOST"], config["SMTP_PORT"], timeout=timeout, context=context)
    else:
        smtp = smtplib.SMTP(config["SMTP_HOST"], config["SMTP_PORT"], timeout=timeout)

    with smtp:
        if not config.get("SMTP_USE_SSL") and config.get("SMTP_USE_STARTTLS"):
            smtp.starttls(context=context)
        if config.get("SMTP_USERNAME"):
            smtp.login(config["SMTP_USERNAME"], config.get("SMTP_PASSWORD") or "")
        smtp.send_message(message)
