import os

from flask import Flask, g, render_template, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from app.celery_app import make_celery
from app.extensions import csrf, db, login_manager, migrate
from app.services.document_progress import (
    build_document_progress,
    is_document_active_status,
    public_error_message,
)
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

    from flask import got_request_exception

    from app.services.system_errors import record_request_exception

    # Technisches Fehlerprotokoll fuer das Plattform-Panel (ohne Inhalte).
    got_request_exception.connect(record_request_exception, app)
    csrf.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Bitte melde dich an, um fortzufahren."
    login_manager.login_message_category = "error"

    from app import models  # noqa: F401  (ensure models are registered with SQLAlchemy)
    from app.auth.routes import auth_bp
    from app.blueprints.assistant.routes import assistant_bp
    from app.blueprints.bestand.routes import bestand_bp
    from app.blueprints.chat.routes import chat_bp
    from app.blueprints.cockpit.routes import cockpit_bp
    from app.blueprints.customers.routes import customers_bp
    from app.blueprints.dashboard.routes import dashboard_bp
    from app.blueprints.documents.routes import documents_bp
    from app.blueprints.intake.routes import intake_bp
    from app.blueprints.leipziger.routes import leipziger_bp
    from app.blueprints.office.routes import office_bp
    from app.blueprints.platform.routes import platform_bp
    from app.blueprints.portal.routes import portal_bp
    from app.blueprints.potenziale.routes import potenziale_bp
    from app.blueprints.pwa.routes import pwa_bp
    from app.blueprints.recommendations.routes import recommendations_bp
    from app.blueprints.search.routes import search_bp
    from app.blueprints.settings.routes import settings_bp
    from app.blueprints.tasks.routes import tasks_bp
    from app.blueprints.timetracking.routes import timetracking_bp
    from app.blueprints.tools.routes import tools_bp
    from app.blueprints.upload.routes import upload_bp
    from app.models import User

    app.register_blueprint(auth_bp)
    app.register_blueprint(portal_bp)
    app.register_blueprint(leipziger_bp)
    app.register_blueprint(intake_bp)
    app.register_blueprint(tools_bp)
    app.register_blueprint(bestand_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(cockpit_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(assistant_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(customers_bp)
    app.register_blueprint(potenziale_bp)
    app.register_blueprint(pwa_bp)
    app.register_blueprint(recommendations_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(timetracking_bp)
    app.register_blueprint(platform_bp)
    app.register_blueprint(office_bp)

    from app.cli import register_cli

    register_cli(app)

    app.jinja_env.globals["build_document_progress"] = build_document_progress
    app.jinja_env.globals["is_document_active_status"] = is_document_active_status
    app.jinja_env.globals["public_error_message"] = public_error_message

    from app.navigation import build_navigation
    from app.template_filters import register_template_filters

    register_template_filters(app)

    from app.http_performance import register_http_performance

    register_http_performance(app)

    app.context_processor(build_navigation)
    from app.navigation import assistant_panel_actions

    app.jinja_env.globals["assistant_panel_actions"] = assistant_panel_actions

    @app.get("/favicon.ico")
    def favicon():
        # Browser fragen /favicon.ico direkt an (ohne <link>); ohne Antwort zeigen sie ein Globus-Symbol.
        return send_from_directory(
            os.path.join(app.static_folder, "img"), "favicon.ico", mimetype="image/x-icon", max_age=86400
        )

    @login_manager.user_loader
    def load_user(user_id):
        from app.services import user_sessions
        from app.services.account_state import account_login_block_reason

        # Session-ID hat das Format "<id>:<auth_version>". Aendert sich auth_version
        # (Passwortaenderung, Deaktivierung, Loeschung, 2FA-Reset), sind alle bestehenden
        # Sessions sofort ungueltig. Aeltere Sessions ohne Version werden verworfen.
        raw_id, _, raw_version = str(user_id).partition(":")
        if not raw_id.isdigit() or not raw_version.isdigit():
            return None

        # Der eingeloggte Nutzer wird per ID aus der Session geladen, bevor sein eigener
        # Tenant-Kontext ueberhaupt bekannt ist - dieser eine Lookup ist deshalb bewusst
        # ungescoped. Alle benoetigten Attribute werden noch INNERHALB des bypass-Blocks
        # gelesen: war das Objekt durch einen vorherigen Commit expired (SQLAlchemy
        # expire_on_commit), loest erst der ERSTE Attributzugriff den Reload aus - ausserhalb
        # des Blocks wuerde das mangels Tenant-Kontext fehlschlagen.
        with bypass_tenant_scope():
            user = db.session.get(User, int(raw_id))
            if user is None or (user.auth_version or 0) != int(raw_version):
                return None
            tenant_id = user.tenant_id
            # Deaktivierte/geloeschte Konten und Konten deaktivierter Bueros verlieren sofort
            # den Zugriff, nicht erst beim naechsten Login.
            if account_login_block_reason(user) is not None:
                return None
            # Einzeln widerrufene Sitzungen ("auf anderen Geraeten abmelden").
            if not user_sessions.validate_session(user):
                return None
        set_current_tenant_id(tenant_id)
        return user

    @app.before_request
    def _begin_tenant_scope():
        g._tenant_scope_token = begin_request_tenant_scope()

    from app.auth.permissions import enforce_role_access

    # Nach _begin_tenant_scope registriert: current_user wird hier geladen und setzt den
    # Tenant-Kontext des Nutzers.
    app.before_request(enforce_role_access)

    @app.errorhandler(403)
    def _forbidden(error):
        return render_template("errors/403.html"), 403

    # Verstaendliche Fehlerseiten ohne technische Details (keine Stacktraces im Frontend).
    @app.errorhandler(404)
    def _not_found(error):
        if request.accept_mimetypes.best == "application/json":
            return {"error": "Nicht gefunden."}, 404
        return render_template(
            "errors/error.html",
            title="Seite nicht gefunden",
            message="Die aufgerufene Seite gibt es nicht oder sie ist nicht mehr verfügbar.",
        ), 404

    @app.errorhandler(500)
    def _server_error(error):
        db.session.rollback()
        if request.accept_mimetypes.best == "application/json":
            return {"error": "Es ist ein Fehler aufgetreten. Bitte erneut versuchen."}, 500
        return render_template(
            "errors/error.html",
            title="Es ist ein Fehler aufgetreten",
            message="Die Aktion konnte nicht ausgeführt werden. Bitte versuchen Sie es erneut. "
            "Besteht das Problem weiter, wenden Sie sich an Ihren Büro-Admin.",
        ), 500

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
