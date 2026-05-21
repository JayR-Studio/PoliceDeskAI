from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    file_type = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(100), nullable=True)
    total_chunks = db.Column(db.Integer, default=0)

    storage_bucket = db.Column(db.String(255), nullable=True)
    storage_path = db.Column(db.String(500), nullable=True)
    storage_url = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    chunks = db.relationship(
        "DocumentChunk",
        backref="document",
        lazy=True,
        cascade="all, delete-orphan"
    )


class DocumentChunk(db.Model):
    __tablename__ = "document_chunks"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)

    chunk_number = db.Column(db.Integer, nullable=False)
    chunk_text = db.Column(db.Text, nullable=False)
    word_count = db.Column(db.Integer, nullable=False)

    page_start = db.Column(db.Integer, nullable=True)
    page_end = db.Column(db.Integer, nullable=True)

    embedding_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)


class ChatSession(db.Model):
    __tablename__ = "chat_sessions"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    messages = db.relationship(
        "ChatMessage",
        backref="session",
        lazy=True,
        cascade="all, delete-orphan"
    )


class ChatMessage(db.Model):
    __tablename__ = "chat_messages"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("chat_sessions.id"), nullable=False)

    role = db.Column(db.String(20), nullable=False)  # user or assistant
    content = db.Column(db.Text, nullable=False)

    answer_style = db.Column(db.String(50), nullable=True)
    sources_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)
