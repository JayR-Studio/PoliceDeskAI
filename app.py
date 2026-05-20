import os
import json
from functools import wraps
from dotenv import load_dotenv

load_dotenv()
from services.embeddings import create_embedding
from services.retriever import semantic_search
from services.ai_answer import generate_rag_answer
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from werkzeug.utils import secure_filename

from config import Config
from models import db, Document, DocumentChunk, ChatSession, ChatMessage
from services.document_loader import extract_text
from services.chunker import split_text_into_chunks
from datetime import datetime, timedelta

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

    try:
        with db.engine.connect() as connection:
            columns = connection.exec_driver_sql("PRAGMA table_info(document_chunks)").fetchall()
            column_names = [column[1] for column in columns]

            if "embedding_json" not in column_names:
                connection.exec_driver_sql("ALTER TABLE document_chunks ADD COLUMN embedding_json TEXT")
                connection.commit()

    except Exception as e:
        print(f"Local migration warning: {e}")


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

        filename = secure_filename(file.filename)
        file_type = filename.rsplit(".", 1)[1].lower()
        save_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
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

            if not title:
                title = filename.rsplit(".", 1)[0]

            saved_document = Document(
                title=title,
                filename=filename,
                file_type=file_type,
                category=category if category else None,
                total_chunks=len(chunks)
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

            flash(
                f"{filename} uploaded and saved successfully with {len(chunks)} chunks.",
                "success"
            )

        except Exception as e:
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
@admin_required
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

    filename = document.filename
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)

    try:
        db.session.delete(document)
        db.session.commit()

        if os.path.exists(file_path) and os.path.isfile(file_path):
            os.remove(file_path)

        flash(f"{document.title} deleted successfully.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Failed to delete document: {str(e)}", "error")

    return redirect(url_for("documents"))


@app.route("/chat", methods=["GET", "POST"])
def chat():
    chat_session = get_or_create_chat_session()
    answer_style = "auto"
    latest_results = []
    rag_answer = None

    if request.method == "POST":
        query = request.form.get("query", "").strip()
        answer_style = request.form.get("answer_style", "auto").strip()

        if not query:
            flash("Please enter a question.", "error")
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
                assistant_text = "I could not find relevant information in the uploaded police documents. You may need to upload or generate embeddings for the right document."
                sources = []
            else:
                rag_answer = generate_rag_answer(
                    question=query,
                    search_results=latest_results,
                    answer_style=answer_style
                )

                assistant_text = rag_answer["answer"]
                sources = rag_answer["sources"]

            assistant_message = ChatMessage(
                session_id=chat_session.id,
                role="assistant",
                content=assistant_text,
                answer_style=answer_style,
                sources_json=json.dumps(sources)
            )
            db.session.add(assistant_message)
            db.session.commit()

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

    return render_template(
        "chat.html",
        messages=messages,
        answer_style=answer_style,
        latest_results=latest_results
    )


@app.route("/chat/clear", methods=["POST"])
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
            chunk.embedding_json = json.dumps(embedding)
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


# clear embeddings only
@app.route("/admin/clear-embeddings", methods=["POST"])
@admin_required
def clear_embeddings():
    """
    This is useful when:
     - You changed embedding model
     - Embeddings generated wrongly
     - You want to regenerate all embeddings
    """
    chunks = DocumentChunk.query.all()

    for chunk in chunks:
        chunk.embedding_json = None

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
    "Use this only when you want a full reset."
    DocumentChunk.query.delete()
    Document.query.delete()

    upload_folder = app.config["UPLOAD_FOLDER"]

    for filename in os.listdir(upload_folder):
        file_path = os.path.join(upload_folder, filename)

        if os.path.isfile(file_path):
            os.remove(file_path)

    db.session.commit()

    flash("Full reset completed. Documents, chunks, embeddings, and uploaded files have been cleared.", "success")
    return redirect(url_for("documents"))


@app.route("/source/<int:chunk_id>")
def view_source(chunk_id):
    chunk = DocumentChunk.query.get_or_404(chunk_id)

    return render_template(
        "source_view.html",
        chunk=chunk
    )


@app.route("/health")
def health_check():
    return {
        "status": "ok",
        "app": "PoliceDesk AI"
    }, 200


if __name__ == "__main__":
    app.run(debug=app.config["DEBUG"])
