import json
import os
import random
from dotenv import load_dotenv
from openai import OpenAI

from models import DocumentChunk, CBTQuestionBank

load_dotenv()

CBT_MODEL = "gpt-4.1-mini"


def get_random_bank_questions(document_id, question_count):
    """
    Gets random saved CBT questions from the question bank for one document.
    """

    questions = (
        CBTQuestionBank.query
        .filter_by(document_id=document_id)
        .all()
    )

    if not questions:
        return []

    random.shuffle(questions)

    return questions[:question_count]


def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing. Check your .env file.")

    return OpenAI(api_key=api_key)


def clean_json_response(response_text):
    text = response_text.strip()

    if text.startswith("```json"):
        text = text.replace("```json", "", 1).strip()

    if text.startswith("```"):
        text = text.replace("```", "", 1).strip()

    if text.endswith("```"):
        text = text[:-3].strip()

    return text


def get_random_chunks_for_document(document_id, limit=8):
    chunks = (
        DocumentChunk.query
        .filter_by(document_id=document_id)
        .all()
    )

    if not chunks:
        return []

    random.shuffle(chunks)
    return chunks[:limit]


def build_context_from_chunks(chunks):
    context_parts = []

    for chunk in chunks:
        page_text = ""

        if chunk.page_start:
            page_text = f"Page {chunk.page_start}"

            if chunk.page_end and chunk.page_end != chunk.page_start:
                page_text += f"-{chunk.page_end}"

        context_parts.append(
            f"""
[Chunk ID: {chunk.id}]
Chunk Number: {chunk.chunk_number}
{page_text}

Text:
{chunk.chunk_text}
"""
        )

    return "\n\n".join(context_parts)


def distribute_questions(total_questions, document_ids):
    document_count = len(document_ids)

    if document_count == 0:
        return {}

    base_count = total_questions // document_count
    remainder = total_questions % document_count

    distribution = {}

    for index, document_id in enumerate(document_ids):
        count = base_count

        if index < remainder:
            count += 1

        distribution[int(document_id)] = count

    return distribution


def create_randomized_options(correct_answer_text, wrong_options):
    """
    Randomizes answer options so the correct answer is not always A.
    """

    options = []

    options.append({
        "text": correct_answer_text.strip(),
        "is_correct": True
    })

    for wrong_option in wrong_options[:3]:
        options.append({
            "text": wrong_option.strip(),
            "is_correct": False
        })

    if len(options) != 4:
        raise ValueError("Exactly 4 options are required.")

    random.shuffle(options)

    option_letters = ["A", "B", "C", "D"]
    formatted = {}

    correct_letter = None

    for index, option in enumerate(options):
        letter = option_letters[index]
        formatted[f"option_{letter.lower()}"] = option["text"]

        if option["is_correct"]:
            correct_letter = letter

    formatted["correct_answer"] = correct_letter

    return formatted


def validate_generated_question(question):
    required_fields = [
        "question_text",
        "correct_answer_text",
        "wrong_options",
        "explanation",
        "source_chunk_id"
    ]

    for field in required_fields:
        if field not in question:
            return False

    if not question["question_text"].strip():
        return False

    if not question["correct_answer_text"].strip():
        return False

    if not isinstance(question["wrong_options"], list):
        return False

    if len(question["wrong_options"]) < 3:
        return False

    return True


def generate_raw_questions_for_document(document, question_count):
    """
    AI generates question text, correct answer text, and wrong options.
    Python later randomizes A-D.
    """

    chunks = get_random_chunks_for_document(document.id, limit=8)

    if not chunks:
        return []

    context = build_context_from_chunks(chunks)

    system_message = """
You are PoliceDesk AI CBT Generator.

Generate exam-style multiple-choice questions using ONLY the provided document context.

Important rules:
- Do not invent facts outside the provided context.
- Do not decide option letters.
- Do not make the correct answer always the first option.
- Return the correct answer as text only.
- Return three wrong options as a list.
- Wrong options must be believable but clearly incorrect.
- Questions should test understanding, not just word matching.
- Avoid repeated question patterns.
- Avoid vague questions.
- Avoid questions that require information not in the context.
- Use clear professional language.

Return ONLY valid JSON.
Do not include markdown.
Do not include commentary.

JSON format:
{
  "questions": [
    {
      "question_text": "Question here",
      "correct_answer_text": "Correct answer text here",
      "wrong_options": [
        "Wrong option 1",
        "Wrong option 2",
        "Wrong option 3"
      ],
      "explanation": "Short explanation here",
      "source_chunk_id": 123
    }
  ]
}
"""

    user_message = f"""
Document title:
{document.title}

Document category:
{document.category or "Uncategorized"}

Number of questions to generate:
{question_count}

Document context:
{context}
"""

    client = get_openai_client()

    response = client.responses.create(
        model=CBT_MODEL,
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
        temperature=0.6
    )

    response_text = clean_json_response(response.output_text)
    data = json.loads(response_text)

    return data.get("questions", [])[:question_count]


def generate_question_bank_batch(document, question_count=10):
    """
    Generates a batch of randomized CBT questions for one document.
    """

    raw_questions = generate_raw_questions_for_document(
        document=document,
        question_count=question_count
    )

    cleaned_questions = []

    for question in raw_questions:
        if not validate_generated_question(question):
            continue

        try:
            randomized = create_randomized_options(
                correct_answer_text=question["correct_answer_text"],
                wrong_options=question["wrong_options"]
            )

            cleaned_questions.append({
                "question_text": question["question_text"].strip(),
                "option_a": randomized["option_a"],
                "option_b": randomized["option_b"],
                "option_c": randomized["option_c"],
                "option_d": randomized["option_d"],
                "correct_answer": randomized["correct_answer"],
                "explanation": question.get("explanation", "").strip(),
                "source_chunk_id": question.get("source_chunk_id")
            })

        except Exception:
            continue

    return cleaned_questions


def count_question_bank_for_document(document_id):
    return CBTQuestionBank.query.filter_by(document_id=document_id).count()