"""Delete all old tasks, import deduplicated tasks from reanalysis, assign members."""
from __future__ import annotations
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from datetime import datetime
from sqlalchemy import select, delete
from app.database import async_session, Task, Member, Meeting
from app.members_config import find_member_by_transcript_name

# Load reanalyzed tasks
with open("data/reanalyzed_tasks.json", "r", encoding="utf-8") as f:
    raw_tasks = json.load(f)

# Deduplicate: tasks from meetings 4 and 5 (both 04.03.2026) overlap
# Keep only meeting_id=1 (first occurrence) for duplicate tasks
def dedup_tasks(tasks):
    seen = set()
    result = []
    for t in tasks:
        # Normalize key: first 50 chars of task
        key = t["task"][:50].lower().strip()
        if key in seen:
            continue
        seen.add(key)
        result.append(t)
    return result

deduped = dedup_tasks(raw_tasks)


async def main():
    async with async_session() as session:
        # Get all members
        all_members = (await session.execute(select(Member))).scalars().all()
        members_map = {}
        for m in all_members:
            if m.display_name:
                members_map[m.display_name.lower()] = m
            if m.username:
                members_map[m.username.lower()] = m

        # Get meetings for mapping dates
        meetings = (await session.execute(select(Meeting))).scalars().all()
        meeting_by_id = {m.id: m for m in meetings}

        # Delete all existing tasks
        await session.execute(delete(Task))
        await session.flush()
        print(f"Deleted all old tasks")

        # Insert new deduplicated tasks
        created = 0
        assigned_count = 0
        unassigned_count = 0

        for t in deduped:
            assignee_name = t.get("assignee", "не определён")
            assignee_member = None

            if assignee_name and assignee_name != "не определён":
                # Replace old name
                if assignee_name == "Сергей Стамбровский":
                    assignee_name = "Сергей С"
                cfg = find_member_by_transcript_name(assignee_name)
                if cfg and cfg.get("username"):
                    assignee_member = next(
                        (m for m in all_members if m.username and m.username.lower() == cfg["username"].lower()),
                        None
                    )

            deadline = None
            if t.get("deadline"):
                try:
                    deadline = datetime.fromisoformat(t["deadline"])
                except (ValueError, TypeError):
                    pass

            # Find meeting_id
            meeting_id = t.get("meeting_id")

            task = Task(
                meeting_id=meeting_id,
                assignee_id=assignee_member.id if assignee_member else None,
                title=t["task"][:500],
                description=f"Ответственный (из транскрипта): {assignee_name}",
                priority="medium",
                status="new",
                deadline=deadline,
            )
            session.add(task)
            created += 1
            if assignee_member:
                assigned_count += 1
            else:
                unassigned_count += 1

        await session.commit()

        print(f"\nСоздано задач: {created}")
        print(f"С ответственным: {assigned_count}")
        print(f"Без ответственного: {unassigned_count}")

        # Show final list
        result = (await session.execute(
            select(Task, Member)
            .outerjoin(Member, Task.assignee_id == Member.id)
            .order_by(Task.id)
        )).all()

        print(f"\n{'='*80}")
        print(f"ФИНАЛЬНЫЙ СПИСОК ЗАДАЧ ({len(result)})")
        print(f"{'='*80}\n")

        for task, member in result:
            name = member.display_name if member else "НЕ НАЗНАЧЕНО"
            deadline = task.deadline.strftime("%d.%m.%Y") if task.deadline else "без срока"
            meeting = meeting_by_id.get(task.meeting_id)
            m_date = meeting.date.strftime("%d.%m.%Y") if meeting else "?"
            print(f"#{task.id:3} [{m_date}] {task.title[:65]}")
            print(f"      -> {name} | до {deadline}")


asyncio.run(main())
