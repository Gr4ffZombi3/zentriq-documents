from flask import Flask

from app.celery_app import make_celery
from app.extensions import db, migrate
from app.tenancy import set_current_tenant_id
from config import get_config

DEFAULT_TENANT_SLUG = "default"


def create_app(config_object=None):
    app = Flask(__name__)
    app.config.from_object(config_object or get_config())

    db.init_app(app)
    migrate.init_app(app, db)
    make_celery(app)

    from app import models  # noqa: F401  (ensure models are registered with SQLAlchemy)
    from app.blueprints.dashboard.routes import dashboard_bp
    from app.blueprints.documents.routes import documents_bp
    from app.blueprints.search.routes import search_bp
    from app.blueprints.upload.routes import upload_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(search_bp)

    @app.before_request
    def _set_tenant_context():
        # Uebergangsweise bis M9 (echtes Login-basiertes Tenant-Routing): jede Anfrage
        # laeuft im Kontext eines einzelnen "Default"-Tenants.
        from app.models import Tenant

        tenant = Tenant.query.filter_by(slug=DEFAULT_TENANT_SLUG).first()
        set_current_tenant_id(tenant.id if tenant else None)

    return app
