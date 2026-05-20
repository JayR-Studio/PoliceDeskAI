import re


PAGE_MARKER_PATTERN = re.compile(r"^-{3}\s*Page\s+(\d+)\s*-{3}$", re.IGNORECASE)


def clean_text(text):
    """
    Cleans extracted text before chunking.
    Keeps page markers like --- Page 1 --- so we can track page numbers.
    """
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def build_word_items_with_pages(text):
    """
    Converts text into word items while tracking page numbers.

    Example word item:
    {
        "word": "accident",
        "page": 12
    }
    """
    cleaned_text = clean_text(text)

    if not cleaned_text:
        return []

    word_items = []
    current_page = None

    for line in cleaned_text.splitlines():
        stripped_line = line.strip()

        page_match = PAGE_MARKER_PATTERN.match(stripped_line)

        if page_match:
            current_page = int(page_match.group(1))
            continue

        if not stripped_line:
            continue

        words = stripped_line.split()

        for word in words:
            word_items.append({
                "word": word,
                "page": current_page
            })

    return word_items


def get_page_range(chunk_word_items):
    """
    Returns page_start and page_end for a chunk.
    If no page numbers exist, returns None, None.
    """
    pages = [
        item["page"]
        for item in chunk_word_items
        if item.get("page") is not None
    ]

    if not pages:
        return None, None

    return min(pages), max(pages)


def split_text_into_chunks(text, chunk_size=900, overlap=150):
    """
    Splits text into overlapping chunks and preserves page ranges where available.

    chunk_size and overlap are measured in words.
    """
    word_items = build_word_items_with_pages(text)

    if not word_items:
        return []

    if len(word_items) <= chunk_size:
        page_start, page_end = get_page_range(word_items)

        return [
            {
                "chunk_number": 1,
                "text": " ".join(item["word"] for item in word_items),
                "word_count": len(word_items),
                "page_start": page_start,
                "page_end": page_end
            }
        ]

    chunks = []
    start = 0
    chunk_number = 1

    while start < len(word_items):
        end = start + chunk_size
        chunk_word_items = word_items[start:end]

        chunk_text = " ".join(item["word"] for item in chunk_word_items)
        page_start, page_end = get_page_range(chunk_word_items)

        chunks.append(
            {
                "chunk_number": chunk_number,
                "text": chunk_text,
                "word_count": len(chunk_word_items),
                "page_start": page_start,
                "page_end": page_end
            }
        )

        chunk_number += 1
        start += chunk_size - overlap

    return chunks