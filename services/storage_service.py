import os
import uuid
from datetime import datetime
from supabase import create_client
from werkzeug.utils import secure_filename


def get_supabase_client():
    supabase_url = os.getenv("SUPABASE_URL")
    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

    print("SUPABASE_URL:", supabase_url)
    print("SERVICE ROLE KEY EXISTS:", bool(service_role_key))
    print("SERVICE ROLE KEY START:", service_role_key[:10] if service_role_key else None)

    if not supabase_url:
        raise ValueError("SUPABASE_URL is missing in .env")

    if not service_role_key:
        raise ValueError("SUPABASE_SERVICE_ROLE_KEY is missing in .env")

    return create_client(supabase_url, service_role_key)


def get_storage_bucket():
    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "police-documents")
    return bucket


def build_storage_path(original_filename):
    safe_filename = secure_filename(original_filename)

    today = datetime.utcnow()
    unique_id = uuid.uuid4().hex[:12]

    return f"documents/{today.year}/{today.month:02d}/{unique_id}_{safe_filename}"


def upload_document_to_storage(local_file_path, original_filename):
    """
    Uploads the original document file to Supabase Storage.
    Returns storage metadata.
    """

    if not os.path.exists(local_file_path):
        raise FileNotFoundError("Local file was not found for Supabase upload.")

    supabase = get_supabase_client()
    bucket = get_storage_bucket()
    storage_path = build_storage_path(original_filename)

    with open(local_file_path, "rb") as file:
        supabase.storage.from_(bucket).upload(
            path=storage_path,
            file=file,
            file_options={
                "cache-control": "3600",
                "upsert": "false"
            }
        )

    return {
        "bucket": bucket,
        "path": storage_path,
        "url": None
    }


def delete_document_from_storage(storage_path, bucket=None):
    """
    Deletes a document file from Supabase Storage.
    """

    if not storage_path:
        print("No storage path provided.")
        return False

    supabase = get_supabase_client()
    bucket = bucket or get_storage_bucket()

    print("Deleting from Supabase Storage")
    print("Bucket:", bucket)
    print("Path:", storage_path)

    response = supabase.storage.from_(bucket).remove([storage_path])

    print("Supabase delete response:", response)

    return True


def download_document_from_storage(storage_path, local_file_path, bucket=None):
    """
    Downloads a document from Supabase Storage to a local temp path.
    """

    if not storage_path:
        raise ValueError("Storage path is required.")

    supabase = get_supabase_client()
    bucket = bucket or get_storage_bucket()

    response = supabase.storage.from_(bucket).download(storage_path)

    with open(local_file_path, "wb") as file:
        file.write(response)

    return local_file_path

def create_signed_document_url(storage_path, bucket=None, expires_in=3600):
    """
    Creates a temporary signed URL for viewing/downloading a private document.
    expires_in is in seconds.
    """

    if not storage_path:
        raise ValueError("Storage path is required.")

    supabase = get_supabase_client()
    bucket = bucket or get_storage_bucket()

    response = supabase.storage.from_(bucket).create_signed_url(
        path=storage_path,
        expires_in=expires_in
    )

    if isinstance(response, dict):
        signed_url = response.get("signedURL") or response.get("signedUrl") or response.get("signed_url")
    else:
        signed_url = getattr(response, "signed_url", None) or getattr(response, "signedURL", None)

    if not signed_url:
        raise ValueError(f"Could not create signed URL. Response: {response}")

    return signed_url
