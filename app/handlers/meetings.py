"""Meeting analytics handler. No pre-meeting notifications or status requests."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from aiogram import Router
from sqlalchemy import select

from app.database import async_session, Task, Member, Meeting
from app.utils import is_chairman

logger = logging.getLogger(__name__)
router = Router()


async def get_analytics_text() -> str:
    """Generate meeting analytics summary."""
    async with async_session() as session:
        all_tasks = (await session.execute(select(Task))).scalars().all()
        all_meetings = (await session.execute(
            select(Meeting).where(Meeting.is_confirmed == True).order_by(Meeting.date)
        )).scalars().all()

    if not all_tasks:
        return "📊 Недостаточно данных для аналитики."

    total = len(all_tasks)
    done = sum(1 for t in all_tasks if t.status == "done")
    overdue = sum(1 for t in all_tasks if t.status == "overdue")
    in_progress = sum(1 for t in all_tasks if t.status == "in_progress")
    new = sum(1 for t in all_tasks if t.status == "new")
    completion_rate = round(done / total * 100) if total else 0

    tasks_per_meeting = {}
    for t in all_tasks:
        if t.meeting_id:
            tasks_per_meeting.setdefault(t.meeting_id, {"created": 0, "done": 0})
            tasks_per_meeting[t.meeting_id]["created"] += 1
            if t.status == "done":
                tasks_per_meeting[t.meeting_id]["done"] += 1

    now = datetime.utcnow()
    overdue_days = []
    for t in all_tasks:
        if t.deadline and t.status in ("overdue", "new", "in_progress") and t.deadline < now:
            overdue_days.append((now - t.deadline).days)
    avg_overdue = round(sum(overdue_days) / len(overdue_days)) if overdue_days else 0

    overdue_by_member: dict[str, int] = {}
    async with async_session() as session:
        result = await session.execute(
            select(Task, Member)
            .join(Member, Task.assignee_id == Member.id)
            .where(Task.status == "overdue")
        )
        for task, member in result.all():
            name = member.display_name or member.first_name or "?"
            overdue_by_member[name] = overdue_by_member.get(name, 0) + 1

    from html import escape
    text = "📊 <b>АНАЛИТИКА</b>\n\n"
    text += "<b>Общие показатели:</b>\n"
    text += f"  📋 Всего задач: {total}\n"
    text += f"  ✅ Выполнено: {done} ({completion_rate}%)\n"
    text += f"  🔴 Просрочено: {overdue}\n"
    text += f"  🔵 В работе: {in_progress}\n"
    text += f"  ⬜ Новые: {new}\n\n"
    if avg_overdue:
        text += f"  ⏱ Среднее опоздание: {avg_overdue} дн.\n\n"

    if tasks_per_meeting and all_meetings:
        text += "<b>По совещаниям:</b>\n"
        for m in all_meetings[-5:]:
            stats = tasks_per_meeting.get(m.id, {"created": 0, "done": 0})
            rate = round(stats["done"] / stats["created"] * 100) if stats["created"] else 0
            date = m.date.strftime("%d.%m")
            text += f"  📅 {date}: {stats['created']} задач, {rate}% выполнено\n"
        text += "\n"

    if overdue_by_member:
        text += "<b>Просрочки по участникам:</b>\n"
        for name, count in sorted(overdue_by_member.items(), key=lambda x: -x[1]):
            text += f"  🔴 {escape(name)}: {count}\n"

    return text
