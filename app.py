import os
import json
import uuid
import re
from functools import wraps
from dotenv import load_dotenv

load_dotenv()
from services.embeddings import create_embedding
from services.retriever import semantic_search
from services.ai_answer import generate_rag_answer
from services.storage_service import upload_document_to_storage, delete_document_from_storage, \
    create_signed_document_url
from services.document_loader import extract_text
from services.chunker import split_text_into_chunks
from services.summary_generator import generate_document_summary
from services.cbt_generator import distribute_questions, generate_question_bank_batch, get_random_bank_questions, \
    count_question_bank_for_document

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_from_directory, \
    Response
from werkzeug.utils import secure_filename
from config import Config
from models import (db, Document, DocumentChunk, ChatSession, ChatMessage, CBTSession, CBTQuestion, CBTQuestionBank,
                    SavedSummary, User, UserSubscription, UsageLog, UpgradeRequest)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime, timedelta
from sqlalchemy import text

ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}

app = Flask(__name__)
app.config.from_object(Config)

if app.config["FLASK_ENV"] == "production" and app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
    print("WARNING: SQLite is being used in production. Move to PostgreSQL before real deployment.")


@app.template_filter("fromjson")
def fromjson_filter(value):
    if not value:
        return []

    try:
        return json.loads(value)
    except Exception:
        return []


db.init_app(app)

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


def get_max_direct_upload_bytes():
    max_mb = int(os.getenv("MAX_DIRECT_UPLOAD_MB", "4"))
    return max_mb * 1024 * 1024


PLAN_LIMITS = {
    "free_trial": {
        "ai_chat": 10,
        "study_note": 1,
        "cbt_exam": 2
    },
    "basic": {
        "ai_chat": 50,
        "study_note": 3,
        "cbt_exam": 999999
    },
    "standard": {
        "ai_chat": 150,
        "study_note": 10,
        "cbt_exam": 999999
    },
    "premium": {
        "ai_chat": 300,
        "study_note": 20,
        "cbt_exam": 999999
    }
}


PLAN_PRICES = {
    "free_trial": 0,
    "basic": 10000,
    "standard": 20000,
    "premium": 30000
}


def get_plan_duration_days(plan_name):
    if plan_name == "free_trial":
        return 7

    return 365


def get_active_subscription(user):
    if not user:
        return None

    active_subscription = (
        UserSubscription.query
        .filter_by(user_id=user.id)
        .order_by(UserSubscription.created_at.desc())
        .first()
    )

    return active_subscription


def user_can_perform_action(action_type):
    current_user = get_current_user()

    if not current_user:
        return False, "Please login to continue."

    if current_user.account_status == "suspended":
        return False, "Your account has been suspended. Please contact admin."

    if current_user.account_status == "expired":
        return False, "Your account has expired. Please renew your plan to continue."

    if current_user.account_status == "pending":
        return False, "Your account is pending approval. Please contact admin."

    active_subscription = get_active_subscription(current_user)

    if not active_subscription:
        return False, "No active subscription found. Please contact admin."

    if active_subscription.expires_at and active_subscription.expires_at < datetime.now():
        current_user.account_status = "expired"
        db.session.commit()
        return False, "Your subscription has expired. Please renew your plan."

    plan_name = active_subscription.plan_name or "free_trial"

    plan_limits = PLAN_LIMITS.get(plan_name, PLAN_LIMITS["free_trial"])

    allowed_limit = plan_limits.get(action_type, 0)

    usage_log = get_or_create_usage_log(
        user_id=current_user.id,
        action_type=action_type
    )

    if usage_log.count >= allowed_limit:
        friendly_action_names = {
            "ai_chat": "AI chat questions",
            "study_note": "study notes",
            "cbt_exam": "CBT exams"
        }

        friendly_name = friendly_action_names.get(action_type, action_type.replace("_", " "))

        return False, f"You have reached your monthly limit for {friendly_name}. Please upgrade your plan to continue."

    return True, "Allowed"


def get_user_plan_usage_summary(user):
    active_subscription = get_active_subscription(user)

    if active_subscription:
        plan_name = active_subscription.plan_name or "free_trial"
    else:
        plan_name = "free_trial"

    plan_limits = PLAN_LIMITS.get(plan_name, PLAN_LIMITS["free_trial"])

    month, year = get_current_month_year()

    usage_logs = (
        UsageLog.query
        .filter_by(user_id=user.id, month=month, year=year)
        .all()
    )

    usage_summary = {
        "ai_chat": 0,
        "study_note": 0,
        "cbt_exam": 0
    }

    for log in usage_logs:
        usage_summary[log.action_type] = log.count

    return active_subscription, plan_limits, usage_summary


def clean_ai_answer_for_users(answer):
    if not answer:
        return ""

    # Remove patterns like (Source 1, Chunk 58), (source 2), [Chunk 44], etc.
    answer = re.sub(
        r"\s*[\(\[]\s*source\s*\d+\s*,?\s*chunk\s*\d+\s*[\)\]]",
        "",
        answer,
        flags=re.IGNORECASE
    )

    answer = re.sub(
        r"\s*[\(\[]\s*source\s*\d+\s*[\)\]]",
        "",
        answer,
        flags=re.IGNORECASE
    )

    answer = re.sub(
        r"\s*[\(\[]\s*chunk\s*\d+\s*[\)\]]",
        "",
        answer,
        flags=re.IGNORECASE
    )

    return answer.strip()


def uploaded_file_is_too_large(file):
    """
    Checks uploaded file size without permanently consuming the file stream.
    """

    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)

    return file_size > get_max_direct_upload_bytes(), file_size


def get_or_create_chat_session():
    chat_session_id = session.get("chat_session_id")

    if chat_session_id:
        existing_session = ChatSession.query.get(chat_session_id)

        if existing_session:
            return existing_session

    new_session = ChatSession(title="New Chat")
    db.session.add(new_session)
    db.session.commit()

    session["chat_session_id"] = new_session.id

    return new_session


def get_current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    return User.query.get(user_id)


def get_current_month_year():
    now = datetime.utcnow()
    return now.month, now.year


def get_or_create_usage_log(user_id, action_type):
    month, year = get_current_month_year()

    usage_log = UsageLog.query.filter_by(
        user_id=user_id,
        action_type=action_type,
        month=month,
        year=year
    ).first()

    if usage_log:
        return usage_log

    usage_log = UsageLog(
        user_id=user_id,
        action_type=action_type,
        month=month,
        year=year,
        count=0
    )

    db.session.add(usage_log)
    db.session.flush()

    return usage_log


def record_user_usage(action_type):
    user_id = session.get("user_id")

    if not user_id:
        return None

    usage_log = get_or_create_usage_log(
        user_id=user_id,
        action_type=action_type
    )

    usage_log.count += 1
    db.session.commit()

    return usage_log


def user_required(route_function):
    @wraps(route_function)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please login to continue.", "error")
            return redirect(url_for("login"))

        return route_function(*args, **kwargs)

    return wrapper


def is_admin_logged_in():
    if session.get("is_admin") is not True:
        return False

    login_time_text = session.get("admin_login_time")

    if not login_time_text:
        session.pop("is_admin", None)
        return False

    try:
        login_time = datetime.fromisoformat(login_time_text)
    except ValueError:
        session.pop("is_admin", None)
        session.pop("admin_login_time", None)
        return False

    expiry_time = login_time + timedelta(minutes=30)

    if datetime.now() > expiry_time:
        session.pop("is_admin", None)
        session.pop("admin_login_time", None)
        return False

    return True


def admin_required(route_function):
    @wraps(route_function)
    def wrapper(*args, **kwargs):
        if not is_admin_logged_in():
            flash("Please login as admin to access that page.", "error")
            return redirect(url_for("admin_login"))

        return route_function(*args, **kwargs)

    return wrapper


with app.app_context():
    db.create_all()

    if db.engine.url.drivername.startswith("sqlite"):
        try:
            with db.engine.connect() as connection:
                columns = connection.exec_driver_sql(
                    "PRAGMA table_info(document_chunks)"
                ).fetchall()

                column_names = [column[1] for column in columns]

                if "embedding_json" not in column_names:
                    connection.exec_driver_sql(
                        "ALTER TABLE document_chunks ADD COLUMN embedding_json TEXT"
                    )
                    connection.commit()

        except Exception as e:
            print(f"Local migration warning: {e}")


@app.route("/admin/upload-config")
@admin_required
def upload_config():
    return {
        "supabase_url": os.getenv("SUPABASE_URL"),
        "supabase_publishable_key": os.getenv("SUPABASE_PUBLISHABLE_KEY"),
        "bucket": os.getenv("SUPABASE_STORAGE_BUCKET", "police-documents")
    }


@app.route("/admin/process-storage-document", methods=["POST"])
@admin_required
def process_storage_document():
    data = request.get_json()

    title = data.get("title", "").strip()
    category = data.get("category", "").strip()
    filename = secure_filename(data.get("filename", ""))
    storage_path = data.get("storage_path", "")
    storage_bucket = data.get("bucket", os.getenv("SUPABASE_STORAGE_BUCKET", "police-documents"))

    if not filename or not storage_path:
        return {"error": "Filename and storage path are required."}, 400

    file_type = filename.rsplit(".", 1)[1].lower()

    temp_filename = f"{uuid.uuid4().hex[:12]}_{filename}"
    temp_path = os.path.join("/tmp", temp_filename)

    try:
        from services.storage_service import download_document_from_storage

        download_document_from_storage(
            storage_path=storage_path,
            local_file_path=temp_path,
            bucket=storage_bucket
        )

        extracted_text = extract_text(temp_path)

        if not extracted_text:
            return {"error": "No readable text was found in this document."}, 400

        chunks = split_text_into_chunks(extracted_text)

        if not title:
            title = filename.rsplit(".", 1)[0]

        saved_document = Document(
            title=title,
            filename=filename,
            file_type=file_type,
            category=category if category else None,
            total_chunks=len(chunks),
            storage_bucket=storage_bucket,
            storage_path=storage_path,
            storage_url=None
        )

        db.session.add(saved_document)
        db.session.flush()

        for chunk in chunks:
            saved_chunk = DocumentChunk(
                document_id=saved_document.id,
                chunk_number=chunk["chunk_number"],
                chunk_text=chunk["text"],
                word_count=chunk["word_count"],
                page_start=chunk.get("page_start"),
                page_end=chunk.get("page_end")
            )
            db.session.add(saved_chunk)

        db.session.commit()

        return {
            "success": True,
            "message": f"{filename} uploaded and processed successfully.",
            "document_id": saved_document.id,
            "chunks": len(chunks)
        }

    except Exception as e:
        db.session.rollback()
        return {"error": str(e)}, 500

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.route("/admin/create-signed-upload", methods=["POST"])
@admin_required
def create_signed_upload():
    data = request.get_json()

    filename = secure_filename(data.get("filename", ""))

    if not filename:
        return {"error": "Filename is required."}, 400

    if not allowed_file(filename):
        return {"error": "Only PDF, DOCX, and TXT files are allowed."}, 400

    from services.storage_service import get_supabase_client, get_storage_bucket, build_storage_path

    supabase = get_supabase_client()
    bucket = get_storage_bucket()
    storage_path = build_storage_path(filename)

    try:
        signed_data = supabase.storage.from_(bucket).create_signed_upload_url(storage_path)

        return {
            "bucket": bucket,
            "storage_path": storage_path,
            "signed_data": signed_data
        }

    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/db-test")
@admin_required
def db_test():
    try:
        document_count = Document.query.count()
        chunk_count = DocumentChunk.query.count()
        message_count = ChatMessage.query.count()

        vector_count = db.session.execute(
            text("""
                select count(*) 
                from document_chunks 
                where embedding_vector is not null
            """)
        ).scalar()

        return {
            "status": "connected",
            "database": str(db.engine.url).split("@")[-1],
            "documents": document_count,
            "chunks": chunk_count,
            "messages": message_count,
            "vector_embeddings": vector_count
        }, 200

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }, 500


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    return render_template(
        "index.html"
    )


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if is_admin_logged_in():
        return redirect(url_for("documents"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        admin_username = os.getenv("ADMIN_USERNAME")
        admin_password = os.getenv("ADMIN_PASSWORD")

        if username == admin_username and password == admin_password:
            session["is_admin"] = True
            session["admin_login_time"] = datetime.now().isoformat()
            flash("Admin login successful.", "success")
            return redirect(url_for("documents"))

        flash("Invalid admin username or password.", "error")

    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    session.pop("admin_login_time", None)
    flash("Admin logged out successfully.", "success")
    return redirect(url_for("index"))


@app.route("/admin/upload", methods=["GET", "POST"])
@admin_required
def admin_upload():
    extracted_text = None
    uploaded_filename = None
    chunks = []
    saved_document = None

    if request.method == "POST":
        file = request.files.get("document")
        title = request.form.get("title", "").strip()
        category = request.form.get("category", "").strip()

        if not file or file.filename == "":
            flash("Please select a document before uploading.", "error")
            return redirect(url_for("admin_upload"))

        if not allowed_file(file.filename):
            flash("Only PDF, DOCX, and TXT files are allowed.", "error")
            return redirect(url_for("admin_upload"))

        is_too_large, file_size = uploaded_file_is_too_large(file)
        max_mb = int(os.getenv("MAX_DIRECT_UPLOAD_MB", "4"))

        if is_too_large:
            flash(
                f"This file is too large for direct upload on Vercel. Maximum allowed for now is {max_mb}MB. "
                "We will add large-file upload mode next.",
                "error"
            )
            return redirect(url_for("admin_upload"))

        filename = secure_filename(file.filename)
        file_type = filename.rsplit(".", 1)[1].lower()

        unique_local_filename = f"{uuid.uuid4().hex[:12]}_{filename}"
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], unique_local_filename)

        file.save(save_path)

        try:
            extracted_text = extract_text(save_path)
            uploaded_filename = filename

            if not extracted_text:
                flash("Document uploaded, but no readable text was found.", "error")
                return render_template(
                    "admin_upload.html",
                    extracted_text=extracted_text,
                    uploaded_filename=uploaded_filename,
                    chunks=chunks,
                    saved_document=saved_document
                )

            chunks = split_text_into_chunks(extracted_text)
            storage_data = upload_document_to_storage(save_path, filename)

            if not title:
                title = filename.rsplit(".", 1)[0]

            saved_document = Document(
                title=title,
                filename=filename,
                file_type=file_type,
                category=category if category else None,
                total_chunks=len(chunks),
                storage_bucket=storage_data["bucket"],
                storage_path=storage_data["path"],
                storage_url=storage_data["url"]
            )

            db.session.add(saved_document)
            db.session.flush()

            for chunk in chunks:
                saved_chunk = DocumentChunk(
                    document_id=saved_document.id,
                    chunk_number=chunk["chunk_number"],
                    chunk_text=chunk["text"],
                    word_count=chunk["word_count"],
                    page_start=chunk.get("page_start"),
                    page_end=chunk.get("page_end")
                )
                db.session.add(saved_chunk)

            db.session.commit()
            if os.path.exists(save_path):
                os.remove(save_path)

            flash(
                f"{filename} uploaded and saved successfully with {len(chunks)} chunks.",
                "success"
            )

        except Exception as e:
            if "save_path" in locals() and os.path.exists(save_path):
                os.remove(save_path)
            db.session.rollback()
            flash(f"Document uploaded, but processing failed: {str(e)}", "error")

    return render_template(
        "admin_upload.html",
        extracted_text=extracted_text,
        uploaded_filename=uploaded_filename,
        chunks=chunks,
        saved_document=saved_document
    )


@app.route("/documents")
@user_required
def documents():
    all_documents = Document.query.order_by(Document.created_at.desc()).all()

    document_stats = []

    for document in all_documents:
        total_chunks = DocumentChunk.query.filter_by(document_id=document.id).count()

        embedded_chunks = (
            DocumentChunk.query
            .filter_by(document_id=document.id)
            .filter(DocumentChunk.embedding_json.isnot(None))
            .count()
        )

        if total_chunks == 0:
            status = "No chunks"
            progress = 0
        elif embedded_chunks == total_chunks:
            status = "Ready"
            progress = 100
        elif embedded_chunks == 0:
            status = "Not embedded"
            progress = 0
        else:
            status = "Partial"
            progress = round((embedded_chunks / total_chunks) * 100)

        document_stats.append({
            "document": document,
            "total_chunks": total_chunks,
            "embedded_chunks": embedded_chunks,
            "status": status,
            "progress": progress
        })

    return render_template(
        "documents.html",
        document_stats=document_stats
    )


@app.route("/documents/<int:document_id>")
@admin_required
def document_detail(document_id):
    document = Document.query.get_or_404(document_id)

    chunks = (
        DocumentChunk.query
        .filter_by(document_id=document.id)
        .order_by(DocumentChunk.chunk_number.asc())
        .all()
    )

    total_chunks = len(chunks)
    embedded_chunks = sum(1 for chunk in chunks if chunk.embedding_json)

    return render_template(
        "document_detail.html",
        document=document,
        chunks=chunks,
        total_chunks=total_chunks,
        embedded_chunks=embedded_chunks
    )


@app.route("/documents/<int:document_id>/delete", methods=["POST"])
@admin_required
def delete_document(document_id):
    document = Document.query.get_or_404(document_id)

    document_title = document.title
    storage_path = document.storage_path
    storage_bucket = document.storage_bucket

    print("Deleting document:", document_title)
    print("Storage bucket:", storage_bucket)
    print("Storage path:", storage_path)

    try:
        # First delete from Supabase Storage
        if storage_path:
            delete_success = delete_document_from_storage(
                storage_path,
                storage_bucket
            )

            print("Storage delete success:", delete_success)
        else:
            print("No storage path found for this document.")

        # Then delete database record
        db.session.delete(document)
        db.session.commit()

        flash(f"{document_title} deleted successfully.", "success")

    except Exception as e:
        db.session.rollback()
        print(f"Delete failed: {e}")
        flash(f"Failed to delete document: {str(e)}", "error")

    return redirect(url_for("documents"))


@app.route("/chat", methods=["GET", "POST"])
@user_required
def chat():
    chat_session = get_or_create_chat_session()
    answer_style = "auto"
    latest_results = []

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        answer_style = request.form.get("answer_style", "auto").strip()

        if not query:
            flash("Please enter a question.", "error")
            return redirect(url_for("chat"))

        can_use_chat, limit_message = user_can_perform_action("ai_chat")

        if not can_use_chat:
            flash(limit_message, "error")
            return redirect(url_for("chat"))

        user_message = ChatMessage(
            session_id=chat_session.id,
            role="user",
            content=query,
            answer_style=answer_style
        )

        db.session.add(user_message)
        db.session.commit()

        try:
            latest_results = semantic_search(query, limit=5)

            if not latest_results:
                assistant_text = (
                    "I could not find relevant information in the uploaded police documents. "
                    "You may need to upload the right document or generate embeddings for it."
                )
                sources = []
                should_record_usage = False

            else:
                recent_history = (
                    ChatMessage.query
                    .filter_by(session_id=chat_session.id)
                    .order_by(ChatMessage.created_at.desc())
                    .limit(6)
                    .all()
                )

                recent_history = list(reversed(recent_history))

                rag_answer = generate_rag_answer(
                    question=query,
                    search_results=latest_results,
                    answer_style=answer_style,
                    chat_history=recent_history
                )

                assistant_text = clean_ai_answer_for_users(rag_answer["answer"])
                sources = rag_answer["sources"]
                should_record_usage = True

            assistant_message = ChatMessage(
                session_id=chat_session.id,
                role="assistant",
                content=assistant_text,
                answer_style=answer_style,
                sources_json=json.dumps(sources)
            )

            db.session.add(assistant_message)
            db.session.commit()

            session["typewriter_message_id"] = assistant_message.id

            if should_record_usage:
                record_user_usage("ai_chat")

        except Exception as e:
            db.session.rollback()

            error_message = ChatMessage(
                session_id=chat_session.id,
                role="assistant",
                content=f"AI answer failed: {str(e)}",
                answer_style=answer_style
            )

            db.session.add(error_message)
            db.session.commit()

            flash(f"AI answer failed: {str(e)}", "error")

        return redirect(url_for("chat"))

    messages = (
        ChatMessage.query
        .filter_by(session_id=chat_session.id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )

    typewriter_message_id = session.pop("typewriter_message_id", None)

    return render_template(
        "chat.html",
        messages=messages,
        answer_style=answer_style,
        latest_results=latest_results,
        typewriter_message_id=typewriter_message_id
    )


@app.route("/chat/clear", methods=["POST"])
@user_required
def clear_chat():
    chat_session_id = session.get("chat_session_id")

    if chat_session_id:
        chat_session = ChatSession.query.get(chat_session_id)

        if chat_session:
            db.session.delete(chat_session)
            db.session.commit()

    session.pop("chat_session_id", None)

    flash("Chat history cleared.", "success")
    return redirect(url_for("chat"))


@app.route("/admin/embeddings")
@admin_required
def embeddings_page():
    return render_template("embeddings_progress.html")


@app.route("/admin/embeddings/status")
@admin_required
def embeddings_status():
    total_chunks = DocumentChunk.query.count()

    completed_chunks = (
        DocumentChunk.query
        .filter(DocumentChunk.embedding_json.isnot(None))
        .count()
    )

    pending_chunks = total_chunks - completed_chunks

    progress = 0

    if total_chunks > 0:
        progress = round((completed_chunks / total_chunks) * 100, 2)

    return jsonify({
        "total_chunks": total_chunks,
        "completed_chunks": completed_chunks,
        "pending_chunks": pending_chunks,
        "progress": progress
    })


@app.route("/admin/embeddings/process-batch", methods=["POST"])
@admin_required
def process_embedding_batch():
    batch_size = 3

    pending_chunks = (
        DocumentChunk.query
        .filter(DocumentChunk.embedding_json.is_(None))
        .limit(batch_size)
        .all()
    )

    if not pending_chunks:
        return jsonify({
            "done": True,
            "message": "All embeddings have been generated."
        })

    generated_count = 0
    failed_count = 0

    for chunk in pending_chunks:
        try:
            embedding = create_embedding(chunk.chunk_text)
            embedding_vector = "[" + ",".join(str(value) for value in embedding) + "]"

            chunk.embedding_json = json.dumps(embedding)

            db.session.flush()

            db.session.execute(
                text("""
                    update document_chunks
                    set embedding_vector = cast(:embedding_vector as extensions.vector)
                    where id = :chunk_id
                """),
                {
                    "embedding_vector": embedding_vector,
                    "chunk_id": chunk.id
                }
            )

            generated_count += 1

        except Exception as e:
            print(f"Embedding failed for chunk {chunk.id}: {e}")
            failed_count += 1

    db.session.commit()

    return jsonify({
        "done": False,
        "generated_count": generated_count,
        "failed_count": failed_count,
        "message": f"Processed {generated_count} chunks. {failed_count} failed."
    })


@app.route("/admin/backfill-vector-embeddings", methods=["POST"])
@admin_required
def backfill_vector_embeddings():
    chunks = (
        DocumentChunk.query
        .filter(DocumentChunk.embedding_json.isnot(None))
        .all()
    )

    updated_count = 0
    failed_count = 0

    for chunk in chunks:
        try:
            embedding = json.loads(chunk.embedding_json)
            embedding_vector = "[" + ",".join(str(value) for value in embedding) + "]"

            db.session.execute(
                text("""
                    update document_chunks
                    set embedding_vector = cast(:embedding_vector as extensions.vector)
                    where id = :chunk_id
                    and embedding_vector is null
                """),
                {
                    "embedding_vector": embedding_vector,
                    "chunk_id": chunk.id
                }
            )

            updated_count += 1

        except Exception as e:
            print(f"Vector backfill failed for chunk {chunk.id}: {e}")
            failed_count += 1

    db.session.commit()

    if failed_count:
        flash(f"Backfilled {updated_count} vector embeddings. {failed_count} failed.", "error")
    else:
        flash(f"Backfilled {updated_count} vector embeddings successfully.", "success")

    return redirect(url_for("documents"))


# clear embeddings only
@app.route("/admin/clear-embeddings", methods=["POST"])
@admin_required
def clear_embeddings():
    chunks = DocumentChunk.query.all()

    for chunk in chunks:
        chunk.embedding_json = None

    db.session.execute(
        text("""
            update document_chunks
            set embedding_vector = null
        """)
    )

    db.session.commit()

    flash("All embeddings have been cleared successfully.", "success")
    return redirect(url_for("documents"))


# clear all document and chunks
@app.route("/admin/clear-documents", methods=["POST"])
@admin_required
def clear_documents():
    """
    This is useful when:
     - Document records
     - All chunks
     - All embeddings attached to chunks
    """
    DocumentChunk.query.delete()
    Document.query.delete()

    db.session.commit()

    flash("All documents and chunks have been cleared successfully.", "success")
    return redirect(url_for("documents"))


# Clear documents, chunks, and uploaded files
@app.route("/admin/full-reset", methods=["POST"])
@admin_required
def full_reset():
    documents = Document.query.all()

    for document in documents:
        if document.storage_path:
            try:
                delete_document_from_storage(
                    document.storage_path,
                    document.storage_bucket
                )
            except Exception as storage_error:
                print(f"Storage delete warning for {document.id}: {storage_error}")

    DocumentChunk.query.delete()
    Document.query.delete()

    db.session.commit()

    upload_folder = app.config["UPLOAD_FOLDER"]

    for filename in os.listdir(upload_folder):
        file_path = os.path.join(upload_folder, filename)

        if os.path.isfile(file_path) and filename != ".gitkeep":
            os.remove(file_path)

    flash("Full reset completed. Database records, chunks, embeddings, and stored files have been cleared.", "success")
    return redirect(url_for("documents"))


@app.route("/source/<int:chunk_id>")
@user_required
def view_source(chunk_id):
    chunk = DocumentChunk.query.get_or_404(chunk_id)

    return render_template(
        "source_view.html",
        chunk=chunk
    )


# --------------------------------------------------------------------------------------------------------------
#                                    Read Document
# ---------------------------------------------------------------------------------------------------------------


@app.route("/documents/<int:document_id>/read")
@admin_required
def read_document(document_id):
    document = Document.query.get_or_404(document_id)

    selected_chunk_id = request.args.get("chunk_id", type=int)

    chunks = (
        DocumentChunk.query
        .filter_by(document_id=document.id)
        .order_by(DocumentChunk.chunk_number.asc())
        .all()
    )

    if not chunks:
        selected_chunk = None
    elif selected_chunk_id:
        selected_chunk = (
            DocumentChunk.query
            .filter_by(id=selected_chunk_id, document_id=document.id)
            .first()
        )

        if not selected_chunk:
            selected_chunk = chunks[0]
    else:
        selected_chunk = chunks[0]

    current_index = 0

    if selected_chunk:
        for index, chunk in enumerate(chunks):
            if chunk.id == selected_chunk.id:
                current_index = index
                break

    previous_chunk = chunks[current_index - 1] if selected_chunk and current_index > 0 else None
    next_chunk = chunks[current_index + 1] if selected_chunk and current_index < len(chunks) - 1 else None

    return render_template(
        "document_reader.html",
        document=document,
        chunks=chunks,
        selected_chunk=selected_chunk,
        previous_chunk=previous_chunk,
        next_chunk=next_chunk,
        current_index=current_index
    )


@app.route("/documents/<int:document_id>/view-original")
@user_required
def view_original_document(document_id):
    document = Document.query.get_or_404(document_id)

    if not document.storage_path:
        flash("This document does not have a stored original file.", "error")
        return redirect(url_for("documents"))

    try:
        signed_url = create_signed_document_url(
            storage_path=document.storage_path,
            bucket=document.storage_bucket,
            expires_in=3600
        )

        return redirect(signed_url)

    except Exception as e:
        flash(f"Could not open original document: {str(e)}", "error")
        return redirect(url_for("documents"))


# @app.route("/documents/<int:document_id>/download")
# def download_original_document(document_id):
#     document = Document.query.get_or_404(document_id)
#
#     if not document.storage_path:
#         flash("This document does not have a stored file to download.", "error")
#         return redirect(url_for("documents"))
#
#     try:
#         signed_url = create_signed_document_url(
#             storage_path=document.storage_path,
#             bucket=document.storage_bucket,
#             expires_in=3600
#         )
#
#         return redirect(signed_url)
#
#     except Exception as e:
#         flash(f"Could not download document: {str(e)}", "error")
#         return redirect(url_for("documents"))


@app.route("/manifest.json")
def manifest_json():
    manifest = {
        "id": "/",
        "name": "PoliceDesk AI",
        "short_name": "PoliceDesk",
        "description": "AI-powered police document assistant for study, CBT practice, and official document reference.",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#020817",
        "theme_color": "#020817",
        "icons": [
            {
                "src": "/static/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png"
            },
            {
                "src": "/static/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png"
            }
        ]
    }

    return jsonify(manifest)


@app.route("/sw.js")
def pwa_service_worker():
    sw_code = """
const CACHE_NAME = "policedesk-ai-v3";

const STATIC_ASSETS = [
    "/",
    "/static/css/style.css",
    "/static/js/theme-toggle.js",
    "/manifest.json",
    "/static/icons/icon-192.png",
    "/static/icons/icon-512.png",
    "/static/icons/favicon.ico"
];

self.addEventListener("install", function (event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function (cache) {
            return cache.addAll(STATIC_ASSETS);
        })
    );

    self.skipWaiting();
});

self.addEventListener("activate", function (event) {
    event.waitUntil(
        caches.keys().then(function (cacheNames) {
            return Promise.all(
                cacheNames.map(function (cacheName) {
                    if (cacheName !== CACHE_NAME) {
                        return caches.delete(cacheName);
                    }
                })
            );
        })
    );

    self.clients.claim();
});

self.addEventListener("fetch", function (event) {
    if (event.request.method !== "GET") {
        return;
    }

    event.respondWith(
        fetch(event.request).catch(function () {
            return caches.match(event.request);
        })
    );
});
"""

    return Response(sw_code, mimetype="application/javascript")


@app.route("/health")
def health_check():
    return {
        "status": "ok",
        "app": "PoliceDesk AI"
    }, 200


# --------------------------------------------------------------------------------------------------------------
#                                    CBT FEATURE
# ---------------------------------------------------------------------------------------------------------------


@app.route("/cbt", methods=["GET", "POST"])
@user_required
def cbt():
    documents = Document.query.order_by(Document.created_at.desc()).all()

    document_question_counts = {}

    for document in documents:
        document_question_counts[document.id] = CBTQuestionBank.query.filter_by(
            document_id=document.id
        ).count()

    recent_sessions = (
        CBTSession.query
        .filter(CBTSession.score.isnot(None))
        .order_by(CBTSession.created_at.desc())
        .limit(5)
        .all()
    )

    latest_completed_session = (
        CBTSession.query
        .filter(CBTSession.score.isnot(None))
        .order_by(CBTSession.completed_at.desc())
        .first()
    )

    if request.method == "POST":
        selected_documents = request.form.getlist("document_ids")

        if len(selected_documents) < 1:
            flash("Please select at least one document.", "error")
            return redirect(url_for("cbt"))

        if len(selected_documents) > 4:
            flash("You can select a maximum of 4 documents for one CBT exam.", "error")
            return redirect(url_for("cbt"))

        question_count = request.form.get("question_count", "10")

        try:
            question_count = int(question_count)
        except ValueError:
            question_count = 10

        if question_count not in [10, 20, 30]:
            question_count = 10

        selected_document_objects = []

        for document_id in selected_documents:
            document = Document.query.get(int(document_id))

            if document:
                selected_document_objects.append(document)

        if not selected_document_objects:
            flash("No valid documents selected.", "error")
            return redirect(url_for("cbt"))

        # Check whether selected documents have enough saved CBT questions.
        distribution = distribute_questions(
            total_questions=question_count,
            document_ids=selected_documents
        )

        insufficient_documents = []

        for document in selected_document_objects:
            questions_needed = distribution.get(document.id, 0)
            available_questions = document_question_counts.get(document.id, 0)

            if available_questions < 10:
                insufficient_documents.append(
                    f"{document.title} is not CBT-ready yet. It has only {available_questions} question(s)."
                )

            elif available_questions < questions_needed:
                insufficient_documents.append(
                    f"{document.title} needs {questions_needed} questions but has only {available_questions}."
                )

        if insufficient_documents:
            flash(
                "Some selected documents do not have enough CBT questions yet. "
                + " ".join(insufficient_documents)
                + " Admin should generate more questions first.",
                "error"
            )
            return redirect(url_for("cbt"))

        can_start_cbt, limit_message = user_can_perform_action("cbt_exam")

        if not can_start_cbt:
            flash(limit_message, "error")
            return redirect(url_for("cbt"))

        selected_titles = [document.title for document in selected_document_objects]

        session_title = " + ".join(selected_titles[:2])

        if len(selected_titles) > 2:
            session_title += f" + {len(selected_titles) - 2} more"

        cbt_session = CBTSession(
            title=session_title,
            selected_document_ids=",".join(selected_documents),
            total_questions=question_count,
            status="in_progress"
        )

        db.session.add(cbt_session)
        db.session.flush()

        total_selected_questions = 0

        for document in selected_document_objects:
            questions_needed = distribution.get(document.id, 0)

            if questions_needed <= 0:
                continue

            bank_questions = get_random_bank_questions(
                document_id=document.id,
                question_count=questions_needed
            )

            for bank_question in bank_questions:
                exam_question = CBTQuestion(
                    session_id=cbt_session.id,
                    question_text=bank_question.question_text,
                    option_a=bank_question.option_a,
                    option_b=bank_question.option_b,
                    option_c=bank_question.option_c,
                    option_d=bank_question.option_d,
                    correct_answer=bank_question.correct_answer,
                    explanation=bank_question.explanation,
                    source_document_id=bank_question.document_id,
                    source_chunk_id=bank_question.source_chunk_id
                )

                db.session.add(exam_question)
                total_selected_questions += 1

        if total_selected_questions == 0:
            cbt_session.status = "failed"
            db.session.commit()

            flash("No questions could be selected from the question bank.", "error")
            return redirect(url_for("cbt"))

        cbt_session.total_questions = total_selected_questions
        db.session.commit()

        record_user_usage("cbt_exam")

        flash(f"CBT exam started with {total_selected_questions} questions.", "success")
        return redirect(url_for("take_cbt", session_id=cbt_session.id))

    return render_template(
        "cbt.html",
        documents=documents,
        recent_sessions=recent_sessions,
        document_question_counts=document_question_counts,
        latest_completed_session=latest_completed_session
    )


@app.route("/cbt/<int:session_id>")
@user_required
def take_cbt(session_id):
    cbt_session = CBTSession.query.get_or_404(session_id)

    questions = (
        CBTQuestion.query
        .filter_by(session_id=cbt_session.id)
        .order_by(CBTQuestion.id.asc())
        .all()
    )

    return render_template(
        "take_cbt.html",
        cbt_session=cbt_session,
        questions=questions
    )


@app.route("/cbt/generate-bank/<int:document_id>", methods=["POST"])
@admin_required
def generate_cbt_bank(document_id):
    document = Document.query.get_or_404(document_id)

    target_count = 50
    current_count = CBTQuestionBank.query.filter_by(document_id=document.id).count()
    missing_count = max(target_count - current_count, 0)

    if missing_count == 0:
        flash(f"{document.title} already has {target_count} CBT questions.", "success")
        return redirect(url_for("cbt"))

    batch_size = min(10, missing_count)

    try:
        generated_questions = generate_question_bank_batch(
            document=document,
            question_count=batch_size
        )

        saved_count = 0

        for question in generated_questions:
            cbt_bank_question = CBTQuestionBank(
                document_id=document.id,
                source_chunk_id=question.get("source_chunk_id"),
                question_text=question["question_text"],
                option_a=question["option_a"],
                option_b=question["option_b"],
                option_c=question["option_c"],
                option_d=question["option_d"],
                correct_answer=question["correct_answer"],
                explanation=question["explanation"],
                difficulty="standard"
            )

            db.session.add(cbt_bank_question)
            saved_count += 1

        db.session.commit()

        flash(
            f"Generated {saved_count} CBT questions for {document.title}. "
            f"Current bank: {current_count + saved_count}/50.",
            "success"
        )

    except Exception as e:
        db.session.rollback()
        flash(f"Question bank generation failed: {str(e)}", "error")

    return redirect(url_for("cbt"))


@app.route("/cbt/<int:session_id>/submit", methods=["POST"])
@user_required
def submit_cbt(session_id):
    cbt_session = CBTSession.query.get_or_404(session_id)

    questions = (
        CBTQuestion.query
        .filter_by(session_id=cbt_session.id)
        .order_by(CBTQuestion.id.asc())
        .all()
    )

    if not questions:
        flash("No questions found for this CBT session.", "error")
        return redirect(url_for("cbt"))

    score = 0

    for question in questions:
        user_answer = request.form.get(f"question_{question.id}")

        if user_answer:
            question.user_answer = user_answer.strip().upper()

            if question.user_answer == question.correct_answer:
                score += 1

    total_questions = len(questions)
    percentage = round((score / total_questions) * 100, 2)

    cbt_session.score = score
    cbt_session.percentage = percentage
    cbt_session.status = "completed"
    cbt_session.completed_at = datetime.now()

    db.session.commit()

    flash(f"CBT submitted. You scored {score}/{total_questions}.", "success")
    return redirect(url_for("cbt_result", session_id=cbt_session.id))


@app.route("/cbt/<int:session_id>/result")
@user_required
def cbt_result(session_id):
    cbt_session = CBTSession.query.get_or_404(session_id)

    questions = (
        CBTQuestion.query
        .filter_by(session_id=cbt_session.id)
        .order_by(CBTQuestion.id.asc())
        .all()
    )

    wrong_questions = [
        question for question in questions
        if question.user_answer != question.correct_answer
    ]

    weak_sources = {}

    for question in wrong_questions:
        if not question.source_chunk_id:
            continue

        chunk = DocumentChunk.query.get(question.source_chunk_id)

        if not chunk:
            continue

        document = Document.query.get(chunk.document_id)

        if not document:
            continue

        source_key = f"{document.id}-{chunk.id}"

        if source_key not in weak_sources:
            page_label = "Page not available"

            if chunk.page_start:
                page_label = f"Page {chunk.page_start}"

                if chunk.page_end and chunk.page_end != chunk.page_start:
                    page_label += f"–{chunk.page_end}"

            weak_sources[source_key] = {
                "document": document,
                "chunk": chunk,
                "page_label": page_label,
                "wrong_count": 0,
                "questions": []
            }

        weak_sources[source_key]["wrong_count"] += 1
        weak_sources[source_key]["questions"].append(question)

    recommendation_items = []

    if weak_sources:
        sorted_sources = sorted(
            weak_sources.values(),
            key=lambda item: item["wrong_count"],
            reverse=True
        )

        for item in sorted_sources:
            document = item["document"]
            chunk = item["chunk"]

            recommendation_items.append({
                "document_title": document.title,
                "document_id": document.id,
                "chunk_number": chunk.chunk_number,
                "page_label": item["page_label"],
                "wrong_count": item["wrong_count"],
                "preview": chunk.chunk_text[:260] + "..." if len(chunk.chunk_text) > 260 else chunk.chunk_text
            })

        recommendation_text = "\n".join(
            [
                f"Review {item['document_title']}, {item['page_label']} "
                f"(Section {item['chunk_number']}). You missed {item['wrong_count']} question(s) from this area."
                for item in recommendation_items
            ]
        )
    elif wrong_questions:
        recommendation_text = "Review the selected documents again before retaking the CBT."
    else:
        recommendation_text = "Excellent performance. You answered all questions correctly."

    cbt_session.recommendation = recommendation_text
    db.session.commit()

    return render_template(
        "cbt_result.html",
        cbt_session=cbt_session,
        questions=questions,
        recommendation_items=recommendation_items,
        weak_sources=weak_sources
    )


def analyze_cbt_question_quality(question):
    """
    Returns a list of warning messages for weak CBT questions.
    This does not delete or change the question.
    """

    warnings = []

    question_text = (question.question_text or "").strip()
    options = {
        "A": (question.option_a or "").strip(),
        "B": (question.option_b or "").strip(),
        "C": (question.option_c or "").strip(),
        "D": (question.option_d or "").strip(),
    }

    explanation = (question.explanation or "").strip()
    correct_answer = (question.correct_answer or "").strip().upper()

    if len(question_text) < 25:
        warnings.append("Question may be too short.")

    if not question_text.endswith("?"):
        warnings.append("Question does not end with a question mark.")

    if correct_answer not in ["A", "B", "C", "D"]:
        warnings.append("Invalid correct answer.")

    if not explanation:
        warnings.append("Missing explanation.")

    if explanation and len(explanation) < 20:
        warnings.append("Explanation may be too short.")

    for letter, option_text in options.items():
        if not option_text:
            warnings.append(f"Option {letter} is empty.")

        elif len(option_text) < 3:
            warnings.append(f"Option {letter} may be too short.")

    option_values = [value.lower() for value in options.values() if value]

    if len(option_values) != len(set(option_values)):
        warnings.append("Two or more options are identical.")

    correct_option_text = options.get(correct_answer)

    if correct_option_text:
        wrong_options = [
            text for letter, text in options.items()
            if letter != correct_answer
        ]

        if correct_option_text in wrong_options:
            warnings.append("Correct answer text also appears as a wrong option.")

    return warnings


@app.route("/cbt/question-bank")
@admin_required
def cbt_question_bank():
    selected_document_id = request.args.get("document_id", type=int)

    documents = Document.query.order_by(Document.title.asc()).all()

    query = CBTQuestionBank.query

    if selected_document_id:
        query = query.filter_by(document_id=selected_document_id)

    questions = (
        query
        .order_by(CBTQuestionBank.created_at.desc())
        .all()
    )

    document_question_counts = {}

    for document in documents:
        document_question_counts[document.id] = CBTQuestionBank.query.filter_by(
            document_id=document.id
        ).count()

    question_sources = {}

    for question in questions:
        source_info = {
            "document_title": "Unknown document",
            "page_label": "Page not available",
            "chunk_number": "Not available"
        }

        if question.document_id:
            document = Document.query.get(question.document_id)

            if document:
                source_info["document_title"] = document.title

        if question.source_chunk_id:
            chunk = DocumentChunk.query.get(question.source_chunk_id)

            if chunk:
                source_info["chunk_number"] = chunk.chunk_number

                if chunk.page_start:
                    page_label = f"Page {chunk.page_start}"

                    if chunk.page_end and chunk.page_end != chunk.page_start:
                        page_label += f"–{chunk.page_end}"

                    source_info["page_label"] = page_label

        question_sources[question.id] = source_info

    question_quality_flags = {}

    for question in questions:
        question_quality_flags[question.id] = analyze_cbt_question_quality(question)

    return render_template(
        "cbt_question_bank.html",
        documents=documents,
        questions=questions,
        selected_document_id=selected_document_id,
        document_question_counts=document_question_counts,
        question_sources=question_sources,
        question_quality_flags=question_quality_flags
    )


@app.route("/cbt/question-bank/<int:question_id>/delete", methods=["POST"])
@admin_required
def delete_cbt_bank_question(question_id):
    question = CBTQuestionBank.query.get_or_404(question_id)

    try:
        db.session.delete(question)
        db.session.commit()
        flash("CBT question deleted successfully.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Failed to delete CBT question: {str(e)}", "error")

    return redirect(request.referrer or url_for("cbt_question_bank"))


# --------------------------------------------------------------------------------------------------------------
#                                    SUMMARY FEATURE
# ---------------------------------------------------------------------------------------------------------------


@app.route("/summaries", methods=["GET", "POST"])
@user_required
def summaries():
    documents = Document.query.order_by(Document.created_at.desc()).all()

    recent_summaries = (
        SavedSummary.query
        .order_by(SavedSummary.created_at.desc())
        .limit(8)
        .all()
    )

    if request.method == "POST":
        document_id = request.form.get("document_id", type=int)

        if not document_id:
            flash("Please select a document to summarize.", "error")
            return redirect(url_for("summaries"))

        document = Document.query.get(document_id)

        if not document:
            flash("Selected document was not found.", "error")
            return redirect(url_for("summaries"))

        can_generate_summary, limit_message = user_can_perform_action("study_note")

        if not can_generate_summary:
            flash(limit_message, "error")
            return redirect(url_for("summaries"))

        saved_summary = SavedSummary(
            document_id=document.id,
            title=f"Study Notes: {document.title}",
            summary_type="full_document",
            status="generating"
        )

        db.session.add(saved_summary)
        db.session.commit()

        try:
            summary_text = generate_document_summary(document)

            saved_summary.summary_text = summary_text
            saved_summary.status = "ready"

            db.session.commit()

            record_user_usage("study_note")

            flash("Summary generated successfully.", "success")
            return redirect(url_for("view_summary", summary_id=saved_summary.id))

        except Exception as e:
            db.session.rollback()

            saved_summary.status = "failed"
            db.session.add(saved_summary)
            db.session.commit()

            flash(f"Summary generation failed: {str(e)}", "error")
            return redirect(url_for("summaries"))

    return render_template(
        "summaries.html",
        documents=documents,
        recent_summaries=recent_summaries
    )


@app.route("/summaries/<int:summary_id>")
@user_required
def view_summary(summary_id):
    summary = SavedSummary.query.get_or_404(summary_id)

    return render_template(
        "summary_detail.html",
        summary=summary
    )


@app.route("/summaries/<int:summary_id>/delete", methods=["POST"])
@admin_required
def delete_summary(summary_id):
    summary = SavedSummary.query.get_or_404(summary_id)

    try:
        db.session.delete(summary)
        db.session.commit()
        flash("Summary deleted successfully.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Failed to delete summary: {str(e)}", "error")

    return redirect(url_for("summaries"))


# --------------------------------------------------------------------------------------------------------------
#                                    AUTH FEATURES
# ---------------------------------------------------------------------------------------------------------------


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not full_name or not email or not password:
            flash("Please fill in all required fields.", "error")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("register"))

        existing_user = User.query.filter_by(email=email).first()

        if existing_user:
            flash("An account with this email already exists.", "error")
            return redirect(url_for("register"))

        new_user = User(
            full_name=full_name,
            email=email,
            password_hash=generate_password_hash(password),
            role="user",
            account_status="trial"
        )

        db.session.add(new_user)
        db.session.flush()

        trial_subscription = UserSubscription(
            user_id=new_user.id,
            plan_name="free_trial",
            payment_status="active",
            amount_paid=0,
            currency="NGN",
            starts_at=datetime.now(),
            expires_at=datetime.now() + timedelta(days=7)
        )

        db.session.add(trial_subscription)
        db.session.commit()

        session["user_id"] = new_user.id
        session["user_name"] = new_user.full_name
        session["user_role"] = new_user.role

        flash("Account created successfully. Welcome to PoliceDesk AI.", "success")
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        if not email or not password:
            flash("Please enter your email and password.", "error")
            return redirect(url_for("login"))

        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid email or password.", "error")
            return redirect(url_for("login"))

        if user.account_status == "suspended":
            flash("Your account has been suspended. Please contact admin.", "error")
            return redirect(url_for("login"))

        user.last_login = datetime.now()
        db.session.commit()

        session["user_id"] = user.id
        session["user_name"] = user.full_name
        session["user_role"] = user.role

        flash("Login successful.", "success")
        return redirect(url_for("index"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    session.pop("user_name", None)
    session.pop("user_role", None)

    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/usage")
@user_required
def usage_dashboard():
    current_user = get_current_user()

    if not current_user:
        flash("Please login to continue.", "error")
        return redirect(url_for("login"))

    month, year = get_current_month_year()

    active_subscription, plan_limits, usage_summary = get_user_plan_usage_summary(current_user)

    return render_template(
        "usage.html",
        current_user=current_user,
        usage_summary=usage_summary,
        plan_limits=plan_limits,
        month=month,
        year=year,
        active_subscription=active_subscription,
        now=datetime.now()
    )


# --------------------------------------------------------------------------------------------------------------
#                                    ADMIN PAYMENT FEATURES
# ---------------------------------------------------------------------------------------------------------------


@app.route("/admin/users")
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()

    user_subscriptions = {}

    for user in users:
        latest_subscription = (
            UserSubscription.query
            .filter_by(user_id=user.id)
            .order_by(UserSubscription.created_at.desc())
            .first()
        )

        user_subscriptions[user.id] = latest_subscription

    return render_template(
        "admin_users.html",
        users=users,
        user_subscriptions=user_subscriptions
    )


@app.route("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail(user_id):
    user = User.query.get_or_404(user_id)

    subscriptions = (
        UserSubscription.query
        .filter_by(user_id=user.id)
        .order_by(UserSubscription.created_at.desc())
        .all()
    )

    latest_subscription = subscriptions[0] if subscriptions else None

    month, year = get_current_month_year()

    usage_logs = (
        UsageLog.query
        .filter_by(user_id=user.id, month=month, year=year)
        .all()
    )

    usage_summary = {
        "ai_chat": 0,
        "study_note": 0,
        "cbt_exam": 0
    }

    for log in usage_logs:
        usage_summary[log.action_type] = log.count

    return render_template(
        "admin_user_detail.html",
        user=user,
        subscriptions=subscriptions,
        latest_subscription=latest_subscription,
        usage_summary=usage_summary,
        month=month,
        year=year
    )


@app.route("/admin/users/<int:user_id>/subscription", methods=["POST"])
@admin_required
def update_user_subscription(user_id):
    user = User.query.get_or_404(user_id)

    plan_name = request.form.get("plan_name", "free_trial").strip()
    payment_status = request.form.get("payment_status", "active").strip()

    allowed_plans = ["free_trial", "basic", "standard", "premium"]
    allowed_statuses = ["pending", "active", "expired"]

    if plan_name not in allowed_plans:
        flash("Invalid plan selected.", "error")
        return redirect(url_for("admin_users"))

    if payment_status not in allowed_statuses:
        flash("Invalid payment status selected.", "error")
        return redirect(url_for("admin_users"))

    duration_days = get_plan_duration_days(plan_name)

    new_subscription = UserSubscription(
        user_id=user.id,
        plan_name=plan_name,
        payment_status=payment_status,
        amount_paid=PLAN_PRICES.get(plan_name, 0),
        currency="NGN",
        starts_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=duration_days)
    )

    db.session.add(new_subscription)

    if payment_status == "active":
        user.account_status = "active"
    elif payment_status == "expired":
        user.account_status = "expired"
    else:
        user.account_status = "pending"

    db.session.commit()

    flash(f"{user.full_name}'s subscription has been updated.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/status", methods=["POST"])
@admin_required
def update_user_status(user_id):
    user = User.query.get_or_404(user_id)

    new_status = request.form.get("account_status", "").strip()

    allowed_statuses = ["trial", "active", "pending", "expired", "suspended"]

    if new_status not in allowed_statuses:
        flash("Invalid account status.", "error")
        return redirect(url_for("admin_users"))

    user.account_status = new_status
    db.session.commit()

    flash(f"{user.full_name}'s account status has been updated.", "success")
    return redirect(url_for("admin_users"))


@app.route("/upgrade", methods=["GET", "POST"])
@user_required
def upgrade():
    current_user = get_current_user()

    if not current_user:
        flash("Please login to continue.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        requested_plan = request.form.get("requested_plan", "").strip()
        payment_reference = request.form.get("payment_reference", "").strip()
        message = request.form.get("message", "").strip()

        allowed_plans = ["basic", "standard", "premium"]

        if requested_plan not in allowed_plans:
            flash("Please select a valid upgrade plan.", "error")
            return redirect(url_for("upgrade"))

        upgrade_request = UpgradeRequest(
            user_id=current_user.id,
            requested_plan=requested_plan,
            payment_reference=payment_reference if payment_reference else None,
            message=message if message else None,
            status="pending"
        )

        db.session.add(upgrade_request)
        db.session.commit()

        flash("Upgrade request submitted successfully. Admin will review your payment.", "success")
        return redirect(url_for("usage_dashboard"))

    return render_template("upgrade.html")


@app.route("/admin/upgrade-requests")
@admin_required
def admin_upgrade_requests():
    requests = (
        UpgradeRequest.query
        .order_by(UpgradeRequest.created_at.desc())
        .all()
    )

    return render_template(
        "admin_upgrade_requests.html",
        upgrade_requests=requests
    )


@app.route("/admin/upgrade-requests/<int:request_id>/status", methods=["POST"])
@admin_required
def update_upgrade_request_status(request_id):
    upgrade_request = UpgradeRequest.query.get_or_404(request_id)

    new_status = request.form.get("status", "").strip()

    allowed_statuses = ["pending", "approved", "rejected"]

    if new_status not in allowed_statuses:
        flash("Invalid request status.", "error")
        return redirect(url_for("admin_upgrade_requests"))

    if upgrade_request.status == "approved" and new_status == "approved":
        flash("This request has already been approved.", "error")
        return redirect(url_for("admin_upgrade_requests"))

    user = User.query.get(upgrade_request.user_id)

    if not user:
        flash("User attached to this upgrade request was not found.", "error")
        return redirect(url_for("admin_upgrade_requests"))

    try:
        upgrade_request.status = new_status
        upgrade_request.reviewed_at = datetime.utcnow()

        if new_status == "approved":
            requested_plan = upgrade_request.requested_plan

            allowed_plans = ["basic", "standard", "premium"]

            if requested_plan not in allowed_plans:
                flash("Invalid requested plan. Cannot approve this request.", "error")
                return redirect(url_for("admin_upgrade_requests"))

            duration_days = get_plan_duration_days(requested_plan)

            new_subscription = UserSubscription(
                user_id=user.id,
                plan_name=requested_plan,
                payment_status="active",
                amount_paid=PLAN_PRICES.get(requested_plan, 0),
                currency="NGN",
                starts_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(days=duration_days)
            )

            db.session.add(new_subscription)

            user.account_status = "active"

            flash(
                f"{user.full_name}'s {requested_plan.replace('_', ' ').title()} plan has been activated.",
                "success"
            )

        elif new_status == "rejected":
            flash("Upgrade request rejected.", "success")

        else:
            flash("Upgrade request returned to pending.", "success")

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        flash(f"Failed to update upgrade request: {str(e)}", "error")

    return redirect(url_for("admin_upgrade_requests"))


if __name__ == "__main__":
    app.run(debug=app.config["DEBUG"])
