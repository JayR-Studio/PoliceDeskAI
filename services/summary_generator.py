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

    Your task is to turn official police document content into practical study notes for police officers.

    Rules:
    - Use ONLY the provided document context.
    - Do not invent laws, procedures, sections, ranks, punishments, or facts.
    - If the context is incomplete, clearly say the notes are based only on the available extracted sections.
    - Write in clear, simple English.
    - Make the output useful for study and revision.
    - Focus on what an officer should understand, remember, and apply.
    - Avoid long unnecessary explanations.
    - Do not mention chunk IDs.
    - Do not use markdown symbols like **, ##, ###, or bullet asterisks.
    - Do not bold text with asterisks.
    - Use plain numbered headings.
    - Use dash bullets only where necessary.
    """

    user_message = f"""
    Document title:
    {document.title}

    Document category:
    {document.category or "Uncategorized"}

    Document context:
    {context}

    Create practical police study notes using this exact structure:

    1. Simple Overview
    Give a short, simple explanation of what this document section is about.

    2. Key Points
    List the most important points an officer should know.

    3. Duties and Procedures Mentioned
    Explain any police duties, steps, responsibilities, or procedures mentioned in the document.

    4. Officer Takeaways
    Explain what an officer should remember when applying this knowledge in real police work.

    5. Common Mistakes to Avoid
    List mistakes an officer may make if they misunderstand this section.

    6. Likely CBT Questions
    Create 5 likely CBT-style questions from the document context.
    Do not provide options here. Only list the questions.

    7. Pages or Sections to Study Further
    Mention the pages or sections from the context that the officer should revisit.
    If page numbers are not available, refer to section numbers.
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

    # Remove markdown bullet asterisks at line starts
    lines = cleaned.splitlines()
    cleaned_lines = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("* "):
            stripped = "- " + stripped[2:]

        cleaned_lines.append(stripped)

    return "\n".join(cleaned_lines).strip()
