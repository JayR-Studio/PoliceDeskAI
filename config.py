import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def str_to_bool(value):
    return str(value).lower() in ("true", "1", "yes", "on")


def normalize_database_url(database_url):
    """
    Makes database URLs compatible with SQLAlchemy.
    For Vercel, we use pg8000 because it is pure Python and avoids psycopg2 import issues.
    """
    if not database_url:
        return f"sqlite:///{os.path.join(BASE_DIR, 'policedesk.db')}"

    if database_url.startswith("postgres://"):
        database_url = database_url.replace(
            "postgres://",
            "postgresql+pg8000://",
            1
        )

    if database_url.startswith("postgresql://"):
        database_url = database_url.replace(
            "postgresql://",
            "postgresql+pg8000://",
            1
        )

    if database_url.startswith("postgresql+psycopg2://"):
        database_url = database_url.replace(
            "postgresql+psycopg2://",
            "postgresql+pg8000://",
            1
        )

    return database_url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")

    if not SECRET_KEY:
        raise ValueError("SECRET_KEY is missing. Add it to your .env file.")

    FLASK_ENV = os.getenv("FLASK_ENV", "production")
    DEBUG = str_to_bool(os.getenv("FLASK_DEBUG", "False"))

    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 4 * 1024 * 1024))

    SQLALCHEMY_DATABASE_URI = normalize_database_url(
        os.getenv("DATABASE_URL")
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }