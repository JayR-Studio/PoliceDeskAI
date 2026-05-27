import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

ANSWER_MODEL = "gpt-4.1-mini"


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing. Check your .env file.")

    return OpenAI(api_key=api_key)


def build_context(search_results):
    """
    Converts retrieved chunks into a clear context block for the AI.
    """

    context_parts = []

    for index, item in enumerate(search_results, start=1):
        chunk = item["chunk"]
        score = item["score"]

        document_title = chunk.document.title
        category = chunk.document.category or "Uncategorized"

        context_parts.append(
            f"""
[Source {index}]
Document: {document_title}
Category: {category}
Chunk Number: {chunk.chunk_number}
Similarity Score: {score:.3f}

Text:
{chunk.chunk_text}
"""
        )

    return "\n\n".join(context_parts)


def get_style_instruction(answer_style):
    """
    Returns strict formatting instruction based on selected answer style.
    """

    styles = {
        "essay": """
Write the answer in essay format.

Rules:
- Use smooth paragraphs.
- Do not use numbered lists.
- Do not use bullet points.
- Do not use headings unless absolutely necessary.
- Explain the answer naturally and professionally.
""",
        "steps": """
Write the answer in step-by-step format.

Rules:
- Use numbered steps.
- Keep each step clear and practical.
- Do not write long essay paragraphs.
""",
        "bullets": """
Write the answer in bullet point format.

Rules:
- Use short bullet points.
- Do not use numbered steps.
- Keep the answer easy to scan.
""",
        "summary": """
Write a short summary.

Rules:
- Keep it brief.
- Use no more than two short paragraphs.
- Avoid unnecessary details.
""",
        "auto": """
Choose the best format for the question.

Rules:
- If the user asks for essay format, write in essay format.
- If the user asks for steps, use numbered steps.
- If the user asks for bullet points, use bullet points.
- If no style is requested, use a clear professional format.
"""
    }

    return styles.get(answer_style, styles["auto"])


def build_chat_history_context(chat_history=None):
    """
    Converts recent chat history into a short context block.
    This helps the AI understand follow-up questions.
    """

    if not chat_history:
        return "No previous conversation context."

    history_parts = []

    for message in chat_history:
        role = "Officer" if message.role == "user" else "PoliceDesk AI"

        history_parts.append(
            f"{role}: {message.content}"
        )

    return "\n".join(history_parts)


def generate_rag_answer(question, search_results, answer_style="auto", chat_history=None):
    """
    Generates a source-backed answer using retrieved document chunks
    and recent conversation context.
    """

    if not search_results:
        return {
            "answer": "I could not find relevant information in the uploaded police documents.",
            "sources": []
        }

    context = build_context(search_results)
    style_instruction = get_style_instruction(answer_style)
    chat_history_context = build_chat_history_context(chat_history)

    system_message = f"""
You are PoliceDesk AI, a police document assistant.

Your highest priority rules:
1. Answer using ONLY the provided source context.
2. Use recent conversation context only to understand follow-up questions.
3. Do not use chat history as an official source.
4. Do not invent procedures, laws, page numbers, sections, facts, or police rules.
5. If the provided source context does not contain enough information, say so clearly.
6. Follow the selected answer style strictly.
7. Do not mention internal source numbers, chunk numbers, chunk IDs, or phrases like “Source 1” or “Chunk 58” in the answer. 
8. Use the retrieved context silently. 
9. If reference is needed, say “according to the referenced document” instead.
10.The app will show source buttons separately below the answer.

Safety and professionalism:
- Give careful, lawful, professional guidance.
- Tell the officer to confirm sensitive, unclear, or high-risk matters with a superior officer.
- Do not help with unlawful conduct, false reporting, coercion, abuse of power, fabrication of evidence, or cover-ups.

Selected answer style:
{style_instruction}

Source handling:
- You may mention the document title naturally in the answer.
- Do not include raw similarity scores.
- Do not mention chunk numbers in the main answer.
- The app will display sources separately below the answer.
"""

    user_message = f"""
Recent conversation context:
{chat_history_context}

Officer's current question:
{question}

Retrieved official source context:
{context}

Write the answer now.
"""

    client = get_openai_client()

    response = client.responses.create(
        model=ANSWER_MODEL,
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
        temperature=0.1
    )

    answer = response.output_text

    sources = []

    for index, item in enumerate(search_results, start=1):
        chunk = item["chunk"]

        sources.append({
            "source_number": index,
            "chunk_id": chunk.id,
            "document_title": chunk.document.title,
            "category": chunk.document.category or "Uncategorized",
            "chunk_number": chunk.chunk_number,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "score": item["score"]
        })

    return {
        "answer": answer,
        "sources": sources
    }
