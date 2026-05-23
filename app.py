import os
import json
import uuid
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

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from werkzeug.utils import secure_filename
from config import Config
from models import db, Document, DocumentChunk, ChatSession, ChatMessage
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
@admin_required
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


@app.route("/documents/<int:document_id>/download")
@admin_required
def download_original_document(document_id):
    document = Document.query.get_or_404(document_id)

    if not document.storage_path:
        flash("This document does not have a stored file to download.", "error")
        return redirect(url_for("documents"))

    try:
        signed_url = create_signed_document_url(
            storage_path=document.storage_path,
            bucket=document.storage_bucket,
            expires_in=3600
        )

        return redirect(signed_url)

    except Exception as e:
        flash(f"Could not download document: {str(e)}", "error")
        return redirect(url_for("documents"))


@app.route("/health")
def health_check():
    return {
        "status": "ok",
        "app": "PoliceDesk AI"
    }, 200


if __name__ == "__main__":
    app.run(debug=app.config["DEBUG"])
