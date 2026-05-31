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

    created_at = db.Column(db.DateTime, default=datetime.now)

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
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    title = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)

    user = db.relationship("User", backref="chat_sessions")

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


class CBTSession(db.Model):
    __tablename__ = "cbt_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    title = db.Column(db.String(255), nullable=True)
    selected_document_ids = db.Column(db.Text, nullable=False)

    total_questions = db.Column(db.Integer, default=10)
    score = db.Column(db.Integer, nullable=True)
    percentage = db.Column(db.Float, nullable=True)

    status = db.Column(db.String(50), default="created")
    recommendation = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)
    completed_at = db.Column(db.DateTime, nullable=True)

    questions = db.relationship(
        "CBTQuestion",
        backref="session",
        lazy=True,
        cascade="all, delete-orphan"
    )

    user = db.relationship("User", backref="cbt_sessions")


class CBTQuestion(db.Model):
    __tablename__ = "cbt_questions"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("cbt_sessions.id"), nullable=False)

    question_text = db.Column(db.Text, nullable=False)

    option_a = db.Column(db.Text, nullable=False)
    option_b = db.Column(db.Text, nullable=False)
    option_c = db.Column(db.Text, nullable=False)
    option_d = db.Column(db.Text, nullable=False)

    correct_answer = db.Column(db.String(1), nullable=False)
    user_answer = db.Column(db.String(1), nullable=True)

    explanation = db.Column(db.Text, nullable=True)

    source_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=True)
    source_chunk_id = db.Column(db.Integer, db.ForeignKey("document_chunks.id"), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)


class CBTQuestionBank(db.Model):
    __tablename__ = "cbt_question_bank"

    id = db.Column(db.Integer, primary_key=True)

    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)
    source_chunk_id = db.Column(db.Integer, db.ForeignKey("document_chunks.id"), nullable=True)

    question_text = db.Column(db.Text, nullable=False)

    option_a = db.Column(db.Text, nullable=False)
    option_b = db.Column(db.Text, nullable=False)
    option_c = db.Column(db.Text, nullable=False)
    option_d = db.Column(db.Text, nullable=False)

    correct_answer = db.Column(db.String(1), nullable=False)
    explanation = db.Column(db.Text, nullable=True)

    difficulty = db.Column(db.String(50), default="standard")
    created_at = db.Column(db.DateTime, default=datetime.now)


class SavedSummary(db.Model):
    __tablename__ = "saved_summaries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False)

    title = db.Column(db.String(255), nullable=False)
    summary_type = db.Column(db.String(50), default="full_document")

    summary_text = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default="created")

    created_at = db.Column(db.DateTime, default=datetime.now)

    document = db.relationship("Document", backref="summaries")
    user = db.relationship("User", backref="saved_summaries")
    

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    full_name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    role = db.Column(db.String(50), default="user")
    account_status = db.Column(db.String(50), default="trial")

    failed_login_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)
    last_login = db.Column(db.DateTime, nullable=True)

    subscriptions = db.relationship(
        "UserSubscription",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan"
    )

    usage_logs = db.relationship(
        "UsageLog",
        backref="user",
        lazy=True,
        cascade="all, delete-orphan"
    )


class UserSubscription(db.Model):
    __tablename__ = "user_subscriptions"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    plan_name = db.Column(db.String(50), default="free_trial")
    payment_status = db.Column(db.String(50), default="pending")

    amount_paid = db.Column(db.Integer, default=0)
    currency = db.Column(db.String(10), default="NGN")

    starts_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.now)


class UpgradeRequest(db.Model):
    __tablename__ = "upgrade_requests"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    requested_plan = db.Column(db.String(50), nullable=False)
    payment_reference = db.Column(db.String(255), nullable=True)
    message = db.Column(db.Text, nullable=True)

    status = db.Column(db.String(50), default="pending")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref="upgrade_requests")


class UsageLog(db.Model):
    __tablename__ = "usage_logs"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    action_type = db.Column(db.String(50), nullable=False)

    month = db.Column(db.Integer, nullable=False)
    year = db.Column(db.Integer, nullable=False)

    count = db.Column(db.Integer, default=0)

    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class SupportMessage(db.Model):
    __tablename__ = "support_messages"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    subject = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), default="open")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref="support_messages")