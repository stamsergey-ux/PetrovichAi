"""Scheduler: marks overdue tasks, sends notifications, manages meetings."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import select, and_

from app.database import async_session, Task, Member, ScheduledMeeting


async def mark_overdue_tasks():
    """Mark tasks past deadline as overdue. No notifications sent."""
    now = datetime.utcnow()
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(and_(
                Task.status.in_(["new", "in_progress"]),
                Task.deadline != None,
                Task.deadline < now,
            ))
        )
        overdue_tasks = result.scalars().all()
        for task in overdue_tasks:
            task.status = "overdue"
        if overdue_tasks:
            await session.commit()

            # Push overdue to Aishot
            try:
                from app.webhook import push_event
                for task in overdue_tasks:
                    assignee = await session.get(Member, task.assignee_id) if task.assignee_id else None
                    await push_event("task_overdue", {
                        "task_id": task.id, "title": task.title,
                        "assignee": assignee.name if assignee else "—",
                        "deadline": str(task.deadline) if task.deadline else "—",
                    })
            except Exception:
                pass

            # Notify chairman about overdue tasks
            chairman_result = await session.execute(
                select(Member).where(Member.is_chairman == True)
            )
            chairmen = chairman_result.scalars().all()

            overdue_text = f"🚨 *Просрочено задач: {len(overdue_tasks)}*\n\n"
            for task in overdue_tasks[:10]:
                days_over = (now - task.deadline).days if task.deadline else 0
                overdue_text += f"  🔴 #{task.id} {task.title}\n"
                overdue_text += f"      ⚠️ просрочено на {days_over} дн.\n\n"

            for ch in chairmen:
                try:
                    await bot.send_message(ch.telegram_id, overdue_text)
                except Exception:
                    pass


async def weekly_digest(bot: Bot, group_chat_id: int | None = None):
    """Send weekly digest of task progress."""
    async with async_session() as session:
        all_tasks = (await session.execute(select(Task))).scalars().all()

    now = datetime.utcnow()
    week_ago = now - timedelta(days=7)

    completed_this_week = [t for t in all_tasks if t.completed_at and t.completed_at > week_ago]
    overdue = [t for t in all_tasks if t.status == "overdue"]
    in_progress = [t for t in all_tasks if t.status == "in_progress"]
    new_tasks = [t for t in all_tasks if t.status == "new"]
    total = len(all_tasks)
    done_total = sum(1 for t in all_tasks if t.status == "done")

    # Progress bar
    filled = round(done_total / total * 15) if total else 0
    bar = "▓" * filled + "░" * (15 - filled)

    text = f"📊 ЕЖЕНЕДЕЛЬНЫЙ ДАЙДЖЕСТ\n\n"
    text += f"Прогресс: [{bar}] {done_total}/{total}\n\n"
    text += f"✅ Выполнено за неделю: {len(completed_this_week)}\n"
    text += f"🔵 В работе: {len(in_progress)}\n"
    text += f"⬜ Новые: {len(new_tasks)}\n"
    text += f"🔴 Просрочено: {len(overdue)}\n"

    if overdue:
        text += "\n🚨 Просроченные задачи:\n"
        for t in overdue[:10]:
            text += f"  🔴 #{t.id} {t.title}\n"

    if completed_this_week:
        text += "\n🎉 Выполнено за неделю:\n"
        for t in completed_this_week[:10]:
            text += f"  ✅ #{t.id} {t.title}\n"

    if group_chat_id:
        try:
            await bot.send_message(group_chat_id, text)
        except Exception:
            pass

    # Also send to chairmen
    async with async_session() as session:
        result = await session.execute(select(Member).where(Member.is_chairman == True))
        chairmen = result.scalars().all()

    for ch in chairmen:
        try:
            await bot.send_message(ch.telegram_id, text)
        except Exception:
            pass


async def ensure_weekly_meeting():
    """Auto-create next Wednesday 17:00 meeting if none exists."""
    now = datetime.utcnow()
    days_ahead = (2 - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= 17:
        days_ahead = 7
    next_wednesday = (now + timedelta(days=days_ahead)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )

    async with async_session() as session:
        existing = (await session.execute(
            select(ScheduledMeeting).where(
                ScheduledMeeting.scheduled_date == next_wednesday,
                ScheduledMeeting.is_completed == False,
            )
        )).scalar_one_or_none()

        if not existing:
            meeting = ScheduledMeeting(
                scheduled_date=next_wednesday,
                title="Совещание СД",
            )
            session.add(meeting)
            await session.commit()
            print(f"Auto-created Wednesday meeting: {next_wednesday}")


async def check_upcoming_meetings(bot: Bot):
    """Check for meetings happening in the next 24-48h and trigger pre-meeting actions."""
    from app.handlers.meetings import send_status_requests, generate_and_send_agenda

    await ensure_weekly_meeting()

    now = datetime.utcnow()
    in_48h = now + timedelta(hours=48)
    in_24h = now + timedelta(hours=24)

    async with async_session() as session:
        result = await session.execute(
            select(ScheduledMeeting).where(
                ScheduledMeeting.is_completed == False,
                ScheduledMeeting.status_collection_sent == False,
                ScheduledMeeting.scheduled_date <= in_48h,
                ScheduledMeeting.scheduled_date > now,
            )
        )
        for meeting in result.scalars().all():
            try:
                await send_status_requests(bot, meeting.id)
            except Exception as e:
                print(f"Status collection error: {e}")

        result = await session.execute(
            select(ScheduledMeeting).where(
                ScheduledMeeting.is_completed == False,
                ScheduledMeeting.agenda_sent == False,
                ScheduledMeeting.scheduled_date <= in_24h,
                ScheduledMeeting.scheduled_date > now,
            )
        )
        for meeting in result.scalars().all():
            try:
                await generate_and_send_agenda(bot, meeting.id)
            except Exception as e:
                print(f"Agenda generation error: {e}")


async def run_scheduler(bot: Bot):
    """Main scheduler loop — marks overdue tasks every hour."""
    while True:
        try:
            await mark_overdue_tasks()
        except Exception as e:
            print(f"Scheduler error: {e}")
        await asyncio.sleep(3600)
