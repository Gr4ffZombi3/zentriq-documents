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
    OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
    OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o")

    TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
    OCR_MIN_CONFIDENCE = float(os.environ.get("OCR_MIN_CONFIDENCE", "60"))
    OCR_MIN_TEXT_LENGTH = int(os.environ.get("OCR_MIN_TEXT_LENGTH", "20"))

    # M12: Analyse-Engine
    FIELD_CONFIDENCE_UNCERTAIN_THRESHOLD = float(os.environ.get("FIELD_CONFIDENCE_UNCERTAIN_THRESHOLD", "70"))
    ANALYSIS_ENGINE_VERSION = os.environ.get("ANALYSIS_ENGINE_VERSION", "m12.1")
    ANALYSIS_PROMPT_VERSION = os.environ.get("ANALYSIS_PROMPT_VERSION", "v1")
    ANALYSIS_NARRATIVE_ENABLED = os.environ.get("ANALYSIS_NARRATIVE_ENABLED", "true").lower() == "true"
    ANALYSIS_NARRATIVE_MODEL = os.environ.get("ANALYSIS_NARRATIVE_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o"))

    # Session-/Cookie-Haertung
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)


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
