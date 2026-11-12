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

    # Live-Zusammenarbeit (Dienst zentriq-live, live_service/): Anwesenheit und Aenderungshinweise
    # in Kundenakten. Ohne laufenden Dienst bleibt die Oberflaeche still (kein Fehler).
    LIVE_ENABLED = os.environ.get("LIVE_ENABLED", "1").lower() not in ("0", "false", "no")
    LIVE_REDIS_URL = os.environ.get("LIVE_REDIS_URL") or CELERY_BROKER_URL
    # Anruferkennung ueber Webex Calling (app/services/telephony.py): Secret der Webhook-Registrierung.
    # Ohne Secret nimmt der Webhook nichts an (503). Optional: nur Meldungen dieser Webex-Org-ID.
    WEBEX_WEBHOOK_SECRET = os.environ.get("WEBEX_WEBHOOK_SECRET", "")
    WEBEX_ORG_ID = os.environ.get("WEBEX_ORG_ID", "")
    # Placetel Notify ("Anruf an externe API melden"): gemeinsames Secret aus Einstellungen > Externe APIs.
    PLACETEL_NOTIFY_SECRET = os.environ.get("PLACETEL_NOTIFY_SECRET", "")
    # Hybridbetrieb KI (app/services/local_ai.py): eigener Rechner mit lokalem Modell holt Auftraege ab.
    LOCAL_AI_ENABLED = os.environ.get("LOCAL_AI_ENABLED", "true").lower() == "true"
    LOCAL_AI_LEASE_SECONDS = int(os.environ.get("LOCAL_AI_LEASE_SECONDS", "300"))
    LOCAL_AI_ONLINE_SECONDS = int(os.environ.get("LOCAL_AI_ONLINE_SECONDS", "90"))
    LOCAL_AI_MAX_ATTEMPTS = int(os.environ.get("LOCAL_AI_MAX_ATTEMPTS", "3"))
    LOCAL_AI_QUEUE_DAYS = int(os.environ.get("LOCAL_AI_QUEUE_DAYS", "7"))
    LOCAL_AI_AUTO_MIN_CHARS = int(os.environ.get("LOCAL_AI_AUTO_MIN_CHARS", "1500"))
    LOCAL_AI_POLL_SECONDS = int(os.environ.get("LOCAL_AI_POLL_SECONDS", "5"))
    # Aufbewahrung der Anrufe (telephony_calls): aeltere werden bei jedem neuen Anruf und per
    # flask telefonie aufraeumen geloescht. 0 = nie.
    TELEPHONY_CALL_RETENTION_DAYS = int(os.environ.get("TELEPHONY_CALL_RETENTION_DAYS", "7"))

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

    # Kfz-Beitragsberechnung ueber den oeffentlichen HUK-Webrechner (app/services/huk/):
    # Playwright (headless Chromium) im Celery-Worker, hoechstens ein Browser gleichzeitig.
    HUK_ENABLED = os.environ.get("HUK_ENABLED", "true").lower() == "true"
    HUK_RUN_TIMEOUT_SECONDS = int(os.environ.get("HUK_RUN_TIMEOUT_SECONDS", "150"))
    HUK_MAX_ATTEMPTS = int(os.environ.get("HUK_MAX_ATTEMPTS", "2"))
    # Unter diesem freien Arbeitsspeicher (MB) wartet ein Auftrag, statt Chromium zu starten.
    HUK_MIN_FREE_MB = int(os.environ.get("HUK_MIN_FREE_MB", "350"))
    HUK_STORAGE_DIR = os.environ.get("HUK_STORAGE_DIR", str(BASE_DIR / "storage" / "huk"))
    HUK_BROWSER_TMP_DIR = os.environ.get("HUK_BROWSER_TMP_DIR", str(BASE_DIR / "storage" / "tmp" / "huk"))
    # Belege (Screenshot der Rechnerseite) werden nach dieser Frist geloescht; das Ergebnis bleibt.
    HUK_EVIDENCE_RETENTION_DAYS = int(os.environ.get("HUK_EVIDENCE_RETENTION_DAYS", "90"))
    # Claude (eigene Anbindung, nicht der ZentriqAI-Router): nur zum Uebernehmen von Angaben aus
    # einem Freitext. 0 = aus. Kostenobergrenze je Auftrag in USD; Modell/Preise einstellbar.
    HUK_AI_MAX_COST_USD = float(os.environ.get("HUK_AI_MAX_COST_USD", "0"))
    HUK_AI_MAX_CALLS = int(os.environ.get("HUK_AI_MAX_CALLS", "3"))
    HUK_AI_MODEL = os.environ.get("HUK_AI_MODEL", "claude-opus-5-5")
    HUK_AI_PRICE_INPUT_PER_MTOK = float(os.environ.get("HUK_AI_PRICE_INPUT_PER_MTOK", "4.0"))
    HUK_AI_PRICE_OUTPUT_PER_MTOK = float(os.environ.get("HUK_AI_PRICE_OUTPUT_PER_MTOK", "20.0"))
    HUK_AI_MAX_OUTPUT_TOKENS = int(os.environ.get("HUK_AI_MAX_OUTPUT_TOKENS", "2000"))
    HUK_AI_MAX_INPUT_CHARS = int(os.environ.get("HUK_AI_MAX_INPUT_CHARS", "3000"))
    HUK_AI_TIMEOUT_SECONDS = float(os.environ.get("HUK_AI_TIMEOUT_SECONDS", "40"))

    # Webrecherche (app/services/web_research.py): HUK-Fragen ueber www.huk.de (ohne Schluessel);
    # allgemeine Themen nur mit Google Programmable Search (offizielle API, 100 Anfragen/Tag frei,
    # darueber kostenpflichtig) - ohne beide Werte ist die allgemeine Suche aus.
    GOOGLE_SEARCH_API_KEY = os.environ.get("GOOGLE_SEARCH_API_KEY")
    GOOGLE_SEARCH_CX = os.environ.get("GOOGLE_SEARCH_CX")
    # Abrechnungen (Originaldateien, nur Buero-Admin)
    SETTLEMENT_STORAGE_DIR = os.environ.get("SETTLEMENT_STORAGE_DIR", str(BASE_DIR / "storage" / "settlements"))

    # ZentriqAI (app/services/ai/): Multi-Anbieter-Router. API-Keys nur in der Umgebung.
    ZENTRIQ_AI_ENABLED = os.environ.get("ZENTRIQ_AI_ENABLED", "true").lower() == "true"
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
    CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY")
    MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY")
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
    # OmniRoute (OpenAI-kompatibles Gateway, z. B. http://127.0.0.1:20128/v1) - optional.
    OMNIROUTE_BASE_URL = os.environ.get("OMNIROUTE_BASE_URL")
    OMNIROUTE_API_KEY = os.environ.get("OMNIROUTE_API_KEY")
    # Kostengrenzen fuer kostenpflichtige Modelle (USD, plattformweit). 0 = keine bezahlten Modelle.
    ZENTRIQ_AI_DAILY_BUDGET_USD = float(os.environ.get("ZENTRIQ_AI_DAILY_BUDGET_USD", "0"))
    ZENTRIQ_AI_MONTHLY_BUDGET_USD = float(os.environ.get("ZENTRIQ_AI_MONTHLY_BUDGET_USD", "0"))
    # Gesamtzeit fuer eine Antwort inkl. Ausweichen - muss unter dem nginx-Timeout (60 s) bleiben.
    ZENTRIQ_AI_TIMEOUT_SECONDS = float(os.environ.get("ZENTRIQ_AI_TIMEOUT_SECONDS", "50"))
    ZENTRIQ_AI_CALL_TIMEOUT_SECONDS = float(os.environ.get("ZENTRIQ_AI_CALL_TIMEOUT_SECONDS", "25"))
    ZENTRIQ_AI_MAX_OUTPUT_TOKENS = int(os.environ.get("ZENTRIQ_AI_MAX_OUTPUT_TOKENS", "2000"))
    ZENTRIQ_AI_MAX_INPUT_CHARS = int(os.environ.get("ZENTRIQ_AI_MAX_INPUT_CHARS", "12000"))
    # Zeichenbudget fuer den mitgesendeten Verlauf (aeltere Nachrichten nur als Zusammenfassung).
    ZENTRIQ_AI_CONTEXT_CHARS = int(os.environ.get("ZENTRIQ_AI_CONTEXT_CHARS", "12000"))
    # Zeichen eines Anhangs (PDF-/Text-Inhalt, Transkript), die an das Modell gehen.
    ZENTRIQ_AI_ATTACHMENT_CHARS = int(os.environ.get("ZENTRIQ_AI_ATTACHMENT_CHARS", "20000"))
    ZENTRIQ_AI_SUMMARY_AFTER_MESSAGES = int(os.environ.get("ZENTRIQ_AI_SUMMARY_AFTER_MESSAGES", "16"))
    # Identische Anfrage (gleicher Benutzer, Verlauf, Modellwahl) aus dem Cache beantworten. 0 = aus.
    ZENTRIQ_AI_CACHE_TTL_SECONDS = int(os.environ.get("ZENTRIQ_AI_CACHE_TTL_SECONDS", "3600"))
    # Faellt ein manuell gewaehltes Modell aus, automatisch auf andere ausweichen.
    ZENTRIQ_AI_MANUAL_FALLBACK = os.environ.get("ZENTRIQ_AI_MANUAL_FALLBACK", "true").lower() == "true"
    # Aufbewahrung: Gespraeche nach N Tagen ohne Aktivitaet loeschen (flask zentriq-ai-purge). 0 = nie.
    ZENTRIQ_AI_RETENTION_DAYS = int(os.environ.get("ZENTRIQ_AI_RETENTION_DAYS", "180"))
    ZENTRIQ_AI_STATE_BACKEND = os.environ.get("ZENTRIQ_AI_STATE_BACKEND", "redis")

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
    # Sitzung endet nach 8 Stunden ohne Aktivitaet (gleitend, auch serverseitig geprueft in
    # app/services/user_sessions.py) - nicht mehr beim Schliessen des Browsers.
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    # Das CSRF-Token gilt so lange wie die Sitzung (Flask-WTF-Standard waeren 60 Minuten). Bis
    # 11/2026 scheiterte Speichern auf Seiten, die laenger als eine Stunde offen waren, mit
    # "Seite veraltet", obwohl die Anmeldung noch gueltig war. Das Token bleibt an die Sitzung
    # gebunden; js/session-guard.js holt nach einer neuen Anmeldung ein frisches.
    WTF_CSRF_TIME_LIMIT = int(PERMANENT_SESSION_LIFETIME.total_seconds())

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
    # "Diesem Browser vertrauen": so viele Tage entfaellt der 2FA-Code bei der Anmeldung in diesem
    # Browser (Passwort bleibt Pflicht). Das Cookie ist immer HttpOnly + SameSite=Strict und in
    # Produktion Secure (nur Tests/lokale Entwicklung ohne HTTPS schalten das ab).
    TWO_FACTOR_TRUST_DAYS = int(os.environ.get("TWO_FACTOR_TRUST_DAYS", "30"))
    TRUSTED_BROWSER_COOKIE_SECURE = os.environ.get("TRUSTED_BROWSER_COOKIE_SECURE", "true").lower() == "true"

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
    TRUSTED_BROWSER_COOKIE_SECURE = False
    # Verhindert echte/gemockte OpenAI-Aufrufe fuer den Analysebericht-Text in der gesamten
    # bestehenden Testsuite; der Narrativ-Pfad wird gezielt in test_analysis_report.py getestet.
    ANALYSIS_NARRATIVE_ENABLED = False
    # Die bestehende Testsuite meldet Konten ohne 2FA an. Die Pflicht-Einrichtung wird in
    # tests/test_two_factor.py gezielt mit TWO_FACTOR_ENFORCED = True geprueft.
    TWO_FACTOR_ENFORCED = False
    PUBLIC_URL = "https://zentriq.test"
    SMTP_HOST = None
    MAIL_FROM = None
    # Nie echte Anthropic-Aufrufe in Tests; tests/test_assistant.py setzt einen Test-Key und
    # ersetzt den Client.
    ANTHROPIC_API_KEY = None
    # ZentriqAI: nie echte Anbieter-Aufrufe; Zustand prozesslokal statt Redis.
    GEMINI_API_KEY = None
    GROQ_API_KEY = None
    CEREBRAS_API_KEY = None
    MISTRAL_API_KEY = None
    OPENROUTER_API_KEY = None
    OMNIROUTE_API_KEY = None
    ZENTRIQ_AI_STATE_BACKEND = "memory"


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
