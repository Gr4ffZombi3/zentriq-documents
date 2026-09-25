import imaplib
from dataclasses import dataclass


class MailboxConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImapMessage:
    uid: str
    raw_message: bytes


class ImapMailboxReader:
    def __init__(self, config):
        self.host = config.get("PLACETEL_IMAP_HOST")
        self.port = int(config.get("PLACETEL_IMAP_PORT", 993))
        self.username = config.get("PLACETEL_IMAP_USERNAME")
        self.password = config.get("PLACETEL_IMAP_PASSWORD")
        self.folder = config.get("PLACETEL_IMAP_FOLDER", "INBOX")
        self.use_ssl = bool(config.get("PLACETEL_IMAP_SSL", True))
        if not all((self.host, self.username, self.password, self.folder)):
            raise MailboxConfigurationError("IMAP-Host, Benutzername, Passwort und Ordner müssen konfiguriert sein.")

    def fetch_since(self, last_uid: str | None) -> list[ImapMessage]:
        client_class = imaplib.IMAP4_SSL if self.use_ssl else imaplib.IMAP4
        with client_class(self.host, self.port) as client:
            client.login(self.username, self.password)
            status, _ = client.select(self.folder, readonly=True)
            if status != "OK":
                raise RuntimeError("Der konfigurierte IMAP-Ordner konnte nicht geöffnet werden.")
            start_uid = int(last_uid) + 1 if last_uid and last_uid.isdigit() else 1
            status, data = client.uid("search", None, f"UID {start_uid}:*")
            if status != "OK":
                raise RuntimeError("Die IMAP-Suche ist fehlgeschlagen.")
            messages: list[ImapMessage] = []
            for uid_bytes in data[0].split():
                status, payload = client.uid("fetch", uid_bytes, "(BODY.PEEK[])")
                if status != "OK":
                    raise RuntimeError("Eine IMAP-Nachricht konnte nicht gelesen werden.")
                raw_message = next(
                    (part[1] for part in payload if isinstance(part, tuple) and isinstance(part[1], bytes)),
                    None,
                )
                if raw_message is not None:
                    messages.append(ImapMessage(uid_bytes.decode("ascii"), raw_message))
            return messages
