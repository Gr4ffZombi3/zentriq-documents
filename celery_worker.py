from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402
from app.celery_app import make_celery  # noqa: E402

flask_app = create_app()
celery = make_celery(flask_app)

import app.tasks.document_tasks  # noqa: E402,F401
