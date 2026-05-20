import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def str_to_bool(value):
    return str(value).lower() in ("true", "1", "yes", "on")


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")

    if not SECRET_KEY:
        raise ValueError("SECRET_KEY is missing. Add it to your .env file.")

    FLASK_ENV = os.getenv("FLASK_ENV", "production")
    DEBUG = str_to_bool(os.getenv("FLASK_DEBUG", "False"))

    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 20 * 1024 * 1024))

    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{os.path.join(BASE_DIR, 'policedesk.db')}"
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False
