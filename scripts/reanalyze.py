"""Re-analyze all protocols with full member list."""
from __future__ import annotations
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sqlalchemy import select
from app.database import async_session, Meeting
from anthropic import AsyncAnthropic

client = AsyncAnthropic(api_key=os.getenv("CLAUDE_API_KEY"))

MEMBERS_LIST = """
1. Сергей Стамбровский (stamsergey) — Председатель
2. Ренат Ш — член СД
3. Данила О — член СД
4. Виктория М — член СД
5. Надежда П — член СД
6. Катя Б (Екатерина Бокова) — член СД
7. Сергей И — член СД
8. Дмитрий Е (Дмитрий Егоров) — член СД
9. Егор — член СД
10. Дарья Ю — член СД
11. Мария С (Мария Смирнова) — член СД
Также упоминаются: Лиза Полякова, Федор Любимцев, Екатерина Валерьевна, Косинский, Полина, Алексей, Вадим, Катя Годунова, Дмитрий Петрович, Илья, Ростислав, Дима, Маша, Мурзин
"""


async def reanalyze(meeting):
    prompt = f"""Проанализируй протокол совещания и извлеки ВСЕ задачи с ответственными.

ИЗВЕСТНЫЕ УЧАСТНИКИ:
{MEMBERS_LIST}

ВАЖНО:
- Для каждой задачи определи ответственного из списка участников
- Если задача формулируется как "попросить у Димы" — ответственный тот, кто должен попросить (обычно Председатель или тот, кто это озвучил)
- Если задача "Егор должен подготовить" — ответственный Егор
- Если ответственный неясен из контекста, напиши "не определён"
- Укажи дедлайн если упомянут явно или косвенно
- Верни ТОЛЬКО JSON массив, без пояснений

Формат:
[
  {{"task": "описание задачи на русском", "assignee": "Имя из списка участников или не определён", "deadline": "YYYY-MM-DD или null"}}
]

ПРОТОКОЛ (дата: {meeting.date.strftime('%Y-%m-%d')}, название: {meeting.title}):
{meeting.raw_transcript[:12000]}
"""
    resp = await client.messages.create(
        model="claude-sonnet-4-20250514", max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    text = resp.content[0].text
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        return json.loads(text)
    except Exception:
        print(f"  Parse error, raw: {text[:200]}")
        return []


async def main():
    async with async_session() as session:
        meetings = (await session.execute(
            select(Meeting).where(Meeting.is_confirmed == True).order_by(Meeting.date)
        )).scalars().all()

    print(f"Переанализирую {len(meetings)} протоколов...\n")

    all_tasks = []
    for m in meetings:
        print(f"  [{m.date.strftime('%d.%m.%Y')}] {m.title[:60]}...")
        tasks = await reanalyze(m)
        for t in tasks:
            t["meeting_date"] = m.date.strftime("%d.%m.%Y")
            t["meeting_id"] = m.id
        all_tasks.extend(tasks)
        print(f"    -> {len(tasks)} задач")

    print(f"\n{'='*80}")
    print(f"ИТОГО: {len(all_tasks)} задач из {len(meetings)} протоколов")
    print(f"{'='*80}\n")

    for i, t in enumerate(all_tasks, 1):
        assignee = t.get("assignee", "?")
        deadline = t.get("deadline") or "без срока"
        print(f"{i:2}. [{t['meeting_date']}] {t['task'][:70]}")
        print(f"    -> {assignee} | до {deadline}")

    # Save to file for later use
    with open("data/reanalyzed_tasks.json", "w", encoding="utf-8") as f:
        json.dump(all_tasks, f, ensure_ascii=False, indent=2)
    print(f"\nСохранено в data/reanalyzed_tasks.json")


asyncio.run(main())
