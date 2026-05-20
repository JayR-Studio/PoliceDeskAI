import json
import math

from models import DocumentChunk
from services.embeddings import create_embedding


def cosine_similarity(vector_a, vector_b):
    """
    Calculates similarity between two vectors.
    Higher score means more similar.
    """

    dot_product = sum(a * b for a, b in zip(vector_a, vector_b))

    magnitude_a = math.sqrt(sum(a * a for a in vector_a))
    magnitude_b = math.sqrt(sum(b * b for b in vector_b))

    if magnitude_a == 0 or magnitude_b == 0:
        return 0

    return dot_product / (magnitude_a * magnitude_b)


def semantic_search(query, limit=8):
    """
    Searches document chunks by meaning using embeddings.
    """

    query_embedding = create_embedding(query)

    chunks = (
        DocumentChunk.query
        .filter(DocumentChunk.embedding_json.isnot(None))
        .all()
    )

    scored_results = []

    for chunk in chunks:
        try:
            chunk_embedding = json.loads(chunk.embedding_json)
            score = cosine_similarity(query_embedding, chunk_embedding)

            scored_results.append(
                {
                    "chunk": chunk,
                    "score": score
                }
            )

        except Exception:
            continue

    scored_results.sort(key=lambda item: item["score"], reverse=True)

    return scored_results[:limit]