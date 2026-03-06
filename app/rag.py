"""Simple RAG (Retrieval-Augmented Generation) for meeting history search.

v1: keyword-based search over stored chunks.
v2 will use vector embeddings (Qdrant).
"""

from sqlalchemy import select
from app.database import async_session, Meeting, MeetingEmbedding


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks for storage and retrieval."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


async def store_meeting_chunks(meeting_id: int, text: str):
    """Store text chunks for a meeting."""
    chunks = chunk_text(text)
    async with async_session() as session:
        for i, chunk in enumerate(chunks):
            emb = MeetingEmbedding(
                meeting_id=meeting_id,
                chunk_text=chunk,
                chunk_index=i,
            )
            session.add(emb)
        await session.commit()


async def search_relevant_chunks(query: str, limit: int = 5) -> list[str]:
    """Search for relevant meeting chunks based on keyword matching.

    v1: simple keyword overlap scoring.
    v2: will use vector similarity search.
    """
    query_words = set(query.lower().split())

    async with async_session() as session:
        result = await session.execute(
            select(MeetingEmbedding.chunk_text, Meeting.date)
            .join(Meeting, Meeting.id == MeetingEmbedding.meeting_id)
            .order_by(Meeting.date.desc())
        )
        rows = result.all()

    scored = []
    for chunk_text, meeting_date in rows:
        chunk_words = set(chunk_text.lower().split())
        overlap = len(query_words & chunk_words)
        if overlap > 0:
            scored.append((overlap, meeting_date, chunk_text))

    scored.sort(key=lambda x: x[0], reverse=True)

    results = []
    for _, date, text in scored[:limit]:
        date_str = date.strftime("%d.%m.%Y") if date else "?"
        results.append(f"[Совещание от {date_str}]\n{text}")

    return results
