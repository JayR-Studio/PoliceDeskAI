import os
from dotenv import load_dotenv
from openai import OpenAI

from models import DocumentChunk

load_dotenv()

SUMMARY_MODEL = "gpt-4.1-mini"


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing. Check your .env file.")

    return OpenAI(api_key=api_key)


def get_summary_chunks_for_document(document_id, limit=12):
    """
    Gets a safe number of chunks from the selected document.
    For now, we summarize the first important chunks.
    Later, we can add full map-reduce summary for very large documents.
    """

    chunks = (
        DocumentChunk.query
        .filter_by(document_id=document_id)
        .order_by(DocumentChunk.chunk_number.asc())
        .limit(limit)
        .all()
    )

    return chunks


def build_summary_context(chunks):
    context_parts = []

    for chunk in chunks:
        page_label = ""

        if chunk.page_start:
            page_label = f"Page {chunk.page_start}"

            if chunk.page_end and chunk.page_end != chunk.page_start:
                page_label += f"–{chunk.page_end}"

        context_parts.append(
            f"""
Section {chunk.chunk_number}
{page_label}

{chunk.chunk_text}
"""
        )

    return "\n\n".join(context_parts)


def generate_document_summary(document):
    """
    Generates a simple study-friendly summary from document chunks.
    """

    chunks = get_summary_chunks_for_document(document.id, limit=12)

    if not chunks:
        raise ValueError("No readable chunks found for this document.")

    context = build_summary_context(chunks)

    system_message = """
    You are PoliceDesk AI, a police document study assistant.

    Your task is to summarize official police documents for easy study.

    Rules:
    - Use ONLY the provided document context.
    - Do not invent laws, procedures, sections, ranks, or facts.
    - If the context is incomplete, say the summary is based only on the available extracted sections.
    - Write in clear, simple English.
    - Use headings.
    - Focus on what an officer needs to understand.
    - Avoid unnecessary long explanations.
    - Do not mention chunk IDs.
    - Do not use markdown symbols like **, ##, ###, or bullet asterisks.
    - Do not bold text with asterisks.
    - Use plain headings written normally.
    - Use numbered sections and dash bullets only.
    """

    user_message = f"""
Document title:
{document.title}

Document category:
{document.category or "Uncategorized"}

Document context:
{context}

Create a study-friendly summary with this structure:

1. Short Overview
2. Key Points
3. Important Duties / Procedures Mentioned
4. Things Officers Should Remember
5. Suggested Areas to Study Further
"""

    client = get_openai_client()

    response = client.responses.create(
        model=SUMMARY_MODEL,
        input=[
            {
                "role": "system",
                "content": system_message
            },
            {
                "role": "user",
                "content": user_message
            }
        ],
        temperature=0.2
    )

    return clean_summary_text(response.output_text)


def clean_summary_text(text):
    """
    Removes common markdown symbols from AI summary output.
    """

    if not text:
        return ""

    cleaned = text.replace("**", "")
    cleaned = cleaned.replace("###", "")
    cleaned = cleaned.replace("##", "")
    cleaned = cleaned.replace("#", "")

    return cleaned.strip()