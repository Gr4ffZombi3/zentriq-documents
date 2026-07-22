from flask import Flask, g
from werkzeug.middleware.proxy_fix import ProxyFix

from app.celery_app import make_celery
from app.extensions import csrf, db, login_manager, migrate
from app.services.document_progress import build_document_progress, is_document_active_status
from app.tenancy import (
    begin_request_tenant_scope,
    bypass_tenant_scope,
    end_request_tenant_scope,
    set_current_tenant_id,
)
from config import get_config


def create_app(config_object=None):
    app = Flask(__name__)
    app.config.from_object(config_object or get_config())

    # Hinter Nginx (Produktion): vertraut genau einem Proxy-Hop fuer X-Forwarded-For/
    # -Proto/-Host, damit request.remote_addr (Audit-Log) die echte Client-IP zeigt statt
    # 127.0.0.1, und Flask HTTPS korrekt erkennt (relevant fuer SESSION_COOKIE_SECURE).
    # Ohne echten Proxy (lokale Entwicklung) ein No-Op, da die Header dann fehlen.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db)
    make_celery(app)
    csrf.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Bitte melde dich an, um fortzufahren."
    login_manager.login_message_category = "error"

    from app import models  # noqa: F401  (ensure models are registered with SQLAlchemy)
    from app.auth.routes import auth_bp
    from app.blueprints.bestand.routes import bestand_bp
    from app.blueprints.chat.routes import chat_bp
    from app.blueprints.cockpit.routes import cockpit_bp
    from app.blueprints.customers.routes import customers_bp
    from app.blueprints.dashboard.routes import dashboard_bp
    from app.blueprints.documents.routes import documents_bp
    from app.blueprints.potenziale.routes import potenziale_bp
    from app.blueprints.recommendations.routes import recommendations_bp
    from app.blueprints.search.routes import search_bp
    from app.blueprints.settings.routes import settings_bp
    from app.blueprints.tasks.routes import tasks_bp
    from app.blueprints.upload.routes import upload_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(bestand_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(cockpit_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(potenziale_bp)
    app.register_blueprint(recommendations_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(tasks_bp)
    app.jinja_env.globals["build_document_progress"] = build_document_progress
    app.jinja_env.globals["is_document_active_status"] = is_document_active_status

    @login_manager.user_loader
    def load_user(user_id):
        from app.models import User

        # Der eingeloggte Nutzer wird per ID aus der Session geladen, bevor sein eigener
        # Tenant-Kontext ueberhaupt bekannt ist - dieser eine Lookup ist deshalb bewusst
        # ungescoped. tenant_id wird noch INNERHALB des bypass-Blocks gelesen: war das
        # Objekt durch einen vorherigen Commit expired (SQLAlchemy expire_on_commit),
        # loest erst der ERSTE Attributzugriff den Reload aus, nicht schon db.session.get()
        # selbst - ausserhalb des Blocks wuerde das mangels Tenant-Kontext fehlschlagen.
        with bypass_tenant_scope():
            user = db.session.get(User, int(user_id))
            tenant_id = user.tenant_id if user is not None else None
        if user is not None:
            set_current_tenant_id(tenant_id)
        return user

    @app.before_request
    def _begin_tenant_scope():
        g._tenant_scope_token = begin_request_tenant_scope()

    @app.teardown_request
    def _end_tenant_scope(exception=None):
        # Seit Flask 2.2 ist `g` an den App-Context gebunden, nicht den Request-Context.
        # In Tests (und jedem Code, der einen App-Context ueber mehrere simulierte Requests
        # offenhaelt) wuerde Flask-Logins zwischengespeicherter g._login_user sonst ueber
        # Requests hinweg bestehen bleiben und beim naechsten Request NICHT erneut per
        # user_loader geladen werden.
        g.pop("_login_user", None)
        token = g.pop("_tenant_scope_token", None)
        if token is not None:
            end_request_tenant_scope(token)

    return app
