import os
import fitz  # PyMuPDF
from docx import Document


def extract_text_from_pdf(file_path):
    text = ""

    with fitz.open(file_path) as pdf:
        for page_number, page in enumerate(pdf, start=1):
            page_text = page.get_text()

            if page_text.strip():
                text += f"\n\n--- Page {page_number} ---\n"
                text += page_text

    return text.strip()


def extract_text_from_docx(file_path):
    document = Document(file_path)
    paragraphs = []

    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            paragraphs.append(paragraph.text.strip())

    return "\n\n".join(paragraphs)


def extract_text_from_txt(file_path):
    with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
        return file.read().strip()


def extract_text(file_path):
    if not os.path.exists(file_path):
        raise FileNotFoundError("The uploaded file was not found.")

    extension = file_path.rsplit(".", 1)[1].lower()

    if extension == "pdf":
        return extract_text_from_pdf(file_path)

    if extension == "docx":
        return extract_text_from_docx(file_path)

    if extension == "txt":
        return extract_text_from_txt(file_path)

    raise ValueError("Unsupported file type.")
