from flask import Flask

from app.celery_app import make_celery
from app.extensions import db, migrate
from config import get_config


def create_app(config_object=None):
    app = Flask(__name__)
    app.config.from_object(config_object or get_config())

    db.init_app(app)
    migrate.init_app(app, db)
    make_celery(app)

    from app import models  # noqa: F401  (ensure models are registered with SQLAlchemy)
    from app.blueprints.dashboard.routes import dashboard_bp
    from app.blueprints.documents.routes import documents_bp
    from app.blueprints.upload.routes import upload_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(upload_bp)

    return app
