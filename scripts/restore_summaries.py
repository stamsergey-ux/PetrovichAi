"""One-time script: replace AI-generated summaries with original transcript text."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from app.database import async_session, Meeting


async def main():
    async with async_session() as session:
        result = await session.execute(select(Meeting))
        meetings = result.scalars().all()

        updated = 0
        for m in meetings:
            if m.raw_transcript and m.summary != m.raw_transcript:
                old_len = len(m.summary or "")
                m.summary = m.raw_transcript
                updated += 1
                print(f"  ✓ #{m.id} «{(m.title or 'Без названия')[:40]}» — summary {old_len} → {len(m.raw_transcript)} chars")

        await session.commit()

    print(f"\nГотово: обновлено {updated} из {len(meetings)} протоколов.")


if __name__ == "__main__":
    asyncio.run(main())
