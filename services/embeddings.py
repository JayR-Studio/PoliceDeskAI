import os
from openai import OpenAI

EMBEDDING_MODEL = "text-embedding-3-small"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def create_embedding(text):
    """
    Converts text into an embedding vector.
    """

    if not text or not text.strip():
        raise ValueError("Cannot create embedding from empty text.")

    # Keep text reasonably sized for embedding.
    cleaned_text = text.replace("\n", " ").strip()

    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=cleaned_text
    )

    return response.data[0].embedding
