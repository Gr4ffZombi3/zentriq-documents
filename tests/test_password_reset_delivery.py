"""Passwort vergessen mit echtem SMTP-Versand (lokaler Test-SMTP-Server per TCP): Mail-Eingang,
Reset-Link auf PUBLIC_URL, Passwortaenderung, Anmeldung mit neuem/altem Passwort - sowie
serverseitige Protokollierung, wenn der Mailversand nicht konfiguriert ist."""

import email
import logging
import socketserver
import threading
from email import policy

import pytest

from app.cli import send_test_mail_command
from app.models import SystemErrorEvent
from tests.test_password_reset_flow import GENERIC, PASSWORD, _forgot, _make, _reload, _reset
from tests.two_factor_helpers import totp_code

PUBLIC_URL = "https://www.zentriqai.de"


class _SMTPHandler(socketserver.StreamRequestHandler):
    """Minimaler SMTP-Empfaenger (RFC 5321-Grundbefehle) - genug fuer smtplib.send_message."""

    def _reply(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode())

    def handle(self):
        self._reply("220 test-smtp ready")
        while True:
            line = self.rfile.readline().decode(errors="replace").rstrip("\r\n")
            if not line:
                return
            command = line.split(" ", 1)[0].upper()
            if command in ("EHLO", "HELO"):
                self._reply("250 test-smtp")
            elif command in ("MAIL", "RCPT", "RSET", "NOOP"):
                self._reply("250 OK")
            elif command == "DATA":
                self._reply("354 End data with <CR><LF>.<CR><LF>")
                lines = []
                while True:
                    data_line = self.rfile.readline()
                    if data_line in (b".\r\n", b".\n", b""):
                        break
                    lines.append(data_line[1:] if data_line.startswith(b"..") else data_line)
                self.server.inbox.append(email.message_from_bytes(b"".join(lines), policy=policy.default))
                self._reply("250 queued")
            elif command == "QUIT":
                self._reply("221 bye")
                return
            else:
                self._reply("502 not implemented")


@pytest.fixture()
def smtp_server(app):
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _SMTPHandler)
    server.daemon_threads = True
    server.inbox = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app.config.update(
        SMTP_HOST="127.0.0.1",
        SMTP_PORT=server.server_address[1],
        SMTP_USE_STARTTLS=False,
        SMTP_USE_SSL=False,
        SMTP_USERNAME=None,
        MAIL_FROM="Zentriq <noreply@zentriqai.de>",
        PUBLIC_URL=PUBLIC_URL,
    )
    yield server.inbox
    server.shutdown()
    server.server_close()


def _login(client, identifier, password):
    return client.post(
        "/auth/login", data={"login_type": "email", "identifier": identifier, "password": password}
    )


def test_reset_mail_is_delivered_and_new_password_works(client, tenant, smtp_server):
    user, secret = _make(tenant, "reset-test@example.com")

    response = _forgot(client, "reset-test@example.com")
    assert GENERIC in response.get_data(as_text=True)

    assert len(smtp_server) == 1
    message = smtp_server[0]
    assert message["To"] == "reset-test@example.com"
    body = message.get_content()
    link = next(line for line in body.splitlines() if line.startswith("https://"))
    # Korrekte Domain, Token im Fragment (nie im Pfad/Query).
    assert link.startswith(f"{PUBLIC_URL}/auth/reset-password#token=")
    token = link.split("#token=", 1)[1]
    assert "30 Minuten gültig" in body and "nur einmal" in body

    assert _reset(client, token, totp_code(user, secret), "brandneu-passwort-7").status_code == 302
    refreshed = _reload(user.id)
    assert refreshed.check_password("brandneu-passwort-7")
    assert not refreshed.check_password(PASSWORD)

    # Anmeldung: neues Passwort fuehrt zur 2FA-Abfrage, altes wird abgelehnt.
    new_login = _login(client, "reset-test@example.com", "brandneu-passwort-7")
    assert new_login.status_code == 302 and "/auth/2fa" in new_login.headers["Location"]
    with client.session_transaction() as flask_session:
        flask_session.clear()
    old_login = _login(client, "reset-test@example.com", PASSWORD)
    assert old_login.status_code == 200  # Formular erneut mit Fehlermeldung, keine Weiterleitung

    # Der Link ist verbraucht.
    again = _reset(client, token, totp_code(user, secret), "drittes-passwort-3")
    assert again.headers["Location"].endswith("/auth/forgot-password")
    assert _reload(user.id).check_password("brandneu-passwort-7")


def test_unknown_address_gets_same_answer_and_no_mail(client, tenant, smtp_server):
    _make(tenant, "bekannt@example.com")
    response = _forgot(client, "unbekannt@example.com")
    assert GENERIC in response.get_data(as_text=True)
    assert smtp_server == []


def test_missing_mail_config_is_logged_with_setting_names(app, client, tenant, caplog):
    app.config.update(SMTP_HOST=None, MAIL_FROM=None, PUBLIC_URL="")
    _make(tenant, "ohne-mail@example.com")

    with caplog.at_level(logging.ERROR):
        response = _forgot(client, "ohne-mail@example.com")

    assert GENERIC in response.get_data(as_text=True)  # neutrale Antwort bleibt
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "SMTP_HOST" in logged and "MAIL_FROM" in logged and "PUBLIC_URL" in logged
    assert SystemErrorEvent.query.filter_by(source="mail").count() == 1


def test_send_test_mail_cli(app, smtp_server):
    runner = app.test_cli_runner()
    result = runner.invoke(send_test_mail_command, ["--to", "admin@example.com"])
    assert result.exit_code == 0, result.output
    assert len(smtp_server) == 1 and smtp_server[0]["To"] == "admin@example.com"
    assert PUBLIC_URL in smtp_server[0].get_content()


def test_send_test_mail_cli_names_missing_settings(app):
    app.config.update(SMTP_HOST=None, MAIL_FROM=None, PUBLIC_URL="")
    result = app.test_cli_runner().invoke(send_test_mail_command, ["--to", "admin@example.com"])
    assert result.exit_code != 0
    assert "SMTP_HOST" in result.output and "MAIL_FROM" in result.output and "PUBLIC_URL" in result.output
