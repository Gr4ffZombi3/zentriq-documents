from celery import Celery, Task


def make_celery(app):
    class FlaskTask(Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

        def on_failure(self, exc, task_id, args, kwargs, einfo):
            # Technisches Fehlerprotokoll (nur Aufgabenname und Fehlerklasse, keine Inhalte).
            from app.services.system_errors import record_system_error

            with app.app_context():
                record_system_error("task", type(exc).__name__, location=self.name)

    celery_app = Celery(app.import_name, task_cls=FlaskTask)
    celery_app.conf.update(
        broker_url=app.config["CELERY_BROKER_URL"],
        result_backend=app.config["CELERY_RESULT_BACKEND"],
        task_always_eager=app.config.get("CELERY_TASK_ALWAYS_EAGER", False),
    )
    celery_app.set_default()
    app.extensions["celery"] = celery_app
    return celery_app
