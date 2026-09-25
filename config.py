import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(BASE_DIR / "storage" / "uploads"))
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024

    CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
    CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
    CELERY_TASK_ALWAYS_EAGER = False

    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
    OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o")
    OPENAI_TRANSCRIPTION_MODEL = os.environ.get("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
    MAILBOX_CLASSIFICATION_MODEL = os.environ.get("MAILBOX_CLASSIFICATION_MODEL", OPENAI_MODEL)

    # Placetel-Mailbox (fail-closed: ohne explizite Merkmale wird keine Nachricht erkannt)
    PLACETEL_MAILBOX_ENABLED = os.environ.get("PLACETEL_MAILBOX_ENABLED", "false").lower() == "true"
    PLACETEL_TENANT_ID = int(os.environ["PLACETEL_TENANT_ID"]) if os.environ.get("PLACETEL_TENANT_ID") else None
    PLACETEL_IMAP_HOST = os.environ.get("PLACETEL_IMAP_HOST")
    PLACETEL_IMAP_PORT = int(os.environ.get("PLACETEL_IMAP_PORT", "993"))
    PLACETEL_IMAP_USERNAME = os.environ.get("PLACETEL_IMAP_USERNAME")
    PLACETEL_IMAP_PASSWORD = os.environ.get("PLACETEL_IMAP_PASSWORD")
    PLACETEL_IMAP_FOLDER = os.environ.get("PLACETEL_IMAP_FOLDER", "INBOX")
    PLACETEL_IMAP_SSL = os.environ.get("PLACETEL_IMAP_SSL", "true").lower() == "true"
    PLACETEL_SENDER_PATTERNS = os.environ.get("PLACETEL_SENDER_PATTERNS", "")
    PLACETEL_SUBJECT_PATTERNS = os.environ.get("PLACETEL_SUBJECT_PATTERNS", "")
    PLACETEL_CALLER_ID_HEADERS = os.environ.get(
        "PLACETEL_CALLER_ID_HEADERS", "X-Caller-ID,X-Caller-Number,Caller-Number"
    )
    PLACETEL_POLL_INTERVAL_SECONDS = int(os.environ.get("PLACETEL_POLL_INTERVAL_SECONDS", "120"))
    MAILBOX_AUDIO_MAX_MB = int(os.environ.get("MAILBOX_AUDIO_MAX_MB", "25"))
    MAILBOX_DRY_RUN = os.environ.get("MAILBOX_DRY_RUN", "true").lower() == "true"
    MAILBOX_MIN_PHONE_CONFIDENCE = float(os.environ.get("MAILBOX_MIN_PHONE_CONFIDENCE", "0.85"))
    MAILBOX_MIN_DAMAGE_CONFIDENCE = float(os.environ.get("MAILBOX_MIN_DAMAGE_CONFIDENCE", "0.85"))

    # HUK: zwei unabhaengige Schalter verhindern versehentliches Live-Absenden.
    HUK_AUTOMATION_ENABLED = os.environ.get("HUK_AUTOMATION_ENABLED", "false").lower() == "true"
    HUK_FORM_URL = os.environ.get(
        "HUK_FORM_URL",
        "https://www.huk.de/hukinfo/af/form.html?formname=/rueckruf/rueckrufservice",
    )
    HUK_BROWSER_HEADLESS = os.environ.get("HUK_BROWSER_HEADLESS", "true").lower() == "true"
    HUK_BROWSER_TIMEOUT_MS = int(os.environ.get("HUK_BROWSER_TIMEOUT_MS", "30000"))

    TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
    OCR_MIN_CONFIDENCE = float(os.environ.get("OCR_MIN_CONFIDENCE", "60"))
    OCR_MIN_TEXT_LENGTH = int(os.environ.get("OCR_MIN_TEXT_LENGTH", "20"))
    LEIPZIGER_LISTE_PAGE_BATCH_SIZE = int(os.environ.get("LEIPZIGER_LISTE_PAGE_BATCH_SIZE", "1"))

    # M12: Analyse-Engine
    FIELD_CONFIDENCE_UNCERTAIN_THRESHOLD = float(os.environ.get("FIELD_CONFIDENCE_UNCERTAIN_THRESHOLD", "70"))
    ANALYSIS_ENGINE_VERSION = os.environ.get("ANALYSIS_ENGINE_VERSION", "m14.3")
    ANALYSIS_PROMPT_VERSION = os.environ.get("ANALYSIS_PROMPT_VERSION", "v2")
    ANALYSIS_NARRATIVE_ENABLED = os.environ.get("ANALYSIS_NARRATIVE_ENABLED", "true").lower() == "true"
    ANALYSIS_NARRATIVE_MODEL = os.environ.get("ANALYSIS_NARRATIVE_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o"))

    # Session-/Cookie-Haertung
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # Oeffentliche Basis-URL fuer Links in E-Mails. Bewusst NICHT aus dem Host-Header des
    # Requests abgeleitet, damit Reset-Links nicht per Host-Header-Injection umgelenkt werden.
    PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

    # Passwort-Reset (fail-closed: ohne SMTP_HOST, MAIL_FROM und PUBLIC_URL wird nichts versendet)
    PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS = int(os.environ.get("PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS", "1800"))
    PASSWORD_RESET_MAX_REQUESTS_PER_HOUR = int(os.environ.get("PASSWORD_RESET_MAX_REQUESTS_PER_HOUR", "3"))

    # SMTP-Versand
    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    SMTP_USE_STARTTLS = os.environ.get("SMTP_USE_STARTTLS", "true").lower() == "true"
    SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "false").lower() == "true"
    SMTP_TIMEOUT_SECONDS = int(os.environ.get("SMTP_TIMEOUT_SECONDS", "30"))
    MAIL_FROM = os.environ.get("MAIL_FROM")


class DevelopmentConfig(BaseConfig):
    DEBUG = True


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    CELERY_TASK_ALWAYS_EAGER = True
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    # Verhindert echte/gemockte OpenAI-Aufrufe fuer den Analysebericht-Text in der gesamten
    # bestehenden Testsuite; der Narrativ-Pfad wird gezielt in test_analysis_report.py getestet.
    ANALYSIS_NARRATIVE_ENABLED = False
    PLACETEL_MAILBOX_ENABLED = False
    MAILBOX_DRY_RUN = True
    HUK_AUTOMATION_ENABLED = False
    PUBLIC_URL = "https://zentriq.test"
    SMTP_HOST = None
    MAIL_FROM = None


class ProductionConfig(BaseConfig):
    DEBUG = False


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config():
    env = os.environ.get("FLASK_ENV", "development")
    return CONFIG_MAP.get(env, DevelopmentConfig)
