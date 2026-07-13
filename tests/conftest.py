import pytest

from app import create_app
from app.extensions import db as _db
from config import TestingConfig


@pytest.fixture()
def app(tmp_path):
    application = create_app(TestingConfig)
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    return _db
