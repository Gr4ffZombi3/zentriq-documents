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

    # Memo-Transkription: lokal mit faster-whisper (app/services/memo.py), ohne externe API.
    # "small" (int8): ca. 6x schneller als Echtzeit auf 2 vCPU, ~650 MB RAM nur waehrend der
    # Transkription (eigener Prozess). Alternativen: "base" (schneller, ungenauer), "medium".
    WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
    WHISPER_MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", str(BASE_DIR / "storage" / "models" / "whisper"))
    WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
    WHISPER_CPU_THREADS = int(os.environ.get("WHISPER_CPU_THREADS", str(os.cpu_count() or 2)))
    WHISPER_BEAM_SIZE = int(os.environ.get("WHISPER_BEAM_SIZE", "1"))
    TRANSCRIPTION_TMP_DIR = os.environ.get("TRANSCRIPTION_TMP_DIR", str(BASE_DIR / "storage" / "tmp" / "memo"))
    TRANSCRIPTION_MAX_MINUTES = int(os.environ.get("TRANSCRIPTION_MAX_MINUTES", "20"))
    TRANSCRIPTION_TIMEOUT_SECONDS = int(os.environ.get("TRANSCRIPTION_TIMEOUT_SECONDS", "900"))

    # KI-Assistent (app/services/assistant.py): Anthropic API, ausschliesslich serverseitig.
    # Ohne ANTHROPIC_API_KEY ist der Assistent ausgeblendet; ASSISTANT_ENABLED=false schaltet
    # ihn auch mit Key ab.
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
    ASSISTANT_ENABLED = os.environ.get("ASSISTANT_ENABLED", "true").lower() == "true"
    ASSISTANT_MODEL = os.environ.get("ASSISTANT_MODEL", "claude-opus-5-5")
    ASSISTANT_EFFORT = os.environ.get("ASSISTANT_EFFORT", "low")
    ASSISTANT_TIMEOUT_SECONDS = float(os.environ.get("ASSISTANT_TIMEOUT_SECONDS", "50"))
    ASSISTANT_MAX_INPUT_CHARS = int(os.environ.get("ASSISTANT_MAX_INPUT_CHARS", "8000"))
    ASSISTANT_MAX_OUTPUT_TOKENS = int(os.environ.get("ASSISTANT_MAX_OUTPUT_TOKENS", "4000"))

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

    # Zeitzone fuer die Zeiterfassung (Tagesgrenzen, Anzeige). Gespeichert wird immer UTC.
    APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Europe/Berlin")

    # Session-/Cookie-Haertung
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # Offene Selbstregistrierung (legt pro Registrierung einen neuen Mandanten an). Standard:
    # aus. Benutzer werden dann per `flask create-user` angelegt.
    REGISTRATION_ENABLED = os.environ.get("REGISTRATION_ENABLED", "false").lower() == "true"

    # Oeffentliche Basis-URL fuer Links in E-Mails. Bewusst NICHT aus dem Host-Header des
    # Requests abgeleitet, damit Reset-Links nicht per Host-Header-Injection umgelenkt werden.
    PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

    # Passwort-Reset (fail-closed: ohne SMTP_HOST, MAIL_FROM und PUBLIC_URL wird nichts versendet)
    PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS = int(os.environ.get("PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS", "1800"))
    PASSWORD_RESET_MAX_REQUESTS_PER_HOUR = int(os.environ.get("PASSWORD_RESET_MAX_REQUESTS_PER_HOUR", "3"))
    # Zusaetzliche Drosselung pro Client-IP (unabhaengig davon, ob die Adresse existiert).
    PASSWORD_RESET_MAX_REQUESTS_PER_IP_PER_HOUR = int(
        os.environ.get("PASSWORD_RESET_MAX_REQUESTS_PER_IP_PER_HOUR", "10")
    )

    # Zwei-Faktor-Authentifizierung (TOTP). Ist TWO_FACTOR_ENFORCED aktiv, muss jedes Konto
    # 2FA einrichten, bevor es irgendeinen anderen Bereich erreicht. Konten MIT 2FA muessen
    # den Code unabhaengig von diesem Schalter immer eingeben.
    TWO_FACTOR_ENFORCED = os.environ.get("TWO_FACTOR_ENFORCED", "true").lower() == "true"
    TWO_FACTOR_ISSUER = os.environ.get("TWO_FACTOR_ISSUER", "Zentriq")
    TWO_FACTOR_MAX_FAILURES = int(os.environ.get("TWO_FACTOR_MAX_FAILURES", "5"))
    TWO_FACTOR_LOCK_MINUTES = int(os.environ.get("TWO_FACTOR_LOCK_MINUTES", "15"))
    # Wie lange nach korrektem Passwort der 2FA-Code eingegeben werden kann.
    TWO_FACTOR_PENDING_MAX_AGE_SECONDS = int(os.environ.get("TWO_FACTOR_PENDING_MAX_AGE_SECONDS", "300"))
    TWO_FACTOR_RECOVERY_CODE_COUNT = 10

    # Brute-Force-Schutz fuer die Anmeldung (fehlgeschlagene Versuche pro Client-IP).
    LOGIN_MAX_FAILURES_PER_IP = int(os.environ.get("LOGIN_MAX_FAILURES_PER_IP", "20"))
    LOGIN_FAILURE_WINDOW_MINUTES = int(os.environ.get("LOGIN_FAILURE_WINDOW_MINUTES", "15"))

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
    REGISTRATION_ENABLED = False
    # Die bestehende Testsuite meldet Konten ohne 2FA an. Die Pflicht-Einrichtung wird in
    # tests/test_two_factor.py gezielt mit TWO_FACTOR_ENFORCED = True geprueft.
    TWO_FACTOR_ENFORCED = False
    PUBLIC_URL = "https://zentriq.test"
    SMTP_HOST = None
    MAIL_FROM = None
    # Nie echte Anthropic-Aufrufe in Tests; tests/test_assistant.py setzt einen Test-Key und
    # ersetzt den Client.
    ANTHROPIC_API_KEY = None


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
