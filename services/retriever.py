import json
import math
from types import SimpleNamespace

from sqlalchemy import text

from models import db, Document, DocumentChunk
from services.embeddings import create_embedding


def cosine_similarity(vector_a, vector_b):
    """
    Old fallback search helper.
    Calculates similarity between two vectors.
    """
    dot_product = sum(a * b for a, b in zip(vector_a, vector_b))

    magnitude_a = math.sqrt(sum(a * a for a in vector_a))
    magnitude_b = math.sqrt(sum(b * b for b in vector_b))

    if magnitude_a == 0 or magnitude_b == 0:
        return 0

    return dot_product / (magnitude_a * magnitude_b)


def python_fallback_search(query, limit=5):
    """
    Backup search using embedding_json.
    Useful if pgvector fails or while still testing locally.
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

            scored_results.append({
                "chunk": chunk,
                "score": score
            })

        except Exception:
            continue

    scored_results.sort(key=lambda item: item["score"], reverse=True)

    return scored_results[:limit]


def pgvector_search(query, limit=5):
    """
    Main semantic search using Supabase pgvector.
    """
    query_embedding = create_embedding(query)
    query_vector = "[" + ",".join(str(value) for value in query_embedding) + "]"

    sql = text("""
        select *
        from match_document_chunks(
            cast(:query_embedding as extensions.vector),
            :match_count
        )
    """)

    rows = db.session.execute(
        sql,
        {
            "query_embedding": query_vector,
            "match_count": limit
        }
    ).mappings().all()

    results = []

    for row in rows:
        document = SimpleNamespace(
            id=row["document_id"],
            title=row["document_title"],
            category=row["category"]
        )

        chunk = SimpleNamespace(
            id=row["chunk_id"],
            document_id=row["document_id"],
            document=document,
            chunk_number=row["chunk_number"],
            chunk_text=row["chunk_text"],
            word_count=row["word_count"],
            page_start=row["page_start"],
            page_end=row["page_end"]
        )

        results.append({
            "chunk": chunk,
            "score": row["similarity"]
        })

    return results


def semantic_search(query, limit=5):
    """
    Main search function used by the chat route.
    Try pgvector first. If it fails, fallback to Python JSON search.
    """
    try:
        return pgvector_search(query, limit=limit)
    except Exception as e:
        print(f"pgvector search failed, using Python fallback: {e}")
        return python_fallback_search(query, limit=limit)